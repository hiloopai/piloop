import json
import os
import signal
import stat
import subprocess
import sys
import threading
import time
from dataclasses import fields
from pathlib import Path

import psutil
import pytest
from dirty_equals import IsPositive, IsStr

from piloop.plugins.runtime import subprocess as subprocess_runtime
from piloop.plugins.runtime.contract import (
    AgentSpec,
    AgentStart,
    Deadline,
    ExitStatus,
    Handle,
    NotSteerable,
    locked,
    put,
    take,
)
from piloop.plugins.runtime.kit.turns import Turns
from piloop.plugins.runtime.subprocess import Subprocess
from piloop.proc import alive, started


def script(tmp_path: Path, body: str) -> Path:
    path = tmp_path / 'agent_session.sh'
    path.write_text(f'#!/bin/sh\n{body}\n')
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def spec(
    tmp_path: Path,
    executable: Path,
    *args: str,
    env: dict[str, str] | None = None,
    pattern: str | None = None,
    resume: list[str] | None = None,
) -> AgentSpec:
    return AgentSpec(
        executable=str(executable),
        args=list(args),
        cwd=tmp_path,
        log_dir=tmp_path / 'logs',
        env=env or {},
        session_id_pattern=pattern,
        resume_args=resume or [],
    )


def test_runs_to_completion_and_captures_output(tmp_path: Path) -> None:
    rt = Subprocess().open('subprocess')
    exe = script(tmp_path, 'echo "prompt=$1 var=$PILOOP_TEST token=$PILOOP_PROCESS_TAG"; echo oops >&2; exit 3')
    handle = rt.start(spec(tmp_path, exe, 'the brief', env={'PILOOP_TEST': 'yes'}))
    assert handle.runtime == 'subprocess'
    assert handle.data == {
        'pid': IsPositive(),
        'pgid': handle.data['pid'],
        'start_time': IsPositive(),
        'started_at': IsPositive(),
        'log_dir': str(tmp_path / 'logs'),
        'session_id_pattern': None,
        'token': IsStr(regex='[0-9a-f]{32}'),
    }
    outcome = rt.wait(handle, Deadline.after(10))
    assert (outcome.status, outcome.exit_code, outcome.error) == (ExitStatus.EXITED, 3, 'agent exit 3')
    assert outcome.seconds == IsPositive()
    assert not rt._alive(handle)
    assert rt.read(handle) == f'prompt=the brief var=yes token={handle.data["token"]}'
    assert (tmp_path / 'logs' / 'agent_session.stderr').read_text() == 'oops\n'


def test_timeout_kills_the_whole_process_group(tmp_path: Path) -> None:
    rt = Subprocess().open('subprocess')
    exe = script(tmp_path, 'sleep 30 &\necho $! > child.pid\nwait')
    handle = rt.start(spec(tmp_path, exe))
    assert rt._alive(handle)
    outcome = rt.wait(handle, Deadline.after(0.5))
    assert (outcome.status, outcome.exit_code, outcome.error) == (
        ExitStatus.TIMED_OUT,
        None,
        'agent timeout after 0.5s',
    )
    child = int((tmp_path / 'child.pid').read_text())
    time.sleep(0.2)
    assert started(child) is None, 'the sleeping grandchild must die with the group'


def test_a_deadline_that_an_extension_moves_while_the_agent_runs_ends_it_at_the_new_time(tmp_path: Path) -> None:
    rt = Subprocess().open('subprocess')
    handle = rt.start(spec(tmp_path, script(tmp_path, 'sleep 30')))
    extended = threading.Event()
    threading.Timer(0.1, extended.set).start()
    outcome = rt.wait(handle, Deadline.after(0.3, lambda: 0.5 if extended.is_set() else 0.0))
    assert (outcome.status, outcome.error) == (ExitStatus.TIMED_OUT, 'agent timeout after 0.8s')
    assert outcome.seconds >= 0.8, 'not at the first deadline'


ESCAPE = f"""
{sys.executable} -c 'import os, time; os.setsid(); open("escaped.pid", "w").write(str(os.getpid())); time.sleep(60)' &
while [ ! -s escaped.pid ]; do sleep 0.05; done
"""


