"""Reject unbound/malformed final-sync receipts; real-socket success/failure is in test_litestream."""

import json
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from pytest_mock import MockerFixture

from piloop.plugins.leader.contract import Unreachable
from piloop.plugins.leader.kit.litestream import Litestream, _Child


@pytest.fixture
def child(tmp_path: Path) -> Iterator[_Child]:
    executable = tmp_path / 'litestream'
    executable.write_text(f'#!{sys.executable}\nimport signal\nsignal.pause()\n')
    executable.chmod(0o700)
    database = tmp_path / 'home.db'
    database.touch()
    running = Litestream(str(executable), None).replicate(database, 'file:///unused')
    assert isinstance(running, _Child)
    running._socket.touch()  # these unit tests replace the control command's result
    try:
        yield running
    finally:
        running.stop()


@pytest.mark.parametrize(
    'invalid',
    [
        'not JSON',
        {'db_path': '/wrong.db'},
        {'txid': 2},
        {'replica_txid': None},
        {'txid': 0, 'replica_txid': 0},
        {'txid': -1},
        {'txid': True},
        {'txid': '1'},
        {'txid': 2**64},
        {'duration_ms': -1},
    ],
)
def test_a_matching_database_generation_and_remote_receipt_are_required(
    child: _Child, mocker: MockerFixture, invalid: str | dict[str, object]
) -> None:
    valid = {'db_path': str(child._database), 'txid': 1, 'replica_txid': 1, 'duration_ms': 0}
    response = invalid if isinstance(invalid, str) else json.dumps(valid | invalid)
    mocker.patch('subprocess.run', return_value=subprocess.CompletedProcess([], 0, response, ''))
    with pytest.raises(Unreachable, match='final sync'):
        child.sync()


@pytest.mark.parametrize('failure', ['timeout', 'lost reply', 'replaced inode', 'exited', 'missing socket'])
def test_no_receipt_is_inferred_from_failure(child: _Child, mocker: MockerFixture, failure: str) -> None:
    def respond(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        if failure == 'timeout':
            raise subprocess.TimeoutExpired('sync', 6)
        if failure in ('lost reply', 'missing socket'):
            return subprocess.CompletedProcess([], 1, '', failure)
        if failure == 'replaced inode':
            replacement = child._database.with_suffix('.new')
            replacement.touch()
            replacement.replace(child._database)
        if failure == 'exited':
            child.process.terminate()
            child.process.wait(timeout=5)
        return subprocess.CompletedProcess(
            [],
            0,
            json.dumps(
                {
                    'db_path': str(child._database),
                    'txid': 1,
                    'replica_txid': 1,
                    'duration_ms': 0,
                }
            ),
            '',
        )

    mocker.patch('subprocess.run', side_effect=respond)
    with pytest.raises(Unreachable, match='final sync'):
        child.sync()


def test_a_blocked_log_relay_does_not_hold_cleanup(tmp_path: Path, mocker: MockerFixture) -> None:
    entered, release = threading.Event(), threading.Event()
    stderr = sys.stderr

    class Blocked:
        def write(self, value: str) -> int:
            if threading.current_thread().name == 'litestream-log':
                entered.set()
                release.wait(5)  # bounded fail-safe so the regression cannot leak a blocked thread
                return len(value)
            return stderr.write(value)

        def flush(self) -> None:
            stderr.flush()

    executable, database = tmp_path / 'litestream', tmp_path / 'home.db'
    executable.write_text(
        f'#!{sys.executable}\nimport sys, signal\nprint("started", file=sys.stderr, flush=True)\nsignal.pause()\n'
    )
    executable.chmod(0o700)
    database.touch()
    mocker.patch('sys.stderr', Blocked())
    running = Litestream(str(executable), None).replicate(database, 'file:///unused')
    assert isinstance(running, _Child)
    try:
        assert entered.wait(5), 'the real log relay reached the blocked stderr sink'
        before = time.monotonic()
        running.stop()
        assert time.monotonic() - before < 3
        assert running.process.poll() is not None
        assert running._log.is_alive(), 'log delivery remains blocked without holding ownership cleanup'
    finally:
        release.set()
        running._log.join(timeout=5)
        running.stop()


def test_forced_cleanup_is_an_explicit_failure(child: _Child, mocker: MockerFixture) -> None:
    mocker.patch('piloop.plugins.leader.kit.litestream.STOP_SECONDS', 0)
    mocker.patch.object(child.process, 'terminate')  # simulate a process that does not react to TERM
    with pytest.raises(Unreachable, match='did not stop within its grace'):
        child.stop()
    assert child.process.poll() is not None


def test_a_child_that_cannot_start_leaves_no_private_config(tmp_path: Path, mocker: MockerFixture) -> None:
    database = tmp_path / 'home.db'
    database.touch()
    mocker.patch('tempfile.tempdir', str(tmp_path))
    with pytest.raises(FileNotFoundError):
        Litestream(str(tmp_path / 'missing-executable'), None).replicate(database, 'file:///unused')
    assert not list(tmp_path.glob('piloop-replica-*'))


def test_a_child_that_cannot_be_reaped_is_not_a_success(child: _Child, mocker: MockerFixture) -> None:
    waiting = mocker.patch.object(
        child.process,
        'wait',
        side_effect=[
            subprocess.TimeoutExpired('term', 32),
            subprocess.TimeoutExpired('kill', 1),
        ],
    )
    try:
        with pytest.raises(Unreachable, match='could not be reaped') as raised:
            child.stop()
        assert isinstance(raised.value.__cause__, subprocess.TimeoutExpired)
    finally:
        mocker.stop(waiting)
        child.process.wait(timeout=5)


@pytest.mark.parametrize('socket_present', [False, True])
def test_control_startup_and_sync_share_one_budget(child: _Child, mocker: MockerFixture, socket_present: bool) -> None:
    class Clock:
        now = 0.0

        def monotonic(self) -> float:
            before = self.now
            if socket_present:
                self.now += 6  # scheduler pause after readiness consumes the same deadline
            return before

        def sleep(self, seconds: float) -> None:
            self.now += seconds

    if not socket_present:
        child._socket.unlink()
    mocker.patch('piloop.plugins.leader.kit.litestream.time', Clock())
    request = mocker.patch('subprocess.run')
    with pytest.raises(Unreachable, match='control'):
        child.sync()
    request.assert_not_called()
