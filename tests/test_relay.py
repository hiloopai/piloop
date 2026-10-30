"""The ``piloop`` calls of an agent on a worker elsewhere: relayed by its worker, run on the server as the agent."""

import io
import re
import socket
import subprocess
import sys
import threading
from datetime import timedelta
from functools import partial
from pathlib import Path

import httpx2 as httpx
import pytest
from git import Repo
from pydantic import SecretStr
from pytest_mock import MockerFixture
from sqlalchemy import select

import piloop
from piloop.actor import Agent, System
from piloop.agents.roles import IDEAS, PILOOP_HINT
from piloop.campaign.campaigns import checkout_of
from piloop.cli.main import app
from piloop.db.models import AgentSession, AgentSessionState, Campaign, Comment, Event
from piloop.db.session import Database
from piloop.ids import AgentSessionId, CampaignId, Revision
from piloop.net.tokens import mint_agent
from piloop.plugins.campaign_type.kit.evaluation.section import measure
from piloop.plugins.executor.contract import AgentJob, RepoId, Source
from piloop.plugins.executor.kit import door as door_module, jobs, relay as relay_module
from piloop.plugins.executor.kit.client import Dismissed
from piloop.plugins.executor.kit.door import RELAY_SECONDS, AgentCall, Call, Relayed, main
from piloop.plugins.executor.kit.leases import StaleLease
from piloop.plugins.executor.kit.relay import Relay, Runner, Shipped
from piloop.plugins.role.contract import RELAYED, Files
from piloop.plugins.role.orchestrator import OrchestratorRole
from piloop.plugins.runtime.contract import MESSAGES_ENV, QUEUE, put
from piloop.plugins.section.contract import SECTIONS, Guide
from piloop.plugins.vcs.contract import Repository, UnknownRevision
from piloop.plugins.vcs.git import Git
from piloop.proc import ACTOR_ENV, TEMPFAIL
from piloop.settings import AgentSettings, Settings
from tests.conftest import disk, no_ship
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
    roles = [p.read_text() for p in ROLE_PROMPTS if p.name != 'orchestrator.md']
    for_the_orchestrator = ('orchestration', 'research_commands')
    prompts = [*roles, *(guide.TEXT.read_text() for name, guide in GUIDES.items() if name not in for_the_orchestrator)]
    briefs = [
        f'`{PILOOP_HINT} sql -`',
        f'`{PILOOP_HINT} schema <guide>`',
        *(line.format(piloop='piloop', campaign='demo') for line in IDEAS.values()),
        measure.SUBMIT,
    ]
    named = set().union(*(commands(text) for text in [*prompts, *briefs]))
    assert named == RELAYED


def test_an_orchestrator_on_another_machine_may_run_the_commands_that_its_brief_names_and_no_other() -> None:
    assert OrchestratorRole.PROMPT is not None
    texts = [
        OrchestratorRole.PROMPT.read_text(),
        *(guide.TEXT.read_text() for name, guide in GUIDES.items() if name != 'orchestration'),
        f'`{PILOOP_HINT} schema <guide>`',
        f'`{PILOOP_HINT} sql -`',
        IDEAS[Files.IDEAS].format(piloop='piloop', campaign='demo'),
    ]
    assert set().union(*(commands(text) for text in texts)) == OrchestratorRole.RELAYED


def test_the_commands_that_run_are_those_of_the_agents_role(
    db: Database, campaign: Campaign, tmp_path: Path, runner: Runner
) -> None:
    watchdog = agent_job(db, campaign, tmp_path)
    with db.session() as s:
        orchestrator = AgentSession(kind='orchestrator', campaign_id=campaign.id)
        s.add(orchestrator)
        s.commit()
        job = watchdog.model_copy(update={'agent_session': orchestrator.id})
    refused = runner(watchdog, Call(argv=['inbox', 'show', '1'], token=launched(db, watchdog)))
    assert refused.exit_code == 2, 'not in the default list'
    shown = runner(job, Call(argv=['inbox', 'show', '1'], token=launched(db, job)))
    assert 'an agent on another machine runs only' not in shown.stderr, shown.stderr


@pytest.fixture
def runner(db: Database, tmp_path: Path) -> Runner:
    return Runner(db, tmp_path, partial(checkout_of, db))


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
    actor = Agent(role='watchdog', session=job.agent_session).signature
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


