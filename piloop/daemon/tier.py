"""The processes that serve the API beside the daemon (``[daemon] api_processes``): one leader, N API processes.

The daemon binds the home's Unix socket and the network's listener, then starts each API process with both sockets and
the read end of a pipe (``ApiProcesses``). Each API process serves the whole API on the sockets that it inherited: they
share one accept queue, so the one that is idle accepts first. It reads only the database: its own connections, its own
doorbell, its own relay template (``Served``). It ends when the daemon asks (SIGTERM), or when the pipe closes because
the daemon died. The daemon starts a new one in place of one that exits while it serves.

After gunicorn's arbiter: the master binds, the workers inherit the socket, and the master starts a worker in place of
one that dies.
"""

import contextlib
import os
import select
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

from piloop.daemon.serve import open_files
from piloop.net.server import GRACE_SECONDS
from piloop.otel import Tracer
from piloop.proc import child_env

CHILD_ARGV = (sys.executable, '-P', '-c', 'from piloop.daemon.tier import child; child()')
"""An API process's command line. It imports piloop fresh: nothing of the daemon's memory comes with it."""
SOCKETS_ENV = 'PILOOP_API_SOCKETS'
"""The descriptors that an API process inherits: the Unix socket, the network's (or ``-``), and the daemon's pipe."""
RESPAWN_SECONDS = 1.0
"""How long the daemon waits before it starts an API process in place of one that exited: a crash at start does not
spin."""


@dataclass
class _Slot:
    """One API process, and the write end of its pipe: closing it, or the daemon's death, ends the process."""

    process: subprocess.Popen[bytes]
    pipe: int


class ApiProcesses:
    """``count`` API processes on ``sockets``, from ``start`` until ``close``: what the daemon serves the API with.

    It stops and closes as a ``Server`` does, so a daemon stops its servers alike, whichever serve.
    """

    def __init__(self, count: int, sockets: Sequence[socket.socket | None], home: Path, tracer: Tracer) -> None:
        """``sockets``: the home's Unix socket, then the network's, or None when the daemon listens on no network.

        They are its own from here: ``close`` closes them, once no API process holds them.
        """
        self._count = count
        self._sockets = [s for s in sockets if s is not None]
        self._fds = [None if s is None else s.fileno() for s in sockets]
        self._home = home
        self._tracer = tracer
        self._lock = threading.Lock()
        self._slots: dict[int, _Slot] = {}
        self._stopping = threading.Event()
        self._watchers: list[threading.Thread] = []

    def start(self) -> None:
        """Start each API process, and a thread that starts a new one in place of one that exits."""
        for index in range(self._count):
            with self._lock:
                self._slots[index] = self._spawn(index)
            watcher = threading.Thread(target=self._watch, args=(index,), name=f'api-{index}', daemon=True)
            watcher.start()
            self._watchers.append(watcher)

    def pids(self) -> list[int]:
        """The API processes that run now."""
        with self._lock:
            return [slot.process.pid for slot in self._slots.values()]

    def stop(self) -> None:
        """Start to ``close``, and return at once: each API process ends its long-polls and lets its requests end."""
        self._stopping.set()
        with self._lock:
            slots = list(self._slots.values())
        for slot in slots:
            with contextlib.suppress(ProcessLookupError):
                slot.process.terminate()

    def close(self, grace: float = GRACE_SECONDS) -> None:
        """``stop``, then wait ``grace`` seconds for the API processes to exit, kill the rest, and close the sockets."""
        self.stop()
        deadline = time.monotonic() + grace
        with self._lock:
            slots = list(self._slots.values())
        for slot in slots:
            try:
                slot.process.wait(max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                slot.process.kill()
                slot.process.wait()
            os.close(slot.pipe)
        for watcher in self._watchers:
            watcher.join()
        for each in self._sockets:
            each.close()

    def _spawn(self, index: int) -> _Slot:
        theirs, ours = os.pipe()
        fds = [*(fd for fd in self._fds if fd is not None), theirs]
        named = ','.join('-' if fd is None else str(fd) for fd in [*self._fds, theirs])
        try:
            process = subprocess.Popen(
                CHILD_ARGV,
                pass_fds=fds,
                env=child_env() | {'PILOOP_HOME': str(self._home), SOCKETS_ENV: named},
                stdin=subprocess.DEVNULL,
            )
        finally:
            os.close(theirs)
        self._tracer.event('api_process.started', index=index, pid=process.pid)
        return _Slot(process, ours)

    def _watch(self, index: int) -> None:
        """Wait for the API process ``index`` to exit, then start a new one in its place, until ``stop``."""
        while True:
            with self._lock:
                slot = self._slots[index]
            code = slot.process.wait()
            if self._stopping.is_set():
                return
            self._tracer.event('api_process.exited', index=index, pid=slot.process.pid, code=code)
            if self._stopping.wait(RESPAWN_SECONDS):
                return
            with self._lock:
                os.close(slot.pipe)
                self._slots[index] = self._spawn(index)


def child() -> NoReturn:
    """An API process: serve the API on the sockets that the daemon handed on, until SIGTERM or the daemon's death.

    It opens the home as a client does, and reads which daemon answers from the current term. It exits 0 after a stop,
    and non-zero when it could not serve: the daemon then starts another.
    """
    from piloop.daemon.served import serving  # here: the daemon imports this module, and the API's imports are heavy

    unix, network, pipe = os.environ.pop(SOCKETS_ENV).split(',')
    local = socket.socket(fileno=int(unix))
    remote = None if network == '-' else socket.socket(fileno=int(network))
    wake_r, wake_w = os.pipe()
    os.set_blocking(wake_w, False)
    signal.set_wakeup_fd(wake_w)
    signal.signal(signal.SIGTERM, lambda *_: None)  # the wakeup fd ends the wait below
    signal.signal(signal.SIGINT, signal.SIG_IGN)  # a Ctrl-C in the daemon's terminal: the daemon stops it
    open_files()
    with serving(local, remote):
        select.select([int(pipe), wake_r], [], [])
    sys.exit(0)
