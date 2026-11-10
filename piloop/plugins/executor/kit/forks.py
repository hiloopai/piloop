"""The server's relayed calls, each a fresh ``piloop`` process forked from one warm one that imported piloop already.

A relayed call (``piloop.plugins.executor.kit.relay.Runner``) is a ``piloop`` command, which spent most of its CPU on
imports. The template is a process that the server starts on its first relayed call. It imports the commands that
agents relay (``RELAYED``), then forks one child per call. The child takes the call's environment, cwd, stdin, stdout
and stderr, makes ``settings`` and the console again (both are made at import, from the environment), runs the
command, and exits as a ``piloop`` process exits: each call is still a process of its own, as before.

When the template is gone, or a fork fails before the call started, the call runs as a process of its own
(``PILOOP_ARGV``), and the log says so: the next call starts a new template.
"""

import contextlib
import logging
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Mapping, Sequence
from contextlib import ExitStack
from pathlib import Path
from typing import IO, Protocol

from pydantic import BaseModel

from piloop.plugins.contribution import ROUTES_CLOSE_SECONDS
from piloop.proc import PILOOP_ARGV, child_env, run_group

_log = logging.getLogger(__name__)

TEMPLATE_ARGV = (
    sys.executable,
    '-P',
    '-c',
    'import sys; from piloop.plugins.executor.kit.template import serve; serve(int(sys.argv[1]))',
)
"""The template's command line. The child of a call returns from ``serve`` as ``SystemExit``, so the interpreter ends
it as it ends any ``piloop`` process: its exit handlers run, and its output is flushed."""
REQUEST_BYTES = 1 << 20
"""The most that one request may hold: its argv and its environment."""
THREADED = os.EX_SOFTWARE
"""The template's exit code when it runs more than one thread: it forks no call, and the server starts no other."""
CLOSE_SECONDS = ROUTES_CLOSE_SECONDS
"""How long ``Forks.close`` waits for the template to exit once it closed its end of the socket, before it kills it: a
close of the routes that it serves keeps within their bound."""
GO = b'go'
"""What the server sends a call's child once it read the child's pid: only then does the child run the command."""


class Runs(Protocol):
    """Run the ``piloop`` command ``argv`` as ``run_group`` runs a command: in a session of its own, to its end."""

    def __call__(
        self, argv: Sequence[str], *, cwd: Path, env: Mapping[str, str], timeout: float, input: str | None
    ) -> subprocess.CompletedProcess[str]:
        """What it printed, and its exit code.

        Raises:
            subprocess.TimeoutExpired: it ran for longer than ``timeout`` seconds. Its group is dead.
        """
        ...


def processes(
    argv: Sequence[str], *, cwd: Path, env: Mapping[str, str], timeout: float, input: str | None
) -> subprocess.CompletedProcess[str]:
    """``Runs``: a ``piloop`` process of its own, which imports piloop first."""
    return run_group([*PILOOP_ARGV, *argv], cwd=cwd, env=env, timeout=timeout, input=input)


class Request(BaseModel):
    """What the template's child runs: a ``piloop`` command, in ``cwd``, with ``env``."""

    argv: list[str]
    cwd: str
    env: dict[str, str]


class Unforked(Exception):
    """The call did not start in a child of the template: it is safe to run it in a process of its own."""