def test_a_command_that_is_not_relayed_is_refused_with_what_is(
    db: Database, campaign: Campaign, tmp_path: Path, runner: Runner
) -> None:
    done = runner(agent_job(db, campaign, tmp_path), Call(argv=['campaign', 'stop', 'demo'], token=TOKEN))
    assert done.exit_code == 2 and done.stderr.startswith('piloop campaign stop: an agent on another machine runs only')
    assert 'idea status' in done.stderr


def test_a_relayed_call_that_runs_too_long_is_stopped(
    db: Database, campaign: Campaign, tmp_path: Path, runner: Runner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(relay_module, 'RELAY_SECONDS', 0.2)
    monkeypatch.setattr(relay_module, 'PILOOP_ARGV', ('sh', '-c', 'sleep 5', 'piloop'))
    job = agent_job(db, campaign, tmp_path)
    done = runner(job, Call(argv=['sql', 'SELECT 1'], token=launched(db, job)))
    assert (done.exit_code, done.stderr) == (124, 'piloop: the call ran for 0.2 s on the server and was stopped\n')


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
    relay = Relay(Settings(home=tmp_path).relay_file, server, no_ship)
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
    restarted = Relay(Settings(home=tmp_path).relay_file, server, no_ship)
    restarted.path.touch()  # what a worker that was killed leaves
    restarted.start()
    restarted.open(4, JOB)
    assert post(restarted, call()).status_code == 200, 'a re-attached agent relays again, on the same path'
    restarted.stop()
    Relay(Settings(home=tmp_path).relay_file, server, no_ship).stop()


def test_the_piloop_of_an_agent_elsewhere_prints_what_the_call_printed_and_exits_with_its_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], mocker: MockerFixture
) -> None:
    server = Server()
    relay = Relay(Settings(home=tmp_path).relay_file, server, no_ship)
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
    with socket.socket(socket.AF_UNIX) as hung:  # a worker that dies while it reads the call
        hung.bind(str(relay.path))
        hung.listen()
        threading.Thread(target=lambda: hung.accept()[0].close(), daemon=True).start()
        with pytest.raises(SystemExit, match='the relay of this worker did not answer, and the call did not run'):
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


def test_a_submit_ships_the_version_of_the_agents_checkout_and_waits_across_calls(tmp_path: Path) -> None:
    """The server's checkout lacks the agent's commit, and a call runs at most RELAY_SECONDS: the relay sends the
    version through the CAS, and calls again while the verdict is not in."""
    answers = iter([Relayed(stdout='pending', exit_code=TEMPFAIL)] * 2 + [Relayed(stdout='keep 2', exit_code=0)])
    sent: list[tuple[int, Call]] = []
    shipped: list[str | None] = []

    def server(lease: int, relayed: Call) -> Relayed:
        sent.append((lease, relayed))
        return next(answers)

    def ship(job: AgentJob, revision: Revision | None) -> Shipped:
        shipped.append(revision)
        if revision == 'gone':
            raise UnknownRevision('no gone')
        return Shipped(Revision('abc'), None)

    relay = Relay(Settings(home=tmp_path).relay_file, server, ship)
    relay.start()
    relay.open(3, JOB)
    answered = Relayed.model_validate_json(post(relay, call(argv=['submit'])).content)
    assert answered == Relayed(stdout='keep 2', exit_code=0)
    assert [c.argv for _, c in sent] == [['submit', 'abc', '--wait', f'{RELAY_SECONDS / 2:g}']] * 3, 'shipped once'
    assert Relayed.model_validate_json(post(relay, call(argv=['submit', 'gone'])).content) == Relayed(
        stderr='piloop: the version did not leave this worker: no gone\n', exit_code=1
    )
    assert shipped == [None, 'gone'], 'the checkout head, else the named version'
    relay.stop()


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


def test_the_relay_calls_a_command_that_is_not_done_again_until_its_deadline(tmp_path: Path) -> None:
    def server(lease: int, relayed: Call) -> Relayed:
        return Relayed(stdout='pending', exit_code=TEMPFAIL)

    soon = AgentSettings(relay_deadline=timedelta(milliseconds=50))
    relay = Relay(Settings(home=tmp_path).relay_file, server, no_ship, soon)
    relay.start()
    relay.open(3, JOB)
    assert Relayed.model_validate_json(post(relay, call(argv=['sql'])).content) == Relayed(
        stdout='pending', exit_code=TEMPFAIL
    ), 'the agent gets the last answer: it may call again'
    relay.stop()