def escaped(tmp_path: Path) -> tuple[int, float]:
    """The pid and start time of the child that left the agent's session, as Claude Code's Bash calls do."""
    path, deadline = tmp_path / 'escaped.pid', time.monotonic() + 10
    while not (path.is_file() and path.read_text()) and time.monotonic() < deadline:
        time.sleep(0.05)
    pid = int(path.read_text())
    start = started(pid)
    assert start is not None
    return pid, start


def gone(pid: int, start: float) -> bool:
    deadline = time.monotonic() + 5
    while alive(pid, start) and time.monotonic() < deadline:
        time.sleep(0.05)
    return not alive(pid, start)


def test_a_timeout_kills_what_the_agent_started_in_a_session_of_its_own(tmp_path: Path) -> None:
    """A Claude Code Bash call runs in its own session, outside the agent's group: the builds it ran outlived the
    agent by an hour. Every process of the agent carries its token, and the kill ends the tagged ones."""
    rt = Subprocess().open('subprocess')
    handle = rt.start(spec(tmp_path, script(tmp_path, f'{ESCAPE}sleep 30')))
    pid, start = escaped(tmp_path)
    try:
        assert os.getsid(pid) != handle.data['pid'], 'the child left the group that killpg reaches'
        assert rt.wait(handle, Deadline.after(0.5)).status is ExitStatus.TIMED_OUT
        assert gone(pid, start), 'the escaped child dies with the agent'
    finally:
        if alive(pid, start):
            os.kill(pid, signal.SIGKILL)


def test_kill_ends_an_escaped_child_after_the_agent_itself_exited(tmp_path: Path) -> None:
    """The launcher kills every agent once its wait returns. An agent that exited first leaves its jobs behind."""
    rt = Subprocess().open('subprocess')
    handle = rt.start(spec(tmp_path, script(tmp_path, ESCAPE)))
    pid, start = escaped(tmp_path)
    try:
        assert rt.wait(handle, Deadline.after(10)).status is ExitStatus.EXITED
        assert alive(pid, start), 'the agent exited, its job did not'
        Subprocess().open('subprocess').kill(Handle(handle.runtime, dict(handle.data)))
        assert gone(pid, start), 'any instance, from the handle in the database'
    finally:
        if alive(pid, start):
            os.kill(pid, signal.SIGKILL)


def test_session_id_is_read_from_stdout_while_the_process_still_runs(tmp_path: Path) -> None:
    rt = Subprocess().open('subprocess')
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
    rt.wait(silent, Deadline.after(5))
    unpatterned = rt.start(spec(tmp_path, script(tmp_path, 'echo thread_id')))
    assert rt.session_id(unpatterned) is None, 'no pattern, no id'
    rt.wait(unpatterned, Deadline.after(5))


def test_a_harness_without_resume_arguments_takes_no_message(tmp_path: Path) -> None:
    rt = Subprocess().open('subprocess')
    handle = rt.start(spec(tmp_path, script(tmp_path, 'sleep 5')))
    with pytest.raises(NotSteerable, match='its harness has no resume arguments, or an older piloop'):
        rt.prompt(handle, 'hi')
    rt.kill(handle)
    rt.kill(handle)
    assert not rt._alive(handle)


def test_rehydrated_handle_is_observable_without_an_exit_code(tmp_path: Path) -> None:
    starter = Subprocess().open('subprocess')
    handle = starter.start(spec(tmp_path, script(tmp_path, 'sleep 5')))
    other = Subprocess().open('subprocess')  # another process would rebuild the handle from the database
    rehydrated = Handle(handle.runtime, dict(handle.data))
    assert other._alive(rehydrated)
    other.kill(rehydrated)
    starter.wait(handle, Deadline.after(5))
    assert not other._alive(rehydrated)
    assert other.wait(rehydrated, Deadline.after(1)).exit_code is None
    assert other.read(Handle('subprocess', {**handle.data, 'log_dir': str(tmp_path / 'nope')})) == ''