class Forks:
    """``Runs``: each call a child of one warm template, at most ``size`` at a time.

    The calls over ``size`` wait for one to end. The template starts with the first call, with this process's
    environment for subprocesses and ``home``: the variables of a call, its actor and token, come with each call.
    """

    def __init__(self, home: Path, size: int) -> None:
        self._home = home
        self._threaded = False
        """A template ran more than one thread (``THREADED``): every call runs in a process of its own from then."""
        self._slots = threading.BoundedSemaphore(size)
        self._lock = threading.Lock()
        self._template: tuple[subprocess.Popen[bytes], socket.socket] | None = None
        """The template and this end of its socket, once a call started it."""

    def __call__(
        self, argv: Sequence[str], *, cwd: Path, env: Mapping[str, str], timeout: float, input: str | None
    ) -> subprocess.CompletedProcess[str]:
        with self._slots:
            if not self._threaded:
                try:
                    return self._forked(argv, cwd=cwd, env=env, timeout=timeout, input=input)
                except Unforked as e:
                    _log.warning('a relayed call runs in a process of its own, not forked from the template: %s', e)
            return processes(argv, cwd=cwd, env=env, timeout=timeout, input=input)

    def close(self) -> None:
        """End the template: it exits once its end of the socket reads that this one closed, else it is killed.

        The calls that it started run on. A call after this starts a new template.
        """
        with self._lock:
            template, self._template = self._template, None
        if template is not None:
            process, ours = template
            ours.close()
            try:
                process.wait(CLOSE_SECONDS)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

    def _forked(
        self, argv: Sequence[str], *, cwd: Path, env: Mapping[str, str], timeout: float, input: str | None
    ) -> subprocess.CompletedProcess[str]:
        deadline = time.monotonic() + timeout
        request = Request(argv=list(argv), cwd=str(cwd), env=dict(env)).model_dump_json().encode()
        with ExitStack() as files:
            stdin = files.enter_context(tempfile.TemporaryFile())  # empty, as /dev/null reads, without input
            stdin.write(b'' if input is None else input.encode())
            stdin.seek(0)
            stdout = files.enter_context(tempfile.TemporaryFile())
            stderr = files.enter_context(tempfile.TemporaryFile())
            mine, theirs = _pair()
            files.enter_context(mine)
            with theirs:
                process = self._send(request, [stdin.fileno(), stdout.fileno(), stderr.fileno(), theirs.fileno()])
            try:
                started = _received(mine, deadline)
            except TimeoutError:  # the template took the call, and may still start it: it goes with the template
                self._end(process)
                raise subprocess.TimeoutExpired([*PILOOP_ARGV, *argv], timeout) from None
            if started is None:
                raise Unforked('the template closed the call before its child started it')
            # the child runs only once it reads this. One that died meanwhile sends no code: its exit is 1, below
            with contextlib.suppress(OSError):
                mine.send(GO)
            try:
                code = _received(mine, deadline)
                if code is None:  # it ended with no code, as the interpreter ends on an error: 1
                    code = 1
                else:
                    _received(mine, deadline)  # None: its end closes once its process exited, and its output is out
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):  # it ended meanwhile
                    os.killpg(started, signal.SIGKILL)
                raise subprocess.TimeoutExpired([*PILOOP_ARGV, *argv], timeout) from None
            return subprocess.CompletedProcess(list(argv), code, _text(stdout), _text(stderr))

    def _send(self, request: bytes, fds: list[int]) -> subprocess.Popen[bytes]:
        """Send the call to the template, which this starts unless it runs, and return the template."""
        with self._lock:
            if self._template is not None and (code := self._template[0].poll()) is not None:
                self._template[1].close()
                self._template = None
                if code == THREADED:
                    self._threaded = True
                    raise Unforked('the template ran more than one thread: every relayed call runs so from now on')
                _log.warning('the template of the relayed calls exited with %s: a new one starts', code)
            if self._template is None:
                self._template = self._start()
            process, ours = self._template
        try:
            socket.send_fds(ours, [request], fds)
        except OSError as e:
            self._end(process)
            raise Unforked(f'the template took no call: {e}') from e
        return process

    def _end(self, process: subprocess.Popen[bytes]) -> None:
        """Kill the template ``process``, unless another one replaced it: the next call starts a new one."""
        with self._lock:
            if self._template is not None and self._template[0] is process:
                self._template[1].close()
                self._template = None
        process.kill()
        process.wait()

    def _start(self) -> tuple[subprocess.Popen[bytes], socket.socket]:
        ours, theirs = _pair()
        with theirs:
            try:
                process = subprocess.Popen(
                    [*TEMPLATE_ARGV, str(theirs.fileno())],
                    pass_fds=[theirs.fileno()],
                    env=child_env() | {'PILOOP_HOME': str(self._home)},
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    start_new_session=True,
                )
            except OSError as e:
                ours.close()
                raise Unforked(f'the template did not start: {e}') from e
        return process, ours


def _pair() -> tuple[socket.socket, socket.socket]:
    """A connected pair of Unix sockets that keep each message whole.

    Raises:
        Unforked: the system made none, for want of descriptors, say.
    """
    try:
        return socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    except OSError as e:
        raise Unforked(f'no socket for the call: {e}') from e


def _received(sock: socket.socket, deadline: float) -> int | None:
    """The next number that the child sends: its pid, then its exit code. None once its end closed.

    Raises:
        TimeoutError: ``deadline`` passed first.
    """
    sock.settimeout(max(deadline - time.monotonic(), 0))
    message = sock.recv(64)
    return int(message) if message else None


def _text(file: IO[bytes]) -> str:
    file.seek(0)
    return file.read().decode(errors='replace')
