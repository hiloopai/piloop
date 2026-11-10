"""The processes that serve the API beside the daemon (``[daemon] api_processes``): one leader, N API processes.

The daemon binds the home's Unix socket and the network's listener, then starts each API process with both sockets and
two pipes (``ApiProcesses``). Each API process serves the whole API on the sockets that it inherited: they share one
accept queue, so the one that is idle accepts first. It reads only the database: its own connections, its own doorbell,
its own relay template (``Served``).

- **Ready.** An API process writes ``ready`` on its ready pipe once its servers serve, or why it cannot, and exits.
  ``start`` waits for every first one, and fails with the first refusal: the daemon does not start.
- **Stop.** The daemon's end of the control pipe is the only stop: an API process drains and exits once it reads its
  end. It ignores SIGTERM and SIGINT, which a service manager sends to every process of the daemon: the daemon stops
  its jobs first, while the API processes still take their reports, then closes the pipes. The daemon's death closes
  them too.
- **Respawn.** One that exits after it served is started again, after ``[daemon] api_respawn``. One that was started
  again and exits before it serves counts: after ``[daemon] api_respawns`` in a row the daemon stops.
- **Same code, same settings.** Each API process gets the digest of the daemon's code and settings, and refuses to
  serve with others: a reinstall or an edit of ``config.toml`` while the daemon runs needs a restart.

After gunicorn's arbiter: the master binds, the workers inherit the socket, and the master starts a worker in place of
one that dies.
"""

import hashlib
import os
import select
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from typing import NoReturn

from piloop.daemon.serve import open_files
from piloop.db.session import CLOSE_WAIT_SECONDS
from piloop.net.server import GRACE_SECONDS
from piloop.otel import Tracer
from piloop.plugins.contribution import ROUTES_CLOSE_SECONDS
from piloop.proc import child_env
from piloop.settings import DaemonSettings, Settings
from piloop.version import code_digest

CHILD_ARGV = (sys.executable, '-P', '-c', 'from piloop.daemon.tier import child; child()')
"""An API process's command line. It imports piloop fresh: nothing of the daemon's memory comes with it."""
FDS_ENV = 'PILOOP_API_FDS'
"""What an API process inherits: the Unix socket, the network's (or ``-``), its control pipe and its ready pipe."""
INDEX_ENV = 'PILOOP_API_INDEX'
"""Which of the API processes it is: what its share of a bound of the home depends on."""
DIGEST_ENV = 'PILOOP_API_DIGEST'
"""The digest of the daemon's code and settings (``digest``): an API process serves only with the same."""
READY = b'ready\n'
CLOSE_SECONDS = GRACE_SECONDS + ROUTES_CLOSE_SECONDS + CLOSE_WAIT_SECONDS + 1
"""How long the daemon waits for an API process to exit once it closed its pipe, before it kills it: longer than the
whole close of the process, its servers' grace, its routes' close (a relay template) and its database's."""


class NotServing(RuntimeError):
    """An API process did not serve: the message says which, and why."""


def digest(cfg: Settings) -> str:
    """The digest of this process's code and of ``cfg``: what an API process must find the same as its daemon's."""
    return hashlib.sha256(f'{code_digest()}\0{cfg.model_dump_json()}'.encode()).hexdigest()


@dataclass
class _Slot:
    """One API process, and the daemon's ends of its pipes."""

    process: subprocess.Popen[bytes]
    control: int | None
    """Closing it stops the process. None once closed."""
    ready: int


