import os
import stat
import time
from pathlib import Path

import pytest
from dirty_equals import IsPositive
from inline_snapshot import snapshot

from piloop.runtime import Handle, NotSteerable, WorkerSpec, WorkerStatus
from piloop.runtime.subprocess import SubprocessRuntime, proc_start_ticks


def script(tmp_path: Path, body: str) -> Path:
    path = tmp_path / 'worker.sh'
    path.write_text(f'#!/bin/sh\n{body}\n')
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def spec(
    tmp_path: Path, executable: Path, *args: str, env: dict[str, str] | None = None, pattern: str | None = None
) -> WorkerSpec:
    return WorkerSpec(
        name='w1',
        label='w1',
        executable=str(executable),
        args=list(args),
        prompt='the brief',
        cwd=tmp_path,
        log_dir=tmp_path / 'logs',
        env=env or {},
        session_id_pattern=pattern,
    )


def test_runs_to_completion_and_captures_output(tmp_path: Path) -> None:
    rt = SubprocessRuntime()
    exe = script(tmp_path, 'echo "prompt=$1 var=$PILOOP_TEST"; echo oops >&2; exit 3')
    handle = rt.start(spec(tmp_path, exe, 'the brief', env={'PILOOP_TEST': 'yes'}))
    assert handle.runtime == 'subprocess'
    assert handle.data == {
        'pid': IsPositive(),
        'pgid': handle.data['pid'],
        'start_ticks': IsPositive(),
        'started_at': IsPositive(),
        'log_dir': str(tmp_path / 'logs'),
        'session_id_pattern': None,
    }
    outcome = rt.wait(handle, timeout=10)
    assert (outcome.status, outcome.exit_code, outcome.error) == (WorkerStatus.EXITED, 3, 'worker exit 3')
    assert outcome.seconds == IsPositive()
    assert rt.status(handle) is WorkerStatus.EXITED
    assert rt.read(handle) == snapshot('prompt=the brief var=yes')
    assert (tmp_path / 'logs' / 'worker.stderr').read_text() == 'oops\n'


def test_timeout_kills_the_whole_process_group(tmp_path: Path) -> None:
    rt = SubprocessRuntime()
    exe = script(tmp_path, 'sleep 30 &\necho $! > child.pid\nwait')
    handle = rt.start(spec(tmp_path, exe))
    assert rt.status(handle) is WorkerStatus.WORKING
    outcome = rt.wait(handle, timeout=0.5)
    assert (outcome.status, outcome.exit_code, outcome.error) == (
        WorkerStatus.TIMED_OUT,
        None,
        'worker timeout after 0.5s',
    )
    child = int((tmp_path / 'child.pid').read_text())
    time.sleep(0.2)
    assert proc_start_ticks(child) is None, 'the sleeping grandchild must die with the group'


def test_session_id_is_read_from_stdout_while_the_process_still_runs(tmp_path: Path) -> None:
    rt = SubprocessRuntime()
    exe = script(tmp_path, 'sleep 0.3; echo \'{"type":"thread.started","thread_id":"abc-123"}\'; sleep 5')
    handle = rt.start(spec(tmp_path, exe, pattern=r'"thread_id":"([a-z0-9-]+)"'))
    started = time.monotonic()
    assert rt.session_id(handle) == 'abc-123'
    assert 0.2 < time.monotonic() - started < 3, 'returned as soon as the line appeared'
    rt.kill(handle)
    silent = rt.start(spec(tmp_path, script(tmp_path, 'echo nothing of note'), pattern=r'"thread_id":"([a-z0-9-]+)"'))
    started = time.monotonic()
    assert rt.session_id(silent) is None
    assert time.monotonic() - started < 3, 'a process that ended without the line is not waited for'
    rt.wait(silent, timeout=5)
    unpatterned = rt.start(spec(tmp_path, script(tmp_path, 'echo thread_id')))
    assert rt.session_id(unpatterned) is None, 'no pattern, no id'
    rt.wait(unpatterned, timeout=5)


def test_prompt_is_not_supported(tmp_path: Path) -> None:
    rt = SubprocessRuntime()
    handle = rt.start(spec(tmp_path, script(tmp_path, 'sleep 5')))
    with pytest.raises(NotSteerable, match='cannot be steered'):
        rt.prompt(handle, 'hi')
    rt.kill(handle)
    rt.kill(handle)
    assert rt.status(handle) is WorkerStatus.EXITED


def test_no_jobs_are_reported(tmp_path: Path) -> None:
    """A one-shot process is not watched for what it leaves running, so no worker of it is ever ``silent``."""
    rt = SubprocessRuntime()
    handle = rt.start(spec(tmp_path, script(tmp_path, 'sleep 5 &\nwait')))
    assert rt.jobs(handle) == []
    rt.kill(handle)


def test_rehydrated_handle_is_observable_without_an_exit_code(tmp_path: Path) -> None:
    starter = SubprocessRuntime()
    handle = starter.start(spec(tmp_path, script(tmp_path, 'sleep 5')))
    other = SubprocessRuntime()  # another process would rebuild the handle from the database
    rehydrated = Handle(handle.runtime, dict(handle.data))
    assert other.status(rehydrated) is WorkerStatus.WORKING
    other.kill(rehydrated)
    starter.wait(handle, timeout=5)
    assert other.status(rehydrated) is WorkerStatus.EXITED
    assert other.wait(rehydrated, timeout=1).exit_code is None
    assert other.read(Handle('subprocess', {**handle.data, 'log_dir': str(tmp_path / 'nope')})) == ''


def test_reused_pid_is_not_mistaken_for_the_worker(tmp_path: Path) -> None:
    rt = SubprocessRuntime()
    handle = rt.start(spec(tmp_path, script(tmp_path, 'true')))
    rt.wait(handle, timeout=5)
    impostor = Handle('subprocess', {**handle.data, 'pid': os.getpid(), 'start_ticks': -1})
    assert SubprocessRuntime().status(impostor) is WorkerStatus.EXITED


def test_kill_tolerates_a_vanished_group(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = SubprocessRuntime()
    handle = rt.start(spec(tmp_path, script(tmp_path, 'sleep 5')))

    def gone(*_: object) -> None:
        raise ProcessLookupError

    monkeypatch.setattr(os, 'killpg', gone)
    rt.kill(handle)
    monkeypatch.undo()
    rt.kill(handle)


def test_proc_start_ticks_handles_spaces_in_comm(tmp_path: Path) -> None:
    assert proc_start_ticks(os.getpid()) == IsPositive()
    assert proc_start_ticks(2**22 + 12345) is None
