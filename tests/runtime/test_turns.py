"""The supervisor of an agent (``kit.turns``), run in this process: the runtime's tests run it as the agent's."""

import io
import signal
import sys
from pathlib import Path

import pytest

from piloop.plugins.runtime.contract import QUEUE, put, take
from piloop.plugins.runtime.kit import turns as supervisor
from piloop.plugins.runtime.kit.turns import Turns


def stdin(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, command: str) -> None:
    turns = Turns(
        prefix=['sh', '-c', command, 'sh'],
        args=[],
        resume_args=['{prompt}'],
        session_id_pattern=None,
        stdout=tmp_path / 'stdout',
        queue=tmp_path / QUEUE,
    )
    monkeypatch.setattr(sys, 'stdin', io.TextIOWrapper(io.BytesIO(turns.model_dump_json().encode())))


def test_the_supervisor_reads_its_turns_on_stdin_and_exits_with_the_last_turns_status(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stdin(monkeypatch, tmp_path, 'exit 3')
    with pytest.raises(SystemExit) as exited:
        supervisor.main()
    assert exited.value.code == 3


def test_the_supervisor_of_a_turn_that_a_signal_ended_ends_by_that_signal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sent: list[tuple[str, int]] = []
    monkeypatch.setattr(supervisor.signal, 'signal', lambda sig, _: sent.append(('default', sig)))
    monkeypatch.setattr(supervisor.os, 'kill', lambda _, sig: sent.append(('kill', sig)))
    stdin(monkeypatch, tmp_path, 'kill -TERM $$')
    with pytest.raises(SystemExit):
        supervisor.main()
    assert sent == [('default', signal.SIGTERM), ('kill', signal.SIGTERM)]


def test_a_resumed_turn_waits_for_messages_and_for_the_session_id_that_its_argv_needs(tmp_path: Path) -> None:
    turns = Turns(
        prefix=['codex'],
        args=['exec', 'the brief'],
        resume_args=['exec', 'resume', '{session_id}', '{prompt}'],
        session_id_pattern='"thread_id":"([^"]+)"',
        stdout=tmp_path / 'stdout',
        queue=tmp_path / QUEUE,
    )
    assert turns.resumed() is None, 'nothing queued, nothing printed'
    put(tmp_path, 'a note')
    assert turns.resumed() is None, 'no stdout yet, so no session id'
    assert take(turns.queue) == ['a note'], 'the messages stay queued'
    (tmp_path / 'stdout').write_text('{"thread_id":"t-1"}\n')
    put(tmp_path, 'a note')
    put(tmp_path, 'and another')
    assert turns.resumed() == ['codex', 'exec', 'resume', 't-1', 'a note\nand another']


def test_the_supervisor_resumes_while_the_last_turn_exited_0_and_messages_wait(tmp_path: Path) -> None:
    log = tmp_path / 'turns.log'
    turns = Turns(
        prefix=['sh', '-c', f'echo "$1" >> {log}', 'sh'],
        args=['first'],
        resume_args=['{prompt}'],
        session_id_pattern=None,
        stdout=tmp_path / 'stdout',
        queue=tmp_path / QUEUE,
    )
    put(tmp_path, 'second')
    assert supervisor.run(turns) == 0
    assert log.read_text() == 'first\nsecond\n'