class ApiProcesses:
    """``count`` API processes on ``sockets``, from ``start`` until ``close``: what the daemon serves the API with.

    It stops and closes as a ``Server`` does, so a daemon stops its servers alike, whichever serve. ``failed`` is
    called, from a thread of its own, when API processes started again keep failing: the daemon then stops.
    """

    def __init__(
        self,
        cfg: Settings,
        sockets: Sequence[socket.socket | None],
        tracer: Tracer,
        failed: Callable[[str], None],
    ) -> None:
        """``sockets``: the home's Unix socket, then the network's, or None when the daemon listens on no network.

        They are its own from here: ``close`` closes them, once no API process holds them.
        """
        self._settings: DaemonSettings = cfg.daemon
        self._home = cfg.home
        self._digest = digest(cfg)
        self._sockets = [s for s in sockets if s is not None]
        self._fds = [None if s is None else s.fileno() for s in sockets]
        self._tracer = tracer
        self._failed = failed
        self._lock = threading.Lock()
        """Held to read or set ``_stopping``, and to start an API process or close a pipe: a stop never misses one."""
        self._stopping = False
        self._stopped = threading.Event()
        """Set with ``_stopping``: what a wait before a respawn ends on."""
        self._slots: dict[int, _Slot] = {}
        self._watchers: list[threading.Thread] = []

    def start(self) -> None:
        """Start each API process, wait until each serves, then watch each one: a new one in place of one that exits.

        Raises:
            NotServing: One did not serve within ``[daemon] api_start``, with why. The others are stopped.
            OSError: One could not be started.
        """
        try:
            with self._lock:
                for index in range(self._settings.api_processes):
                    self._slots[index] = self._spawn(index)
            for index, slot in self._slots.items():
                self._await_ready(index, slot)
        except BaseException:  # the ones that run end, and the sockets close: the daemon does not start
            self.close()
            raise
        for index in self._slots:
            watcher = threading.Thread(target=self._watch, args=(index,), name=f'api-{index}', daemon=True)
            watcher.start()
            self._watchers.append(watcher)

    def pids(self) -> list[int]:
        """The API processes that run now."""
        with self._lock:
            return [slot.process.pid for slot in self._slots.values()]

    def stop(self) -> None:
        """Start to ``close``, and return at once: each API process reads its closed pipe, drains and exits."""
        with self._lock:
            self._stopping = True
            self._stopped.set()
            for slot in self._slots.values():
                self._close_control(slot)

    def close(self, timeout: float = CLOSE_SECONDS) -> None:
        """``stop``, wait ``timeout`` seconds for the API processes to exit, kill the rest, and close the sockets."""
        self.stop()
        deadline = time.monotonic() + timeout
        for watcher in self._watchers:
            watcher.join(max(0.0, deadline - time.monotonic()))
        with self._lock:
            slots = list(self._slots.values())
        for slot in slots:
            try:
                slot.process.wait(max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                slot.process.kill()
                slot.process.wait()
        for watcher in self._watchers:
            watcher.join()
        for slot in slots:
            os.close(slot.ready)
        for each in self._sockets:
            each.close()

    def _spawn(self, index: int) -> _Slot:
        """Start API process ``index``. The caller holds ``_lock``, and has checked that no stop came."""
        control, ours = os.pipe()
        ready, theirs = os.pipe()
        try:
            named = ','.join('-' if fd is None else str(fd) for fd in [*self._fds, control, theirs])
            process = subprocess.Popen(
                CHILD_ARGV,
                pass_fds=[*(fd for fd in self._fds if fd is not None), control, theirs],
                env=child_env()
                | {'PILOOP_HOME': str(self._home), FDS_ENV: named, INDEX_ENV: str(index), DIGEST_ENV: self._digest},
                stdin=subprocess.DEVNULL,
            )
        except BaseException:
            for fd in (ours, ready):
                os.close(fd)
            raise
        finally:
            os.close(control)
            os.close(theirs)
        self._tracer.event('api_process.started', index=index, pid=process.pid)
        return _Slot(process, ours, ready)

    def _close_control(self, slot: _Slot) -> None:
        """Close the daemon's end of ``slot``'s control pipe, once. The caller holds ``_lock``."""
        if slot.control is not None:
            os.close(slot.control)
            slot.control = None

    def _await_ready(self, index: int, slot: _Slot) -> None:
        """Wait until API process ``index`` serves.

        Raises:
            NotServing: It said why it does not, it exited, or ``[daemon] api_start`` passed first.
        """
        waited = self._settings.api_start.total_seconds()
        said, closed = b'', False
        deadline = time.monotonic() + waited
        while not said.endswith(b'\n') and (left := deadline - time.monotonic()) > 0:
            if not select.select([slot.ready], [], [], left)[0]:
                break
            if not (chunk := os.read(slot.ready, 4096)):
                closed = True
                break
            said += chunk
        if said == READY:
            return
        if said:
            why = said.decode(errors='replace').strip()
        elif closed:  # it closes its pipe as it exits, a moment before it can be reaped
            try:
                why = f'it exited with {slot.process.wait(max(1.0, deadline - time.monotonic()))}'
            except subprocess.TimeoutExpired:
                why = 'it closed its ready pipe, and runs on'
        else:
            why = f'not within {waited:g} s'
        raise NotServing(f'API process {index} (pid {slot.process.pid}) does not serve: {why}')

    def _watch(self, index: int) -> None:
        """Wait for API process ``index`` to exit, then start a new one in its place once it waited, until ``stop``.

        A new one that does not serve counts as a failure; ``[daemon] api_respawns`` of them in a row stop the daemon.
        """
        failures = 0
        while True:
            with self._lock:
                slot = self._slots[index]
            code = slot.process.wait()
            with self._lock:
                if self._stopping:
                    return
            self._tracer.event('api_process.exited', index=index, pid=slot.process.pid, code=code)
            self._stopped.wait(self._settings.api_respawn.total_seconds())
            with self._lock:
                if self._stopping:
                    return
                self._close_control(slot)
                os.close(slot.ready)
                slot = self._slots[index] = self._spawn(index)
            try:
                self._await_ready(index, slot)
            except NotServing as e:
                with self._lock:
                    if self._stopping:
                        return
                failures += 1
                self._tracer.event('api_process.not_serving', index=index, error=str(e), failures=failures)
                if failures >= self._settings.api_respawns:
                    self._failed(f'{e}, and {failures} in a row')
                    return
                continue
            failures = 0


def child() -> NoReturn:
    """An API process: serve the API on the sockets that the daemon handed on, until its control pipe closes.

    It writes ``ready`` on its ready pipe once it serves, or why it cannot before it exits non-zero.
    """
    from piloop.daemon.served import serving  # here: the daemon imports this module, and the API's imports are heavy
    from piloop.settings import settings

    unix, network, control, ready = os.environ.pop(FDS_ENV).split(',')
    index = int(os.environ.pop(INDEX_ENV))
    expected = os.environ.pop(DIGEST_ENV)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)  # the daemon stops it: by its pipe, once its jobs stopped
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    local = socket.socket(fileno=int(unix))
    remote = None if network == '-' else socket.socket(fileno=int(network))
    with ExitStack() as stack:
        try:
            if digest(settings) != expected:
                raise NotServing(
                    "its code or its settings are not the daemon's: piloop or config.toml changed since the daemon"
                    ' started. Restart it (piloop restart)'
                )
            open_files()
            stack.enter_context(serving(local, remote, index))
        except BaseException as e:  # the daemon reads why: it does not start, or counts a failure
            os.write(int(ready), f'{type(e).__name__}: {e}\n'.encode())
            raise
        os.write(int(ready), READY)
        select.select([int(control)], [], [])
    sys.exit(0)