def test_reused_pid_is_not_mistaken_for_the_implementer(tmp_path: Path) -> None:
    rt = Subprocess().open('subprocess')
    handle = rt.start(spec(tmp_path, script(tmp_path, 'true')))
    rt.wait(handle, Deadline.after(5))
    impostor = Handle('subprocess', {**handle.data, 'pid': os.getpid(), 'start_time': -1.0})
    assert not Subprocess().open('subprocess')._alive(impostor)


@pytest.mark.parametrize('error', [ProcessLookupError, PermissionError])
def test_kill_tolerates_a_vanished_group(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: type[OSError]) -> None:
    """The group exits as it is killed: Linux answers ESRCH, macOS EPERM for a group of zombies."""
    rt = Subprocess().open('subprocess')
    handle = rt.start(spec(tmp_path, script(tmp_path, 'sleep 5')))
    killpg = os.killpg

    def gone(pgid: int, signum: int) -> None:
        killpg(pgid, signum)
        while psutil.Process(pgid).status() != psutil.STATUS_ZOMBIE:  # exited, and not reaped yet
            time.sleep(0.01)
        raise error

    monkeypatch.setattr(os, 'killpg', gone)
    rt.kill(handle)
    monkeypatch.undo()
    rt.kill(handle)


def test_started_is_the_start_time_of_a_live_pid_and_none_for_a_gone_one() -> None:
    assert started(os.getpid()) == IsPositive()
    assert started(2**22 + 12345) is None


def test_an_agent_that_exits_at_once_still_starts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """macOS answers ``getpgid`` with ESRCH for a child that already exited; the start must not ask it."""

    def gone(pid: int) -> int:
        raise ProcessLookupError(3, 'No such process')

    monkeypatch.setattr(os, 'getpgid', gone)
    rt = Subprocess().open('subprocess')
    handle = rt.start(spec(tmp_path, script(tmp_path, 'exit 0')))
    assert handle.data['pgid'] == handle.data['pid']
    assert rt.wait(handle, Deadline.after(10)).status is ExitStatus.EXITED


TURNS = """
if [ "$1" = resume ]; then echo "turn 2 of $2: $3" >> turns.log; exit 0; fi
echo '{"thread_id":"t-42"}'
echo "turn 1: $1" >> turns.log
sleep 1
"""


def test_a_message_waits_for_the_turn_to_end_then_resumes_the_session_with_it(tmp_path: Path) -> None:
    """The subprocess runtime queues a message: nothing reaches the turn in progress. When the process exits,
    the harness runs again with ``resume_args``: the same session, the queued messages as its prompt."""
    rt = Subprocess().open('subprocess')
    exe = script(tmp_path, TURNS)
    handle = rt.start(
        spec(
            tmp_path, exe, 'the brief', pattern=r'"thread_id":"([^"]+)"', resume=['resume', '{session_id}', '{prompt}']
        )
    )
    assert handle.data['turns'] == 1, 'a supervisor runs it'
    elsewhere = Subprocess().open('subprocess')  # the scheduler's push and piloop prompt use instances of their own
    elsewhere.prompt(handle, '[piloop] note on run#1 by research#2: stop after the next commit')
    elsewhere.prompt(handle, 'and say so')
    while not (tmp_path / 'turns.log').exists():
        time.sleep(0.05)
    assert 'turn 2' not in (tmp_path / 'turns.log').read_text(), 'the turn in progress gets nothing'
    outcome = rt.wait(handle, Deadline.after(20))
    assert (outcome.status, outcome.error) == (ExitStatus.EXITED, None)
    assert (tmp_path / 'turns.log').read_text() == (
        'turn 1: the brief\nturn 2 of t-42: [piloop] note on run#1 by research#2: stop after the next commit\n'
        'and say so\n'
    ), 'one resumed turn for both messages, one per line'
    assert not (tmp_path / 'logs' / 'queued-messages').exists()
    with pytest.raises(NotSteerable, match='has ended'):
        elsewhere.prompt(handle, 'too late')


