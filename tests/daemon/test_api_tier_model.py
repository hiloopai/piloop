"""Paths of the API tier's TLA+ model (``tests/models/api_tier.tla``), driven over real API processes.

The model's actions here: ``start``, ``child_ready``, ``child_fail`` (the child exits), ``w_exit`` and ``w_timeout``
(its watcher waits out ``api_respawn``), ``stop`` (``stop``, then the ``stop`` that ``close`` makes again),
``snapshot`` and ``reaped`` (``close`` waits for each process), ``w_spawn``. Each test asserts the model's rules on the
real objects: each pipe closed once (``NoDoubleClose``, ``ClosedOnce``) and no child left (``NoChildOutlivesStop``).
``StartNeedsReady`` is ``test_tier.py``'s ``test_the_start_fails_with_why_the_first_api_process_does_not_serve``.
"""

import os
import sys
import threading
from collections import Counter
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from piloop.daemon import tier as tier_module
from piloop.daemon.tier import ApiProcesses
from piloop.net.server import local_listener
from piloop.otel import Tracer
from piloop.settings import DaemonSettings, Settings
from tests.conftest import wait_until
from tests.daemon.test_tier import FAKE, alive

RESPAWN = timedelta(seconds=0.05)


def processes_running(code: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ApiProcesses:
    """One API process that runs ``code`` after ``FAKE`` in place of the API, on a Unix socket of its own."""
    monkeypatch.setattr(tier_module, 'CHILD_ARGV', (sys.executable, '-S', '-c', FAKE + code))
    settings = DaemonSettings.model_validate({'api_processes': 1, 'api_start': 10, 'api_respawn': RESPAWN})
    cfg = Settings(home=tmp_path).model_copy(update={'daemon': settings})
    return ApiProcesses(cfg, [local_listener(cfg.socket_file).socket, None], Tracer(None), lambda why: None)


def counted_closes(monkeypatch: pytest.MonkeyPatch, after: Callable[[int], None] = lambda fd: None) -> Counter[object]:
    """Count each ``os.close`` of the tier module by what it closed: the descriptor and its pipe's inode, as a number
    is used again, or ``'closed'`` for a descriptor that was closed already. ``after`` runs after each."""
    closes: Counter[object] = Counter()

    def close(fd: int) -> None:
        try:
            closed: object = (fd, os.fstat(fd).st_ino)
        except OSError:
            closed = 'closed'
        try:
            os.close(fd)
        finally:
            closes[closed] += 1
            after(fd)

    monkeypatch.setattr(tier_module, 'os', SimpleNamespace(**{**vars(os), 'close': close}))
    return closes


def pipes(processes: ApiProcesses) -> list[tuple[int, int]]:
    """The daemon's ends of the pipes of each API process that runs now, as ``counted_closes`` names them."""
    return [
        (fd, os.fstat(fd).st_ino)
        for slot in processes._slots.values()
        for fd in (slot.control, slot.ready)
        if fd is not None
    ]


def test_a_stop_that_overtakes_a_respawn_closes_each_pipe_once_and_starts_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TLC's path that broke ``NoDoubleClose`` before the fix: start, child_ready, child_fail, w_exit, w_timeout, stop,
    stop, snapshot, reaped, w_spawn. The stop comes once the respawn wait ran out, before the watcher takes the lock."""
    processes = processes_running("os.write(ready, b'ready\\n')", tmp_path, monkeypatch)  # it serves, then exits
    paused, go = threading.Event(), threading.Event()

    class RanOut(threading.Event):
        def wait(self, timeout: float | None = None) -> bool:
            if timeout == RESPAWN.total_seconds() and not go.is_set():
                paused.set()
                go.wait(30)
                return False
            return super().wait(timeout)

    processes._stopped = RanOut()
    processes.start()
    [pid] = processes.pids()
    old = pipes(processes)
    closes = counted_closes(monkeypatch)
    wait_until(paused.is_set, 'its process exited and its watcher waits to start another')
    processes.stop()
    go.set()
    processes.close()
    assert processes.pids() == [pid] and not alive(pid), 'it started none, and none is left'
    assert all(closes[p] == 1 for p in old) and 'closed' not in closes, f'each pipe closed once: {closes}'


def test_a_respawned_api_process_and_each_pipe_end_once_at_the_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """start, child_ready, child_fail, w_exit, w_timeout, w_spawn, child_ready, stop, stop, snapshot, reaped."""
    marker = tmp_path / 'failed-once'
    code = (
        f'from pathlib import Path; m = Path({str(marker)!r}); first = not m.exists()\n'
        'if first: m.write_text(str(os.getpid()))\n'
        "os.write(ready, b'ready\\n')\n"
        'if first: sys.exit(1)\n'
        'os.read(control, 1)\n'
    )
    processes = processes_running(code, tmp_path, monkeypatch)
    closes = counted_closes(monkeypatch)
    processes.start()
    first = int(marker.read_text())
    old = pipes(processes)
    wait_until(lambda: processes.pids() != [first], 'a new API process in place of the one that exited')
    [second] = processes.pids()
    new = pipes(processes)
    processes.close()
    assert not alive(first) and not alive(second), 'no API process outlives the stop'
    assert all(closes[p] == 1 for p in old + new) and 'closed' not in closes, f'each pipe closed once: {closes}'
