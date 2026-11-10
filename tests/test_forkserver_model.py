"""Paths of ``tests/models/forkserver.tla``, each driven over the real template and its children (``Forks``).

Each call is a ``piloop sql`` that inserts a row into ``ran``, so the rows count the starts of its command: the
model's ``runs``. A test names the path of the model that it drives, step by step.
"""

import os
import select
import signal
import socket
import sqlite3
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import closing
from pathlib import Path

import pytest

from piloop.db.session import Database
from piloop.plugins.executor.kit import forks as forks_module
from piloop.plugins.executor.kit.forks import Forks
from piloop.plugins.executor.workers import FORKED
from piloop.proc import child_env

pytestmark = pytest.mark.skipif(not FORKED, reason='the server forks relayed calls on Linux only (workers.FORKED)')

RUN = ['sql', 'INSERT INTO ran (x) VALUES (1)']
ENDLESS = ['sql', 'INSERT INTO ran WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) SELECT x FROM c']


@pytest.fixture
def home(tmp_path: Path, db: Database) -> Path:
    with closing(sqlite3.connect(tmp_path / 'piloop.db')) as con:
        con.execute('CREATE TABLE ran (x INTEGER)')
        con.commit()
    return tmp_path


@pytest.fixture
def forks(home: Path) -> Iterator[Forks]:
    forks = Forks(home, 2)
    try:
        yield forks
    finally:
        forks.close()


def ran(home: Path) -> int:
    """How many times a call's command started and committed: the rows of ``ran``."""
    with closing(sqlite3.connect(home / 'piloop.db')) as con:
        return con.execute('SELECT count(*) FROM ran').fetchone()[0]


def call(forks: Forks, home: Path, argv: list[str], timeout: float = 60) -> subprocess.CompletedProcess[str]:
    return forks(argv, cwd=home, env=child_env() | {'PILOOP_HOME': str(home)}, timeout=timeout, input=None)


def alive(group: int) -> bool:
    try:
        os.killpg(group, 0)
    except ProcessLookupError:
        return False
    return True


def within(holds: Callable[[], bool], seconds: float = 10) -> bool:
    """Whether ``holds`` comes to hold within ``seconds``."""
    end = time.monotonic() + seconds
    while not holds():
        if time.monotonic() > end:
            return False
        time.sleep(0.05)
    return True


def template(forks: Forks) -> subprocess.Popen[bytes]:
    assert forks._template is not None, 'a call started it'
    return forks._template[0]


def test_start_send_fork_child_start_started_child_exit_finish_runs_the_call_once(forks: Forks, home: Path) -> None:
    done = call(forks, home, RUN)
    assert done.returncode == 0 and ran(home) == 1


def test_a_child_that_started_and_ends_with_no_code_is_answered_1_and_never_runs_again(
    forks: Forks, home: Path
) -> None:
    """Path ``start_send, fork, child_start, started, child_exit, finish``, with no code: what the model's
    ``forkserver_no_started_check.cfg`` turns into a second run."""
    env = child_env() | {'PILOOP_HOME': str(home), 'PILOOP_DEBUG': 'maybe'}  # its settings fail after its pid went
    done = forks(RUN, cwd=home, env=env, timeout=60, input=None)
    assert done.returncode == 1 and 'ValidationError' in done.stderr
    assert ran(home) == 0, 'no fallback ran it'


def test_a_call_queued_in_a_template_that_dies_runs_once_as_a_process_of_its_own(forks: Forks, home: Path) -> None:
    """Path ``send, template_dies, unforked, fallback``."""
    assert call(forks, home, ['sql', 'SELECT 1']).returncode == 0
    stalled = template(forks)
    os.kill(stalled.pid, signal.SIGSTOP)  # it takes the next request and forks nothing
    found: list[subprocess.CompletedProcess[str]] = []
    queued = threading.Thread(target=lambda: found.append(call(forks, home, RUN)))
    queued.start()
    time.sleep(0.3)
    stalled.kill()
    queued.join(60)
    assert found[0].returncode == 0 and ran(home) == 1


def test_a_call_that_runs_past_its_deadline_is_killed_with_its_group_and_never_runs_again(
    forks: Forks, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Path ``start_send, fork, child_start, started, timeout_after``."""
    pids: list[int | None] = []
    received = forks_module._received

    def recorded(sock: socket.socket, deadline: float) -> int | None:
        got = received(sock, deadline)
        pids.append(got)
        return got

    monkeypatch.setattr(forks_module, '_received', recorded)
    with pytest.raises(subprocess.TimeoutExpired):
        call(forks, home, ENDLESS, timeout=2)
    child = pids[0]
    assert child is not None
    assert within(lambda: not alive(child)), 'its group is dead'  # the kernel reaps it: the template ignores SIGCHLD
    assert ran(home) == 0


def test_a_call_stopped_before_its_child_started_never_runs(
    forks: Forks, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Path ``start_send, fork, timeout_before, child_start``: the deadline passes as the forked child sends its pid.
    ``_end`` kills the template, not the child. The child waits for the server's ``GO``, which never comes: once the
    call's socket closes, it ends without running the command."""
    received = forks_module._received

    def deadline_as_the_pid_comes(sock: socket.socket, deadline: float) -> int | None:
        select.select([sock], [], [], 60)  # the child forked and sends its pid: the deadline passes now
        monkeypatch.setattr(forks_module, '_received', received)
        raise TimeoutError

    assert call(forks, home, ['sql', 'SELECT 1']).returncode == 0  # the template runs
    monkeypatch.setattr(forks_module, '_received', deadline_as_the_pid_comes)
    with pytest.raises(subprocess.TimeoutExpired):
        call(forks, home, RUN)
    assert not within(lambda: ran(home) > 0, seconds=3), 'a stopped call ran'