def test_a_hook_of_the_harness_takes_the_messages_during_the_turn_and_no_turn_follows(tmp_path: Path) -> None:
    """``PILOOP_MESSAGES`` names the queue: a harness that runs a hook between two tool calls takes them there."""
    rt = Subprocess().open('subprocess')
    go = tmp_path / 'go'
    os.mkfifo(go)
    exe = script(tmp_path, f'read line < {go}; echo "queue=$PILOOP_MESSAGES" > env.txt')
    handle = rt.start(spec(tmp_path, exe, 'brief', resume=['resume', 'id', '{prompt}']))
    rt.prompt(handle, 'first')
    rt.prompt(handle, 'second')
    queue = tmp_path / 'logs' / 'queued-messages'
    assert take(queue) == ['first', 'second']
    assert take(queue) == [], 'taken once'
    go.write_text('\n')
    assert rt.wait(handle, Deadline.after(10)).error is None
    assert (tmp_path / 'env.txt').read_text() == f'queue={queue}\n', 'and no turn of its own follows: it would block'


def test_a_kill_from_any_instance_ends_the_supervisor_and_its_resumed_turn(tmp_path: Path) -> None:
    rt = Subprocess().open('subprocess')
    go = tmp_path / 'go'
    os.mkfifo(go)
    exe = script(tmp_path, f'if [ "$1" = resume ]; then echo $$ > resumed.pid; sleep 30; else read line < {go}; fi')
    handle = rt.start(spec(tmp_path, exe, 'brief', resume=['resume', 'a-minted-id', '{prompt}']))
    rt.prompt(handle, 'more')
    go.write_text('\n')
    pid_file = tmp_path / 'resumed.pid'
    while not (pid_file.exists() and pid_file.read_text().strip()):
        time.sleep(0.05)
    resumed = int(pid_file.read_text())
    elsewhere = Subprocess().open('subprocess')
    assert elsewhere._alive(handle), 'the supervisor leads the agent through all its turns'
    elsewhere.kill(handle)
    assert not elsewhere._alive(handle) and not rt._alive(handle)
    assert rt.wait(handle, Deadline.after(5)).exit_code == -signal.SIGKILL
    assert not psutil.pid_exists(resumed) or psutil.Process(resumed).status() == psutil.STATUS_ZOMBIE


def test_queued_messages_stay_when_the_turn_failed_or_the_session_is_unknown(tmp_path: Path) -> None:
    rt = Subprocess().open('subprocess')
    resume = ['resume', '{session_id}', '{prompt}']
    failed = rt.start(spec(tmp_path, script(tmp_path, 'sleep 0.3; exit 2'), resume=resume))
    rt.prompt(failed, 'x')
    assert rt.wait(failed, Deadline.after(10)).exit_code == 2
    assert (tmp_path / 'logs' / 'queued-messages').read_text() == '"x"\n', 'a failed turn is not resumed'

    (tmp_path / 'logs' / 'queued-messages').unlink()
    unknown = rt.start(spec(tmp_path, script(tmp_path, 'sleep 0.3'), pattern='id=(\\w+)', resume=resume))
    rt.prompt(unknown, 'y')
    assert rt.wait(unknown, Deadline.after(10)).error is None
    assert (tmp_path / 'logs' / 'queued-messages').exists(), 'no session id to resume'

    (tmp_path / 'logs' / 'queued-messages').write_text('\n')
    blank = rt.start(spec(tmp_path, script(tmp_path, 'true'), resume=['again', '{prompt}']))
    assert rt.wait(blank, Deadline.after(10)).error is None, 'an empty queue starts no turn'


def test_a_turn_that_a_signal_ends_ends_its_supervisor_by_the_same_signal(tmp_path: Path) -> None:
    rt = Subprocess().open('subprocess')
    handle = rt.start(spec(tmp_path, script(tmp_path, 'kill -TERM $$'), resume=['resume', 'id', '{prompt}']))
    outcome = rt.wait(handle, Deadline.after(10))
    assert (outcome.exit_code, outcome.error) == (-signal.SIGTERM, f'agent exit {-signal.SIGTERM}')


