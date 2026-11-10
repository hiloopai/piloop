"""The ``piloop`` calls of an agent on a worker elsewhere: relayed by its worker, run on the server as the agent."""

import errno
import io
import logging
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import replace
from functools import partial
from importlib.metadata import EntryPoint
from pathlib import Path
from typing import ClassVar

import httpx2 as httpx
import pytest
from covalence import Cid
from git import Repo
from pydantic import SecretStr
from pytest_mock import MockerFixture
from sqlalchemy import select

import piloop
from piloop.actor import Agent, System
from piloop.agents.roles import PILOOP_HINT
from piloop.campaign.campaigns import checkout_of
from piloop.cli.main import app
from piloop.config import load_campaign_config
from piloop.db.models import AgentSession, AgentSessionState, Campaign, Comment, Event
from piloop.db.session import Database
from piloop.ids import AgentSessionId, CampaignId, Revision
from piloop.net.tokens import mint_agent
from piloop.otel import Tracer
from piloop.plugins.campaign_type.kit.backlog.cli import land
from piloop.plugins.campaign_type.kit.backlog.wire import Base
from piloop.plugins.campaign_type.kit.candidates.ids import CandidateId
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.evaluation.section import measure
from piloop.plugins.campaign_type.kit.runs.controller import RunController
from piloop.plugins.executor import workers as workers_module
from piloop.plugins.executor.contract import AgentJob, RepoId, Source
from piloop.plugins.executor.kit import door as door_module, forks as forks_module, jobs, relay as relay_module
from piloop.plugins.executor.kit.client import Dismissed
from piloop.plugins.executor.kit.clones import NotSent
from piloop.plugins.executor.kit.door import AgentCall, Call, Relayed, main
from piloop.plugins.executor.kit.forks import Forks, Runs, processes
from piloop.plugins.executor.kit.leases import StaleLease
from piloop.plugins.executor.kit.relay import Relay, Runner, Shipped
from piloop.plugins.executor.workers import FORKED, Workers
from piloop.plugins.role.contract import RELAYED, RoleConfig, Verb
from piloop.plugins.runtime.contract import MESSAGES_ENV, QUEUE, put
from piloop.plugins.section.contract import SECTIONS, Guide
from piloop.plugins.vcs.contract import Repository, UnknownRevision, VcsError
from piloop.plugins.vcs.git import Git
from piloop.proc import ACTOR_ENV, AGENT_TOKEN_ENV, CHECKOUT_ENV, PILOOP_ARGV, child_env, run_group
from piloop.settings import Settings
from tests.conftest import (
    ROLE_GROUP,
    ScriptRuntime,
    baseline,
    disk,
    factory,
    forget_loaded,
    install_plugins,
    no_land,
    no_ship,
    no_take,
    workers_of,
)
from tests.fixtures.plugin import Auditor
from tests.test_clones import REPO, commit, fetch_from
from tests.test_lease import agent_job

AGENT = AgentSessionId(7)
JOB = AgentJob.model_construct(agent_session=AGENT)
"""The job of ``AGENT``, as the relay reads it: its agent session."""
TOKEN = SecretStr('plt_agent')
SPAN = re.compile(r'`([^`]+)`')
FENCE = re.compile(r'```[a-z]*\n(.*?)```', re.DOTALL)
CALLER = re.compile(r'^(?:piloop|"\$PILOOP_BIN"|\{piloop\})\s')


def commands(text: str) -> set[tuple[str, ...]]:
    """The ``piloop`` commands that ``text`` names in its code spans and code blocks."""
    blocks = FENCE.findall(text)
    spans = SPAN.findall(FENCE.sub('', text))
    snippets = [*spans, *(line for block in blocks for line in block.splitlines())]
    found: set[tuple[str, ...]] = set()
    for snippet in snippets:
        if CALLER.match(snippet.strip()):
            tokens = [t for t in snippet.split()[1:] if re.fullmatch(r'[a-z][a-z-]*', t)]
            if chain := app.parse_commands(tokens)[0]:
                found.add(tuple(chain))
    return found


GUIDES = {name: p.model for name, p in SECTIONS.providers().items() if issubclass(p.model, Guide)}
"""Every built-in guide, by name."""
ROLE_PROMPTS = sorted((Path(piloop.__file__).parent / 'plugins').glob('**/role/prompts/*.md'))
"""The prompts of the built-in roles."""


def test_an_agent_on_another_machine_may_run_the_commands_that_the_role_prompts_name_and_no_other() -> None:
    """The allow-list is the prompts': a command that a prompt adds is relayed, and one that it drops is not.

    The prompts are those of the roles and every guide that a brief can point to (``piloop schema <guide>``).
    """
    roles = [p.read_text() for p in ROLE_PROMPTS]
    prompts = [*roles, *(guide.TEXT.read_text() for name, guide in GUIDES.items() if name != 'orchestration')]
    briefs = [
        f'`{PILOOP_HINT} sql -`',
        f'`{PILOOP_HINT} schema <guide>`',
        RoleConfig.IDEAS,
        measure.SUBMIT,
    ]
    named = set().union(*(commands(text) for text in [*prompts, *briefs]))
    assert named == RELAYED


class Reader(Auditor):
    """A package's role whose prompt names ``piloop inbox show``: its agents on another machine may run it too."""

    RELAYED: ClassVar[frozenset[Verb]] = frozenset({*RELAYED, ('inbox', 'show')})


