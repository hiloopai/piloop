"""The ``piloop`` calls of an agent on a worker elsewhere: relayed by its worker, run on the server as the agent."""

import io
import re
import socket
import subprocess
import sys
import threading
from collections.abc import Sequence
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
from piloop.agents.roles import PILOOP_HINT
from piloop.campaign.campaigns import checkout_of
from piloop.cli.main import app
from piloop.db.models import AgentSession, AgentSessionState, Campaign, Comment, Event
from piloop.db.session import Database
from piloop.ids import AgentSessionId, CampaignId, Revision
from piloop.net.tokens import mint_agent
from piloop.plugins.campaign_type.kit.backlog.cli import land
from piloop.plugins.campaign_type.kit.backlog.wire import Base
from piloop.plugins.campaign_type.kit.candidates.ids import CandidateId
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.evaluation.section import measure
from piloop.plugins.executor.contract import AgentJob, RepoId, Source
from piloop.plugins.executor.kit import door as door_module, jobs, relay as relay_module
from piloop.plugins.executor.kit.client import Dismissed
from piloop.plugins.executor.kit.door import AgentCall, Call, Relayed, main
from piloop.plugins.executor.kit.leases import StaleLease
from piloop.plugins.executor.kit.relay import Relay, Runner, Shipped
from piloop.plugins.role.contract import RELAYED, RoleConfig
from piloop.plugins.role.orchestrator import OrchestratorRole
from piloop.plugins.runtime.contract import MESSAGES_ENV, QUEUE, put
from piloop.plugins.section.contract import SECTIONS, Guide
from piloop.plugins.vcs.contract import Repository, UnknownRevision, VcsError
from piloop.plugins.vcs.git import Git
from piloop.proc import ACTOR_ENV, CHECKOUT_ENV
from piloop.settings import Settings
from tests.conftest import baseline, disk, no_land, no_ship, no_take
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
    roles = [p.read_text() for p in ROLE_PROMPTS if p.name != 'orchestrator.md']
    for_the_orchestrator = ('orchestration', 'research_commands')
    prompts = [*roles, *(guide.TEXT.read_text() for name, guide in GUIDES.items() if name not in for_the_orchestrator)]
    briefs = [
        f'`{PILOOP_HINT} sql -`',
        f'`{PILOOP_HINT} schema <guide>`',
        RoleConfig.IDEAS,
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
        OrchestratorRole.IDEAS.format(piloop='piloop', campaign='demo'),
    ]
    assert set().union(*(commands(text) for text in texts)) == OrchestratorRole.RELAYED


def test_the_commands_that_run_are_those_of_the_agents_role(
    db: Database, campaign: Campaign, tmp_path: Path, runner: Runner
) -> None:
    worker = agent_job(db, campaign, tmp_path)
    with db.session() as s:
        orchestrator = AgentSession(kind='orchestrator', campaign_id=campaign.id)
        s.add(orchestrator)
        s.commit()
        job = worker.model_copy(update={'agent_session': orchestrator.id})
    refused = runner(worker, Call(argv=['inbox', 'show', '1'], token=launched(db, worker)))
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


def test_a_command_that_is_not_relayed_is_refused_with_what_is(
    db: Database, campaign: Campaign, tmp_path: Path, runner: Runner
) -> None:
    done = runner(agent_job(db, campaign, tmp_path), Call(argv=['campaign', 'stop', 'demo'], token=TOKEN))
    assert done.exit_code == 2 and done.stderr.startswith('piloop campaign stop: an agent on another machine runs only')
    assert 'idea claim' in done.stderr


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
    monkeypatch.setattr(relay_module, 'PILOOP_ARGV', ('sh', '-c', f'printf abc > "${CHECKOUT_ENV}"', 'piloop'))
    job = agent_job(db, campaign, tmp_path)
    token = launched(db, job)
    assert runner(job, Call(argv=['idea', 'claim', '1'], token=token)).checkout == 'abc'
    monkeypatch.setattr(relay_module, 'PILOOP_ARGV', ('sh', '-c', 'true', 'piloop'))
    assert runner(job, Call(argv=['idea', 'claim', '1'], token=token)).checkout is None


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