def test_an_agent_that_a_re_attached_instance_steers_takes_the_message_in_its_next_turn(tmp_path: Path) -> None:
    """After an in-place restart, the new daemon's runtime has no ``Popen`` of the agent, and no spec: its supervisor
    has both, so a message that the new instance queues resumes the session as before the restart."""
    rt = Subprocess().open('subprocess')
    exe = script(tmp_path, TURNS)
    pattern, resume = r'"thread_id":"([^"]+)"', ['resume', '{session_id}', '{prompt}']
    handle = rt.start(spec(tmp_path, exe, 'the brief', pattern=pattern, resume=resume))
    reattached = Subprocess().open('subprocess')
    rehydrated = Handle(handle.runtime, json.loads(json.dumps(handle.data)))
    reattached.prompt(rehydrated, 'after the restart')
    assert reattached.wait(rehydrated, Deadline.after(20)).error is None
    assert (tmp_path / 'turns.log').read_text() == 'turn 1: the brief\nturn 2 of t-42: after the restart\n'
    rt.wait(handle, Deadline.after(10))  # reap it


def test_each_resumed_turn_runs_turn_ones_resume_argv_and_env_with_only_the_prompt_changed(tmp_path: Path) -> None:
    """The harness's model sees the same prefix on every turn, so its prompt cache holds: the supervisor fills only
    ``{prompt}`` and ``{session_id}``, and every turn has its environment, also one that a re-attached instance
    steered."""
    rt = Subprocess().open('subprocess')
    for name in ('go-1', 'go-2'):
        os.mkfifo(tmp_path / name)
    exe = script(
        tmp_path,
        'n=$(( $(ls turn-*.args 2>/dev/null | wc -l) + 1 ))\n'
        'printf "%s\\n" "$@" > turn-$n.args\nenv | sort > turn-$n.env\n'
        'echo "id=s-1"\n'
        '[ -p go-$n ] && read line < go-$n\ntrue',
    )
    pinned = ['exec', 'resume', '--model', 'm', '-c', 'model_reasoning_effort="low"', '-c', 'mcp_servers={}']
    resume = [*pinned, '{session_id}', '{prompt}']
    handle = rt.start(spec(tmp_path, exe, 'the brief', env={'SECRET': 's'}, pattern='id=(\\S+)', resume=resume))
    rt.prompt(handle, 'first note')
    (tmp_path / 'go-1').write_text('\n')
    while not (tmp_path / 'turn-2.env').exists():
        time.sleep(0.05)
    Subprocess().open('subprocess').prompt(Handle(handle.runtime, dict(handle.data)), 'second note')
    (tmp_path / 'go-2').write_text('\n')
    assert rt.wait(handle, Deadline.after(20)).error is None
    argvs = [(tmp_path / f'turn-{n}.args').read_text().splitlines() for n in (2, 3)]
    assert argvs == [[*pinned, 's-1', 'first note'], [*pinned, 's-1', 'second note']]
    envs = {(tmp_path / f'turn-{n}.env').read_text() for n in (1, 2, 3)}
    assert len(envs) == 1 and 'SECRET=s\n' in envs.pop()


def test_an_agent_that_piloop_0_4_resumed_is_killed_through_its_current_turn_and_takes_no_message(
    tmp_path: Path,
) -> None:
    """piloop 0.4 resumed an agent from the daemon and named the resumed process in ``agent_session.current``."""
    (tmp_path / 'logs').mkdir()
    first = subprocess.Popen(['true'])
    first.wait()
    turn = subprocess.Popen(['sleep', '30'], start_new_session=True)
    start = started(turn.pid)
    (tmp_path / 'logs' / 'agent_session.current').write_text(json.dumps({'pid': turn.pid, 'start_time': start}))
    handle = Handle(
        'subprocess',
        {'pid': first.pid, 'pgid': first.pid, 'start_time': 1.0, 'log_dir': str(tmp_path / 'logs'), 'token': 'old'},
    )
    rt = Subprocess().open('subprocess')
    assert rt._alive(handle)
    with pytest.raises(NotSteerable, match='an older piloop started it'):
        rt.prompt(handle, 'hi')
    rt.kill(handle)
    assert turn.wait(5) == -signal.SIGKILL