def test_the_commands_that_run_are_those_of_the_agents_role(
    db: Database, campaign: Campaign, tmp_path: Path, runner: Runner, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_plugins(monkeypatch, [EntryPoint('reader', f'{__name__}:Reader', ROLE_GROUP)])
    try:
        worker = agent_job(db, campaign, tmp_path)
        with db.session() as s:
            reading = {'role': {'reader': {'timeout': 60}}}  # its table puts it in the stored config
            s.get_one(Campaign, campaign.id).config = load_campaign_config(Path(campaign.path), plugins=reading)
            s.flush()
            reader = AgentSession(kind='reader', campaign_id=campaign.id)
            s.add(reader)
            s.commit()
            job = worker.model_copy(update={'agent_session': reader.id})
        refused = runner(worker, Call(argv=['inbox', 'show', '1'], token=launched(db, worker)))
        assert refused.exit_code == 2, 'not in the default list'
        shown = runner(job, Call(argv=['inbox', 'show', '1'], token=launched(db, job)))
        assert 'an agent on another machine runs only' not in shown.stderr, shown.stderr
    finally:
        forget_loaded()


@pytest.fixture
def runner(db: Database, tmp_path: Path) -> Iterator[Runner]:
    """The server's end, as the daemon of this platform runs its calls (``workers.FORKED``)."""
    if not FORKED:
        yield Runner(db, tmp_path, partial(checkout_of, db), processes)
        return
    forks = Forks(tmp_path, 4)
    yield Runner(db, tmp_path, partial(checkout_of, db), forks)
    forks.close()


forked_only = pytest.mark.skipif(not FORKED, reason='the server forks relayed calls on Linux only (workers.FORKED)')


def sh(script: str) -> Runs:
    """Run ``script`` with sh in place of the call's ``piloop`` command, which gets its words after it."""

    def run(
        argv: Sequence[str], *, cwd: Path, env: Mapping[str, str], timeout: float, input: str | None
    ) -> subprocess.CompletedProcess[str]:
        return run_group(['sh', '-c', script, 'piloop', *argv], cwd=cwd, env=env, timeout=timeout, input=input)

    return run


def launched(db: Database, job: AgentJob) -> SecretStr:
    """Mark the job's agent session running, and mint its token, as its worker's start does."""
    with db.session() as s:
        session = s.get_one(AgentSession, job.agent_session)
        session.state = AgentSessionState.RUNNING
        token = mint_agent(s, Agent(role=session.kind, session=session.id))
        s.commit()
    return token


def test_a_relayed_call_runs_on_the_server_as_the_agent_in_its_campaigns_checkout(
    db: Database, campaign: Campaign, tmp_path: Path, runner: Runner
) -> None:
    job = agent_job(db, campaign, tmp_path)
    token = launched(db, job)
    actor = Agent(role='implementer', session=job.agent_session).signature
    note = f"INSERT INTO comments (target_kind, target_id, author, kind, body) VALUES ('campaign', 1, '{actor}', 'note', 'hi')"
    done = runner(job, Call(argv=['sql', '-'], stdin=note, token=token))
    assert done.exit_code == 0, done.stderr
    with db.session() as s:
        [written] = s.scalars(select(Comment).where(Comment.body == 'hi')).all()
        [event] = s.scalars(select(Event).where(Event.kind == 'comment.note')).all()
        assert event.actor == actor, 'signed as the agent of the lease'
    assert runner(job, Call(argv=['sql', 'SELECT 41 + 1'], token=token)).stdout.count('42') == 1, (
        'no stdin: none is read'
    )


def test_a_relayed_call_runs_only_with_the_token_of_the_lease_s_agent(
    db: Database, campaign: Campaign, tmp_path: Path, runner: Runner
) -> None:
    """Another agent of the same worker names the lease's agent: its own token gives it away."""
    job, sibling = agent_job(db, campaign, tmp_path), agent_job(db, campaign, tmp_path)
    launched(db, job)
    for token in (launched(db, sibling), TOKEN):
        done = runner(job, Call(argv=['sql', 'SELECT 1'], token=token))
        assert (done.exit_code, done.stderr) == (
            1,
            f'piloop: the token is not the one of agent_session#{job.agent_session}\n',
        )


def as_agent(db: Database, job: AgentJob, token: SecretStr, tmp_path: Path) -> dict[str, str]:
    """The environment of a relayed call of the job's agent, as ``Runner`` makes it."""
    actor = Agent(role='implementer', session=job.agent_session).signature
    return child_env() | {'PILOOP_HOME': str(tmp_path), ACTOR_ENV: actor, AGENT_TOKEN_ENV: token.get_secret_value()}


def test_each_forked_call_runs_as_its_own_agent_with_its_own_token_also_two_at_once(
    db: Database, campaign: Campaign, tmp_path: Path, runner: Runner, daemon_api: None
) -> None:
    """The template is made with no agent's variables: each child reads ``settings`` from its own call's. An agent
    acts only with its own token, so a child with another call's token would be refused."""
    jobs_ = [agent_job(db, campaign, tmp_path) for _ in range(2)]
    tokens_ = [launched(db, job) for job in jobs_]
    done: list[subprocess.CompletedProcess[str]] = []

    def message(job: AgentJob, token: SecretStr) -> None:
        env = as_agent(db, job, token, tmp_path)
        done.append(
            runner.run(['message', 'demo', f'from {env[ACTOR_ENV]}'], cwd=tmp_path, env=env, timeout=60, input=None)
        )

    calls = [threading.Thread(target=message, args=pair) for pair in zip(jobs_, tokens_, strict=True)]
    for each in calls:
        each.start()
    for each in calls:
        each.join(60)
    assert [d.returncode for d in done] == [0, 0], [d.stderr for d in done]
    with db.session() as s:
        signed = {(c.author, c.body) for c in s.scalars(select(Comment).where(Comment.kind == 'message'))}
    assert signed == {
        (a, f'from {a}') for a in (Agent(role='implementer', session=j.agent_session).signature for j in jobs_)
    }


def template_of(forks: Forks) -> subprocess.Popen[bytes]:
    assert forks._template is not None, 'a call started it'
    return forks._template[0]


@forked_only
@pytest.mark.parametrize('argv', [['sql', 'SELECT 41 + 1'], ['sql'], ['sql', 'SELECT nope']])
def test_a_forked_call_prints_and_exits_as_a_process_of_its_own(tmp_path: Path, db: Database, argv: list[str]) -> None:
    """Its output, and its code: 0, a usage error's 2, an error's message and 1."""
    env = child_env() | {'PILOOP_HOME': str(tmp_path), 'COLUMNS': '80'}
    forks = Forks(tmp_path, 1)
    try:
        forked = forks(argv, cwd=tmp_path, env=env, timeout=60, input=None)
    finally:
        forks.close()
    alone = processes(argv, cwd=tmp_path, env=env, timeout=60, input=None)
    assert (forked.returncode, forked.stdout, forked.stderr) == (alone.returncode, alone.stdout, alone.stderr)


@forked_only
def test_a_forked_call_that_fails_to_read_its_settings_ends_with_a_traceback_and_1(
    tmp_path: Path, db: Database
) -> None:
    """As a ``piloop`` process does, whose import fails: the child sends no code."""
    env = child_env() | {'PILOOP_HOME': str(tmp_path), 'PILOOP_DEBUG': 'maybe'}
    forks = Forks(tmp_path, 1)
    try:
        forked = forks(['sql', 'SELECT 1'], cwd=tmp_path, env=env, timeout=60, input=None)
    finally:
        forks.close()
    alone = processes(['sql', 'SELECT 1'], cwd=tmp_path, env=env, timeout=60, input=None)
    assert forked.returncode == alone.returncode == 1
    assert 'ValidationError' in forked.stderr and 'ValidationError' in alone.stderr


@forked_only
def test_a_template_that_does_not_start_leaves_each_call_to_a_process_of_its_own(
    tmp_path: Path, db: Database, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(forks_module, 'TEMPLATE_ARGV', (str(tmp_path / 'missing'),))
    forks = Forks(tmp_path, 1)
    env = child_env() | {'PILOOP_HOME': str(tmp_path)}
    with caplog.at_level(logging.WARNING):
        done = forks(['sql', 'SELECT 41 + 1'], cwd=tmp_path, env=env, timeout=60, input=None)
    assert done.returncode == 0 and done.stdout.count('42') == 1
    assert 'not forked from the template: the template did not start:' in caplog.text


@forked_only
def test_a_call_that_the_template_does_not_take_runs_in_a_process_of_its_own(
    tmp_path: Path, db: Database, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    forks = Forks(tmp_path, 1)
    env = child_env() | {'PILOOP_HOME': str(tmp_path)}

    def broken(*_: object) -> int:
        raise BrokenPipeError(32, 'Broken pipe')

    try:
        monkeypatch.setattr(forks_module.socket, 'send_fds', broken)
        with caplog.at_level(logging.WARNING):
            done = forks(['sql', 'SELECT 41 + 1'], cwd=tmp_path, env=env, timeout=60, input=None)
        assert done.returncode == 0 and done.stdout.count('42') == 1
        assert 'the template took no call: [Errno 32] Broken pipe' in caplog.text and forks._template is None
    finally:
        forks.close()


@forked_only
def test_a_dead_template_is_replaced_and_a_call_that_it_never_started_runs_in_a_process_of_its_own(
    tmp_path: Path, db: Database, caplog: pytest.LogCaptureFixture
) -> None:
    env = child_env() | {'PILOOP_HOME': str(tmp_path)}
    forks = Forks(tmp_path, 2)
    try:
        assert forks(['sql', 'SELECT 41 + 1'], cwd=tmp_path, env=env, timeout=60, input=None).stdout.count('42') == 1
        first = template_of(forks)
        first.kill()
        first.wait()
        with caplog.at_level(logging.WARNING):
            assert forks(['sql', '-'], cwd=tmp_path, env=env, timeout=60, input='SELECT 6 * 7').stdout.count('42') == 1
        assert 'the template of the relayed calls exited with -9: a new one starts' in caplog.text
        second = template_of(forks)
        assert second is not first
        forks._end(first)  # as a call that the first one stalled ends it late
        assert template_of(forks) is second, 'it ends only its own template'
        os.kill(second.pid, signal.SIGSTOP)  # it takes no call now
        found: list[subprocess.CompletedProcess[str]] = []
        call = threading.Thread(
            target=lambda: found.append(forks(['sql', 'SELECT 7'], cwd=tmp_path, env=env, timeout=60, input=None))
        )
        with caplog.at_level(logging.WARNING):
            call.start()
            time.sleep(0.3)
            second.kill()  # with the call that it took, unstarted
            call.join(60)
        assert found[0].returncode == 0 and '│ 7 │' in found[0].stdout
        assert 'a relayed call runs in a process of its own, not forked from the template:' in caplog.text
    finally:
        forks.close()


@forked_only
def test_a_call_that_its_template_did_not_start_in_time_ends_the_template(tmp_path: Path, db: Database) -> None:
    env = child_env() | {'PILOOP_HOME': str(tmp_path)}
    forks = Forks(tmp_path, 1)
    try:
        assert forks(['sql', 'SELECT 1'], cwd=tmp_path, env=env, timeout=60, input=None).returncode == 0
        stuck = template_of(forks)
        os.kill(stuck.pid, signal.SIGSTOP)
        with pytest.raises(subprocess.TimeoutExpired):
            forks(['sql', 'SELECT 1'], cwd=tmp_path, env=env, timeout=0.5, input=None)
        assert stuck.poll() is not None and forks._template is None, 'it may start the call still: it goes'
    finally:
        forks.close()


@forked_only
def test_a_template_that_runs_another_thread_forks_nothing_and_every_call_runs_in_a_process_of_its_own(
    tmp_path: Path, db: Database, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A fork copies only the thread that forks: a lock that another thread holds stays held in the child."""
    threaded = 'import sys, threading, time; threading.Thread(target=time.sleep, args=(60,), daemon=True).start()'
    serving = 'from piloop.plugins.executor.kit.template import serve; serve(int(sys.argv[1]))'
    monkeypatch.setattr(forks_module, 'TEMPLATE_ARGV', (sys.executable, '-P', '-c', f'{threaded}; {serving}'))
    env = child_env() | {'PILOOP_HOME': str(tmp_path)}
    forks = Forks(tmp_path, 1)
    try:
        with caplog.at_level(logging.WARNING):
            for _ in range(3):
                assert (
                    forks(['sql', 'SELECT 41 + 1'], cwd=tmp_path, env=env, timeout=60, input=None).stdout.count('42')
                    == 1
                )
        assert 'the template ran more than one thread: every relayed call runs so from now on' in caplog.text
        assert forks._template is None, 'none started after it'
    finally:
        forks.close()


@forked_only
def test_a_template_that_does_not_exit_when_closed_is_killed(
    tmp_path: Path, db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(forks_module, 'CLOSE_SECONDS', 0.2)
    env = child_env() | {'PILOOP_HOME': str(tmp_path)}
    forks = Forks(tmp_path, 1)
    assert forks(['sql', 'SELECT 1'], cwd=tmp_path, env=env, timeout=60, input=None).returncode == 0
    stuck = template_of(forks)
    os.kill(stuck.pid, signal.SIGSTOP)
    forks.close()
    assert stuck.returncode == -signal.SIGKILL


def test_off_linux_the_server_runs_each_relayed_call_in_a_process_of_its_own(
    tmp_path: Path, db: Database, runtime: ScriptRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(workers_module, 'FORKED', False)
    executor = workers_of(RunController(db, Tracer(None), tmp_path, factory(runtime), executor=Workers()))
    run = executor._cli.run
    env = child_env() | {'PILOOP_HOME': str(tmp_path)}
    assert run is processes and executor._forks is None, 'no template'
    done = run(['sql', 'SELECT 41 + 1'], cwd=tmp_path, env=env, timeout=60, input=None)
    assert done.returncode == 0 and done.args[: len(PILOOP_ARGV)] == list(PILOOP_ARGV), 'a piloop process of its own'
    executor.close()


@forked_only
def test_a_call_with_no_socket_for_it_runs_in_a_process_of_its_own(
    tmp_path: Path, db: Database, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """As on a system without ``SOCK_SEQPACKET`` Unix sockets, or out of descriptors."""

    def refused(*_: object) -> tuple[socket.socket, socket.socket]:
        raise OSError(errno.EPROTONOSUPPORT, 'Protocol not supported')

    monkeypatch.setattr(forks_module.socket, 'socketpair', refused)
    forks = Forks(tmp_path, 1)
    env = child_env() | {'PILOOP_HOME': str(tmp_path)}
    with caplog.at_level(logging.WARNING):
        done = forks(['sql', 'SELECT 41 + 1'], cwd=tmp_path, env=env, timeout=60, input=None)
    assert done.returncode == 0 and done.stdout.count('42') == 1
    assert f'not forked from the template: no socket for the call: [Errno {errno.EPROTONOSUPPORT}]' in caplog.text
    forks.close()


@forked_only
def test_the_calls_over_the_size_wait_for_one_to_end(tmp_path: Path, db: Database) -> None:
    env = child_env() | {'PILOOP_HOME': str(tmp_path)}
    forks = Forks(tmp_path, 1)
    endless = 'WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) SELECT count(*) FROM c'
    try:
        assert forks(['sql', 'SELECT 1'], cwd=tmp_path, env=env, timeout=60, input=None).returncode == 0
        long = threading.Thread(
            target=lambda: pytest.raises(
                subprocess.TimeoutExpired, forks, ['sql', endless], cwd=tmp_path, env=env, timeout=2, input=None
            )
        )
        long.start()
        time.sleep(0.5)
        began = time.monotonic()
        assert forks(['sql', 'SELECT 1'], cwd=tmp_path, env=env, timeout=60, input=None).returncode == 0
        assert time.monotonic() - began > 1, 'it ran only once the first call was stopped'
        long.join(30)
    finally:
        forks.close()


def test_a_command_that_is_not_relayed_is_refused_with_what_is(
    db: Database, campaign: Campaign, tmp_path: Path, runner: Runner
) -> None:
    done = runner(agent_job(db, campaign, tmp_path), Call(argv=['campaign', 'stop', 'demo'], token=TOKEN))
    assert done.exit_code == 2 and done.stderr.startswith('piloop campaign stop: an agent on another machine runs only')
    assert 'idea claim' in done.stderr


def test_a_relayed_call_that_runs_too_long_is_stopped(
    db: Database, campaign: Campaign, tmp_path: Path, runner: Runner, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = agent_job(db, campaign, tmp_path)
    token = launched(db, job)
    assert runner(job, Call(argv=['sql', 'SELECT 1'], token=token)).exit_code == 0, 'the template runs'
    monkeypatch.setattr(relay_module, 'RELAY_SECONDS', 1.0)
    endless = 'WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) SELECT count(*) FROM c'
    done = runner(job, Call(argv=['sql', endless], token=token))
    assert (done.exit_code, done.stderr) == (124, 'piloop: the call ran for 1 s on the server and was stopped\n')
    monkeypatch.setattr(relay_module, 'RELAY_SECONDS', 60.0)  # a call of a process of its own takes about 1 s off Linux
    assert runner(job, Call(argv=['sql', 'SELECT 1'], token=token)).exit_code == 0, 'the next call runs'


class Server:
    """The server's half of the relay, as ``Remote.cli`` reaches it: what it got, and what it answers."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, Call]] = []
        self.fail: Exception | None = None

    def __call__(self, lease: int, call: Call) -> Relayed:
        self.calls.append((lease, call))
        if self.fail is not None:
            raise self.fail
        return Relayed(stdout=' '.join(call.argv), stderr='', exit_code=0)


def post(relay: Relay, body: bytes | str, path: str = '/v1/cli') -> httpx.Response:
    """A request to ``relay``'s socket, as an agent's ``piloop`` sends it."""
    with httpx.Client(transport=httpx.HTTPTransport(uds=str(relay.path))) as client:
        return client.post(f'http://worker{path}', content=body)


def call(agent: int = AGENT, argv: list[str] | None = None) -> str:
    return AgentCall(agent=AgentSessionId(agent), argv=argv or [], token=TOKEN).model_dump_json()


def test_the_relay_takes_an_agents_calls_on_its_lease_while_its_job_runs_on_this_worker(tmp_path: Path) -> None:
    server = Server()
    relay = Relay(Settings(home=tmp_path).relay_file, server, no_ship, no_land, no_take)
    relay.start()
    assert relay.path.parent.stat().st_mode & 0o077 == 0, "only the worker's user reaches its socket"
    assert post(relay, call()).status_code == 404, 'no job of this agent runs here'
    relay.open(3, JOB)
    answered = post(relay, call(argv=['sql', 'SELECT 1']))
    assert Relayed.model_validate_json(answered.content) == Relayed(stdout='sql SELECT 1', exit_code=0)
    assert server.calls == [(3, Call(argv=['sql', 'SELECT 1'], token=TOKEN))]
    assert post(relay, '{"argv": 1}').status_code == 422
    assert post(relay, '{}', path='/v1/other').status_code == 404
    server.fail = StaleLease('stale_lease: lease#3 is reported')
    assert Relayed.model_validate_json(post(relay, call()).content) == Relayed(
        stderr='piloop: the lease of this agent ended: stale_lease: lease#3 is reported\n', exit_code=1
    )
    server.fail = httpx.ConnectError('refused')
    assert Relayed.model_validate_json(post(relay, call()).content).stderr == (
        'piloop: the call did not run on the server: refused\n'
    )
    server.fail = Dismissed('the server refused this worker (401): revoked')
    assert Relayed.model_validate_json(post(relay, call()).content).stderr == (
        'piloop: the call did not run on the server: the server refused this worker (401): revoked\n'
    )
    server.fail = httpx.ReadTimeout('timed out')
    assert Relayed.model_validate_json(post(relay, call()).content).stderr == (
        'piloop: the answer of the server was lost, and the call may have run there: timed out\n'
    )
    relay.close(3)
    assert post(relay, call()).status_code == 404, 'its job ended'
    relay.stop()
    assert not relay.path.exists()
    restarted = Relay(Settings(home=tmp_path).relay_file, server, no_ship, no_land, no_take)
    restarted.path.touch()  # what a worker that was killed leaves
    restarted.start()
    restarted.open(4, JOB)
    assert post(restarted, call()).status_code == 200, 'a re-attached agent relays again, on the same path'
    restarted.stop()
    Relay(Settings(home=tmp_path).relay_file, server, no_ship, no_land, no_take).stop()


SECOND_RELAY = (
    'import sys; from pathlib import Path; from piloop.plugins.executor.kit.relay import Relay;'
    ' from piloop.settings import Settings; from tests.conftest import no_land, no_ship, no_take;'
    ' Relay(Settings(home=Path(sys.argv[1])).relay_file, print, no_ship, no_land, no_take).start()'
)
"""A second ``piloop worker`` on the home: its relay starts in a process of its own."""


def test_a_second_worker_on_one_home_is_refused_and_the_first_keeps_relaying_its_agents_calls(tmp_path: Path) -> None:
    """A second relay used to unlink the first's socket and bind its own: the first's agents got 404 from it, and once
    it stopped, nothing relayed on the home (B33). Two processes with one worker name on one home meet here too (B39).
    """
    first = Relay(Settings(home=tmp_path).relay_file, Server(), no_ship, no_land, no_take)
    first.start()
    first.open(3, JOB)
    second = [sys.executable, '-c', SECOND_RELAY, str(tmp_path)]
    refused = subprocess.run(second, capture_output=True, text=True, timeout=60, cwd=Path(piloop.__file__).parents[1])
    assert refused.returncode == 1 and 'RelayTaken: another worker relays on' in refused.stderr, refused.stderr
    assert post(first, call()).status_code == 200, "the first worker's agent still reaches its own worker"
    first.stop()
    started = subprocess.run(second, capture_output=True, text=True, timeout=60, cwd=Path(piloop.__file__).parents[1])
    assert started.returncode == 0, f'once the first stopped, its lock is free: {started.stderr}'


def test_the_piloop_of_an_agent_elsewhere_prints_what_the_call_printed_and_exits_with_its_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], mocker: MockerFixture
) -> None:
    server = Server()
    relay = Relay(Settings(home=tmp_path).relay_file, server, no_ship, no_land, no_take)
    relay.start()
    agent = Agent(role='implementer', session=AGENT)
    monkeypatch.setattr(door_module, 'settings', Settings(home=tmp_path, actor=agent))
    with pytest.raises(SystemExit, match='implementer#7 has no PILOOP_AGENT_TOKEN, and the call did not run'):
        main()
    monkeypatch.setattr(door_module, 'settings', Settings(home=tmp_path, actor=agent, agent_token=TOKEN))
    monkeypatch.setattr(sys, 'argv', ['piloop', 'sql', '-'])
    monkeypatch.setattr(sys, 'stdin', io.StringIO('SELECT 1'))
    with pytest.raises(SystemExit, match='the call did not run: agent_session#7 has no job on this worker'):
        main()
    relay.open(3, JOB)
    monkeypatch.setattr(sys, 'stdin', io.StringIO('SELECT 1'))
    with pytest.raises(SystemExit) as ended:
        main()
    assert (ended.value.code, capsys.readouterr().out) == (0, 'sql -')
    assert server.calls == [(3, Call(argv=['sql', '-'], stdin='SELECT 1', token=TOKEN))]
    relay.stop()
    with pytest.raises(SystemExit, match='no worker relays on .*, and the call did not run'):
        main()
    relay.path.touch()  # the socket of a worker that was killed, whose connection did not open in time
    unsent = mocker.patch.object(httpx.Client, 'post', side_effect=httpx.ConnectTimeout('timed out'))
    with pytest.raises(SystemExit, match='the relay of this worker did not answer, and the call did not run'):
        main()
    mocker.stop(unsent)
    relay.path.unlink()
    with socket.socket(socket.AF_UNIX) as hung:  # a worker that dies once it has the call, which it may have relayed
        hung.bind(str(relay.path))
        hung.listen()
        threading.Thread(target=lambda: hung.accept()[0].close(), daemon=True).start()
        with pytest.raises(SystemExit, match='the relay of this worker did not answer, and the call may have run'):
            main()
    monkeypatch.setattr(door_module, 'settings', Settings(home=tmp_path, actor=System.HUMAN))
    with pytest.raises(SystemExit, match='relays the calls of an agent, and human is none'):
        main()
    execv = mocker.patch.object(door_module.os, 'execv', side_effect=SystemExit(0))
    monkeypatch.setattr(sys, 'argv', ['piloop', 'agent', 'messages', '--harness', 'claude'])
    with pytest.raises(SystemExit):
        main()
    assert execv.call_args.args[1][-4:] == ['agent', 'messages', '--harness', 'claude'], 'run here, not relayed'


def test_the_shim_of_a_worker_elsewhere_relays_a_call_and_reads_the_agents_queue_here(tmp_path: Path) -> None:
    from piloop.plugins.executor.kit.jobs import remote_site
    from tests.conftest import disk

    site = remote_site(tmp_path, disk(tmp_path), lambda repo, revisions, have: [], reserve=0)
    done = subprocess.run([site.cli, 'sql', 'SELECT 1'], capture_output=True, text=True, env={'PATH': '/usr/bin:/bin'})
    assert done.returncode == 1 and 'relays the calls of an agent, and human is none' in done.stderr
    log_dir = tmp_path / 'runs' / '1'
    log_dir.mkdir(parents=True)
    put(log_dir, 'try lzma')
    env = {'PATH': '/usr/bin:/bin', MESSAGES_ENV: str(log_dir / QUEUE), ACTOR_ENV: 'implementer#1'}
    taken = subprocess.run([site.cli, 'agent', 'messages'], capture_output=True, text=True, env=env)
    assert (taken.returncode, taken.stdout.strip()) == (0, 'try lzma'), 'its queue is on the worker: read here'
    put(log_dir, 'try zstd')
    called = subprocess.run([site.cli, 'sql', 'SELECT 1'], capture_output=True, text=True, env=env)
    assert called.stderr.startswith('try zstd\n'), 'any call delivers them here, before it is relayed'
    assert 'implementer#1 has no PILOOP_AGENT_TOKEN' in called.stderr and not (log_dir / QUEUE).exists()


def test_a_submit_ships_the_version_of_the_agents_checkout_and_is_called_once(tmp_path: Path) -> None:
    """The server's checkout lacks the agent's commit: the relay sends the version through the CAS, then calls the
    command once, which answers at once."""
    sent: list[tuple[int, Call]] = []
    shipped: list[str | None] = []

    def server(lease: int, relayed: Call) -> Relayed:
        sent.append((lease, relayed))
        return Relayed(stdout='candidate#2 is queued', exit_code=0)

    def ship(job: AgentJob, revision: Revision | None) -> Shipped:
        shipped.append(revision)
        if revision == 'gone':
            raise UnknownRevision('no gone')
        return Shipped(Revision('abc'), None)

    relay = Relay(Settings(home=tmp_path).relay_file, server, ship, no_land, no_take)
    relay.start()
    relay.open(3, JOB)
    answered = Relayed.model_validate_json(post(relay, call(argv=['submit'])).content)
    assert answered == Relayed(stdout='candidate#2 is queued', exit_code=0)
    assert [c.argv for _, c in sent] == [['submit', 'abc']], 'shipped, and called once'
    assert Relayed.model_validate_json(post(relay, call(argv=['submit', 'gone'])).content) == Relayed(
        stderr='piloop: the version did not leave this worker, and the call did not run: no gone\n', exit_code=1
    )
    assert shipped == [None, 'gone'], 'the checkout head, else the named version'
    relay.stop()


def test_a_shipped_call_keeps_the_words_of_the_agents_call(tmp_path: Path) -> None:
    """The relay used to rebuild a submit from its version alone: ``submit --help`` submitted the head (B30)."""
    head, blob = Revision('h' * 40), Cid.of(b'mine')

    def ship(job: AgentJob, revision: Revision | None) -> Shipped:
        return Shipped(revision or head, blob)

    relay = Relay(Settings(home=tmp_path).relay_file, Server(), ship, no_land, no_take)
    sent = ['--blob', str(blob)]
    assert relay.shipped(JOB, ['submit', '--help']) == ['submit', head, *sent, '--help']
    assert relay.shipped(JOB, ['submit', '-h']) == ['submit', head, *sent, '-h']
    assert relay.shipped(JOB, ['sql', '--help']) == ['sql', '--help'], 'a command that ships nothing is as it came'


@pytest.mark.parametrize(
    'error',
    [
        VcsError('no gone'),
        NotSent('the server could not put the versions into the CAS'),
        httpx.RemoteProtocolError('the server went away'),
        Dismissed('the server refused this worker (401): revoked'),
    ],
)
def test_a_call_that_ran_says_so_whatever_stopped_its_checkout_moving(tmp_path: Path, error: Exception) -> None:
    """A Land that raised anything but a ``VcsError`` sent no answer, and the agent read "did not run" (B31)."""
    server = Server()

    def server_moves(lease: int, relayed: Call) -> Relayed:
        server(lease, relayed)
        return Relayed(stdout='idea#1 claimed\n', exit_code=0, checkout=Revision('a' * 40))

    def land(job: AgentJob, revision: Revision) -> None:
        raise error

    relay = Relay(Settings(home=tmp_path).relay_file, server_moves, no_ship, land, no_take)
    relay.start()
    relay.open(3, JOB)
    answered = post(relay, call(argv=['idea', 'claim', '1']))
    relay.stop()
    assert len(server.calls) == 1
    assert Relayed.model_validate_json(answered.content) == Relayed(
        stdout='idea#1 claimed\n',
        stderr=f'piloop: the call ran, but your checkout did not move to {"a" * 40}: {error}\n',
        exit_code=1,
        checkout=Revision('a' * 40),
    )


@pytest.mark.parametrize('error', [NotSent('the server is gone'), httpx.ReadTimeout('timed out'), Dismissed('revoked')])
def test_a_call_whose_version_did_not_leave_says_it_did_not_run(tmp_path: Path, error: Exception) -> None:
    server = Server()

    def ship(job: AgentJob, revision: Revision | None) -> Shipped:
        raise error

    relay = Relay(Settings(home=tmp_path).relay_file, server, ship, no_land, no_take)
    relay.start()
    relay.open(3, JOB)
    answered = post(relay, call(argv=['submit']))
    relay.stop()
    assert server.calls == []
    assert Relayed.model_validate_json(answered.content) == Relayed(
        stderr=f'piloop: the version did not leave this worker, and the call did not run: {error}\n', exit_code=1
    )


@pytest.mark.parametrize(
    'error', [httpx.ConnectError('refused'), httpx.ConnectTimeout('timed out'), httpx.PoolTimeout('timed out')]
)
def test_a_call_that_never_left_this_worker_did_not_run(tmp_path: Path, error: Exception) -> None:
    """A connect or pool timeout never sent the call, as a refused connection did not (B43)."""
    server = Server()
    server.fail = error
    relay = Relay(Settings(home=tmp_path).relay_file, server, no_ship, no_land, no_take)
    relay.start()
    relay.open(3, JOB)
    answered = post(relay, call())
    relay.stop()
    assert Relayed.model_validate_json(answered.content).stderr == (
        f'piloop: the call did not run on the server: {error}\n'
    )


def test_a_worker_ships_what_the_repo_of_record_lacks_of_a_version_of_its_agents_checkout(tmp_path: Path) -> None:
    def no_record(repo: RepoId) -> Repository:
        raise AssertionError(f'a ship reads no repo of record: {repo}')

    site = jobs.local_site(tmp_path, disk(tmp_path), lambda _: tmp_path / 'record', no_record, 0)
    checkout = tmp_path / 'checkout'
    repo = Repo.init(checkout, initial_branch='main')
    (checkout / 'a.txt').write_text('a\n')
    repo.git.add('-A')
    repo.git.commit('-qm', 'base')
    base = Revision(repo.head.commit.hexsha)
    (checkout / 'a.txt').write_text('b\n')
    repo.git.commit('-qam', 'mine')
    job = AgentJob.model_construct(
        agent_session=AGENT, place='p', artifact=Source(vcs=Git(), repo=RepoId(CampaignId(1)), revision=base)
    )
    site_cwd = site.cwd(job)
    site_cwd.parent.mkdir(parents=True)
    checkout.rename(site_cwd)
    head = jobs.ship(site, job, None)
    assert head.revision == Repo(site_cwd).head.commit.hexsha and head.blob is not None, 'its head, and the commit'
    assert jobs.ship(site, job, base) == Shipped(base, None), 'the repo of record has its starting version'


def test_a_relayed_answer_names_the_candidates_that_the_agents_checkout_started_without(
    db: Database, campaign: Campaign, tmp_path: Path, runner: Runner
) -> None:
    job = agent_job(db, campaign, tmp_path)
    token = launched(db, job)
    assert runner(job, Call(argv=['sql', 'SELECT 1'], token=token)).history == [], 'its checkout is of the baseline'
    later = Revision('b' * 40)
    with db.session() as s:
        s.add(Candidate(campaign_id=campaign.id, parent_id=baseline(db, campaign.id).id, tree_sha='t', revision=later))
        s.commit()
    assert runner(job, Call(argv=['sql', 'SELECT 1'], token=token)).history == [later], 'another run submitted it since'
    held = job.model_copy(update={'artifact': job.artifact.model_copy(update={'history': [later]})})
    assert runner(held, Call(argv=['sql', 'SELECT 1'], token=token)).history == [], 'its checkout started with it'


def test_a_worker_elsewhere_takes_the_candidates_that_an_answer_names_so_that_git_show_finds_them(
    tmp_path: Path,
) -> None:
    """Another worker's candidate came after this agent's checkout was made: the answer of its next call brings it."""
    origin = tmp_path / 'origin'
    origin.mkdir()
    subprocess.run(['git', 'init', '-q', '-b', 'main'], cwd=origin, check=True)
    base = commit(origin, 'a.txt', 'a')
    store = disk(tmp_path)
    site = jobs.remote_site(tmp_path / 'worker', store, fetch_from(store, origin, []), 0)
    artifact = Source(vcs=Git(), repo=REPO, revision=Revision(base), history=[Revision(base)])
    job = AgentJob.model_construct(agent_session=AGENT, place='p', artifact=artifact)
    site.cwd(job).parent.mkdir(parents=True)
    site.clones.checkout(job.artifact, site.cwd(job))
    Repo(origin).git.checkout('-q', '--detach', base)
    other = Revision(commit(origin, 'b.txt', 'a parallel candidate'))
    failure: list[Exception] = []

    def take(job: AgentJob, revisions: Sequence[Revision]) -> None:
        if failure:
            raise failure[0]
        jobs.take(site, job, revisions)

    def server(lease: int, relayed: Call) -> Relayed:
        return Relayed(stdout='candidate#2\n', exit_code=0, history=[other])

    relay = Relay(Settings(home=tmp_path).relay_file, server, no_ship, no_land, take)
    relay.start()
    relay.open(3, job)
    answered = Relayed.model_validate_json(post(relay, call(argv=['sql', 'SELECT 1'])).content)
    assert (answered.stdout, answered.stderr, answered.exit_code) == ('candidate#2\n', '', 0)
    assert Repo(site.cwd(job)).git.show(f'{other}:b.txt') == 'a parallel candidate'
    failure.append(OSError('No space left on device'))
    answered = Relayed.model_validate_json(post(relay, call(argv=['sql', 'SELECT 1'])).content)
    assert (answered.exit_code, answered.stderr) == (
        0,
        'piloop: the call ran, but this worker lacks some candidates, and `git show` may not find them: '
        'No space left on device\n',
    ), 'the call ran: its exit code stays'
    relay.stop()


def test_a_relayed_call_that_moves_the_agents_checkout_names_the_revision_and_moves_nothing_on_the_server(
    db: Database, campaign: Campaign, tmp_path: Path, runner: Runner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A claim with a base writes its revision to the file that ``CHECKOUT_ENV`` names: the server's checkout stays."""
    job = agent_job(db, campaign, tmp_path)
    token = launched(db, job)
    writes = replace(runner, run=sh(f'printf abc > "${CHECKOUT_ENV}"'))
    assert writes(job, Call(argv=['idea', 'claim', '1'], token=token)).checkout == 'abc'
    assert replace(runner, run=sh('true'))(job, Call(argv=['idea', 'claim', '1'], token=token)).checkout is None


def test_the_relay_moves_the_agents_checkout_after_a_call_that_names_a_revision(tmp_path: Path) -> None:
    moved: list[tuple[AgentJob, Revision]] = []

    def server(lease: int, relayed: Call) -> Relayed:
        return Relayed(stdout='idea#1 claimed\n', exit_code=0, checkout=Revision(relayed.argv[-1]))

    def land(job: AgentJob, revision: Revision) -> None:
        if revision == 'gone':
            raise VcsError('no gone')
        moved.append((job, revision))

    relay = Relay(Settings(home=tmp_path).relay_file, server, no_ship, land, no_take)
    relay.start()
    relay.open(3, JOB)
    answered = Relayed.model_validate_json(post(relay, call(argv=['idea', 'claim', '1', '--base', 'a' * 40])).content)
    assert (answered.stdout, answered.exit_code) == (f'idea#1 claimed\nyour checkout is at {"a" * 12}\n', 0)
    assert moved == [(JOB, 'a' * 40)]
    failed = Relayed.model_validate_json(post(relay, call(argv=['idea', 'claim', '1', '--base', 'gone'])).content)
    assert (failed.exit_code, failed.stderr) == (
        1,
        'piloop: the call ran, but your checkout did not move to gone: no gone\n',
    ), 'the claim ran: the agent moves its checkout itself'
    relay.stop()


def test_a_claim_with_a_base_moves_the_checkout_here_or_names_the_revision_for_the_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """On the daemon's own worker, the agent's checkout is a worktree of the repo of record: the CLI moves it. A call
    that the server runs for a worker elsewhere writes the revision instead."""
    repo = Repo.init(tmp_path / 'checkout', initial_branch='main')
    (tmp_path / 'checkout' / 'a.txt').write_text('a\n')
    repo.git.add('-A')
    repo.git.commit('-qm', 'one')
    first = repo.head.commit.hexsha
    (tmp_path / 'checkout' / 'a.txt').write_text('b\n')
    repo.git.commit('-qam', 'two')
    monkeypatch.chdir(tmp_path / 'checkout')
    monkeypatch.delenv(CHECKOUT_ENV, raising=False)
    land(Base(candidate=CandidateId(4), revision=first))
    assert (
        repo.head.commit.hexsha == first
        and f'your checkout is at candidate#4 ({first[:12]})' in capsys.readouterr().out
    )
    (tmp_path / 'checkout' / 'a.txt').write_text('dirty\n')
    with pytest.raises(SystemExit, match='your run starts from candidate#4, but your checkout did not move'):
        land(Base(candidate=CandidateId(4), revision=repo.commit('main').hexsha))
    monkeypatch.setenv(CHECKOUT_ENV, str(tmp_path / 'moved'))
    land(Base(candidate=CandidateId(4), revision=first))
    assert (tmp_path / 'moved').read_text() == first
    land(None)