def test_queueing_and_draining_hold_one_lock_so_no_message_falls_between_them(tmp_path: Path) -> None:
    """A message appended while a turn starts is either in that turn's prompt or left for the next one."""
    rt = Subprocess().open('subprocess')
    handle = rt.start(spec(tmp_path, script(tmp_path, 'sleep 0.5'), resume=['resume', 'id', '{prompt}']))
    queue = tmp_path / 'logs' / 'queued-messages'
    with locked(tmp_path / 'logs'):
        appending = threading.Thread(target=rt.prompt, args=(handle, 'waits for the lock'))
        appending.start()
        appending.join(0.3)
        assert appending.is_alive() and not queue.exists(), 'the append waits while the queue is drained'
    appending.join()
    assert queue.read_text() == '"waits for the lock"\n'
    rt.wait(handle, Deadline.after(10))  # its next turn took it
    put(tmp_path / 'logs', 'drained under the lock')
    turns = Turns(
        prefix=['harness'],
        args=[],
        resume_args=['{prompt}'],
        session_id_pattern=None,
        stdout=tmp_path / 'stdout',
        queue=queue,
    )
    with locked(tmp_path / 'logs'):
        draining = threading.Thread(target=turns.resumed)
        draining.start()
        draining.join(0.3)
        assert draining.is_alive() and queue.exists(), 'the drain waits while a message is appended'
    draining.join()
    assert not queue.exists()


def test_a_start_that_fails_after_the_spawn_kills_and_waits_for_the_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """macOS answered a call after the spawn with ESRCH for a quick agent, and the start failed with its ``Popen``
    never waited for. Python then warned ``subprocess N is still running`` in whatever test collected it."""

    def gone(pid: int) -> float:
        raise ProcessLookupError(3, 'No such process')

    monkeypatch.setattr(subprocess_runtime, 'started', gone)
    spawned: list[subprocess.Popen[bytes]] = []
    real = subprocess_runtime._spawn

    def spawn(spec: AgentSpec, command: subprocess_runtime.Command, supervised: bool) -> subprocess.Popen[bytes]:
        spawned.append(proc := real(spec, command, supervised))
        return proc

    monkeypatch.setattr(subprocess_runtime, '_spawn', spawn)
    rt = Subprocess().open('subprocess')
    with pytest.raises(ProcessLookupError):
        rt.start(spec(tmp_path, script(tmp_path, 'sleep 30')))
    assert rt._procs == {}, 'not kept: nothing would ever wait for it'
    assert spawned[0].returncode is not None, 'killed and waited for'


def test_a_start_is_a_spec_once_the_worker_fills_in_where_it_runs(tmp_path: Path) -> None:
    """``AgentStart`` names no place of the machine that made it: ``at`` fills ``{cwd}`` and ``{log_dir}``."""
    start = AgentStart(
        executable='pi',
        args=['--log', '{log_dir}/pi.log', 'Your brief is in {log_dir}/brief.md'],
        env={'PILOOP_ACTOR': 'implementer#1'},
        session_id_pattern='id=(\\S+)',
        resume_args=['--cd', '{cwd}', '{prompt}'],
    )
    spec = start.at(tmp_path / 'wt', tmp_path / 'logs', {'PILOOP_HOME': str(tmp_path)})
    assert spec.args == ['--log', f'{tmp_path}/logs/pi.log', f'Your brief is in {tmp_path}/logs/brief.md']
    assert spec.resume_args == ['--cd', f'{tmp_path}/wt', '{prompt}']
    assert spec.env == {'PILOOP_ACTOR': 'implementer#1', 'PILOOP_HOME': str(tmp_path)}
    filled = {'args', 'resume_args', 'env'}
    assert {f.name: getattr(spec, f.name) for f in fields(AgentStart) if f.name not in filled} == {
        f.name: getattr(start, f.name) for f in fields(AgentStart) if f.name not in filled
    }, 'every other field of the start, as it is'
