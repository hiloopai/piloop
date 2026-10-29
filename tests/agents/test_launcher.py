import json
import os
import re
import threading
import time
import tomllib
from pathlib import Path

import pytest
from covalence import Cid
from dirty_equals import IsFloat
from git import Repo
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from pydantic import SecretStr
from pytest_mock import MockerFixture
from sqlalchemy import select
from sqlalchemy.orm import Session

from piloop.actor import Agent, System
from piloop.agents.launcher import Launcher, fill
from piloop.agents.results import (
    AgentSessionFinished,
    AgentSessionRunning,
    AgentSessionStarted,
    DaemonStopped,
    Ended,
    JobEnded,
    NotStarted,
    Ran,
    SessionSaved,
    StartupAnomaly,
    WrapUp,
    never_worked,
)
from piloop.agents.roles import (
    PILOOP_HINT,
    Role,
    briefing,
    compose_brief,
    compose_resumed,
    read_brief,
    render_sections,
    role,
)
from piloop.config import CampaignConfig, ConfigError, load_campaign_config
from piloop.db.events import emit, in_order
from piloop.db.models import AgentSession, AgentSessionState, Campaign, Comment, Event, Lease, utcnow
from piloop.db.session import Database
from piloop.db.triggers.changelog import AgentSessionRequested
from piloop.ids import AgentSessionId, CampaignId, RoleName, TargetKind, TargetRef
from piloop.otel import Tracer
from piloop.plugins.campaign_type.kit.runs.controller import RunController
from piloop.plugins.campaign_type.kit.runs.ids import IMPLEMENTER
from piloop.plugins.campaign_type.kit.runs.role.watchdog import WatchdogRole
from piloop.plugins.cas.contract import CAS
from piloop.plugins.executor.contract import AGENT, AgentJob, HarnessSession, RepoId, Source
from piloop.plugins.executor.kit import jobs, sessions
from piloop.plugins.executor.kit.capacity import HostSample
from piloop.plugins.executor.kit.leases import SERVE, Answer, LeaseQueued, Leases, LeaseWorkerJoined, read_job
from piloop.plugins.executor.kit.sessions import SessionFile
from piloop.plugins.executor.workers import Workers
from piloop.plugins.harness.contract import MCP, PROMPT, SESSION_ID, Usage, UserConfig, each_provider
from piloop.plugins.harness.kit.jsonl import dash_separators_wrapped
from piloop.plugins.role.contract import FIRST_MESSAGE
from piloop.plugins.runtime.contract import AgentExit, AgentSpec, ExitStatus
from piloop.plugins.secrets.contract import SECRETS, set_secret
from piloop.plugins.section.context import Context
from piloop.plugins.section.contract import Rendering
from piloop.plugins.vcs.contract import Part
from piloop.plugins.vcs.git import Git
from piloop.settings import AgentSettings, Settings
from piloop.store.chunks import read
from tests.conftest import (
    HARNESS_FAILS,
    HARNESS_PROMPT_TO_FILE,
    HARNESS_SLEEPS,
    RESEARCH,
    SCOUT,
    WATCHDOG,
    ScriptRuntime,
    cas_of,
    events,
    factory,
    home_cas,
    reconfigured,
    role_timeout,
    serving,
    serving_api,
    wait_until,
    write_toml,
)
from tests.test_vcs import ALLOWED_WORDS, git_words
from tests.usage.test_usage import READS_PI, SESSIONS

NO_USAGE_FILE = {'provider': 'pi', 'home': '/nonexistent', 'resume': []}
"""A harness that reads usage from files that are never there: it only makes the harness need its session id."""
MINTED_HARNESS = {'headless': ['--session-id', '{session_id}', '{prompt}'], **NO_USAGE_FILE}
CODEX_ID = '01a09262-d574-7e10-9f75-23b0ce9c0a2f'
CODEX_LIKE = f'#!/bin/sh\necho \'{{"type":"thread.started","thread_id":"{CODEX_ID}"}}\'\nsleep 0.3\necho more\n'
"""``codex exec --json``: the thread id is the first stdout line, then the run's events."""

UUID = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$')


def role_in(campaign: Campaign, kind: RoleName = WATCHDOG) -> Role:
    return role(campaign.config, kind, Path(campaign.path))


def brief_in(
    s: Session,
    campaign: Campaign,
    kind: RoleName = WATCHDOG,
    *,
    lines: tuple[str, ...] = (),
    incident: bool = False,
    resumed: bool = False,
) -> str:
    """The brief of agent ``<kind>#7``, started for ``timer``, with ``lines`` as its type's context: whole, or resumed."""
    agent = Agent(role=kind, session=7)
    brief = briefing(role_in(campaign, kind), campaign, agent, reason='timer', lines=lines)
    r = Rendering(s, utcnow(), campaign, Path('/no/workspace'), cas_of(s), brief=brief)
    sections = render_sections(campaign.config.sections_of(kind), r)
    if resumed:
        return compose_resumed(campaign.config.role(kind), agent, sections.resumed)
    return compose_brief(role_in(campaign, kind), campaign, sections.whole, incident=incident)


def request(db: Database, campaign: Campaign, kind: RoleName = WATCHDOG, **fields: object) -> AgentSessionId:
    """A requested row, on ``script`` unless ``fields`` names a harness: the pass that takes a row chooses one."""
    fields.setdefault('harness', 'script')
    with db.session() as s:
        agent_session = AgentSession(kind=kind, campaign_id=campaign.id, reason='test', **fields)
        s.add(agent_session)
        s.commit()
        return agent_session.id


NEVER = never_worked(AgentSettings().startup_seconds)
"""The error of an agent that ended at once and left nothing."""


def controller(
    db: Database,
    runtime: ScriptRuntime,
    tmp_path: Path,
    tracer: Tracer | None = None,
    agents: AgentSettings | None = None,
) -> RunController:
    """The launcher and the in-process worker that takes its leases, as the daemon makes them."""
    return serving(
        RunController(db, tracer or Tracer(None), tmp_path, factory(runtime), executor=Workers(), agents=agents)
    )


def launcher(db: Database, runtime: ScriptRuntime, tmp_path: Path, tracer: Tracer | None = None) -> Launcher:
    return controller(db, runtime, tmp_path, tracer).launcher


def launch(
    db: Database,
    runtime: ScriptRuntime,
    tmp_path: Path,
    agent_session_id: AgentSessionId,
    tracer: Tracer | None = None,
    agents: AgentSettings | None = None,
):
    with db.session() as s:
        campaign = s.get_one(Campaign, s.get_one(AgentSession, agent_session_id).campaign_id)
        brief = compose_brief(role_in(campaign), campaign, '## Context')
    ctl = controller(db, runtime, tmp_path, tracer, agents)
    ended = ctl.launcher.launch(
        agent_session_id,
        brief=ctl.cas.put(brief.encode()),
        place='w',
        artifact=checkout(campaign),
        timeout=role_timeout(db, agent_session_id),
    )
    # its worker heartbeats until it stops: left running, it takes the next launch's job
    ctl.stop(5)
    return ended


def exit_of(ended: Ended) -> AgentExit:
    """How the agent's process ended: the agent ran."""
    assert isinstance(ended, Ran), ended
    return ended.process


def checkout(campaign: Campaign) -> Source:
    """The campaign's head as the artifact: the agent runs in a worktree of it, and what the test harnesses write there
    is ignored by git, so it is no change of the agent's."""
    (Path(campaign.path) / '.git' / 'info' / 'exclude').write_text('*.txt\njson.py\npiloop/\n')
    return Source(vcs=Git(), repo=RepoId(campaign.id), revision=Repo(campaign.path).head.commit.hexsha)


def worktree(tmp_path: Path) -> Path:
    """The worktree that an agent of ``launch`` works in: it goes when the job ends, so harnesses write in its parent."""
    return tmp_path / 'w' / 'worktree'


def agent_session(db: Database, agent_session_id: AgentSessionId) -> AgentSession:
    with db.session() as s:
        return s.get_one(AgentSession, agent_session_id)


def use_harness(db: Database, campaign: Campaign, body: str, **definition: object) -> None:
    """Point the test harness at ``body``; ``definition`` overrides keys of its ``[plugins.harness.script]`` table."""
    write_toml(Path(campaign.path), body)
    script = {
        **load_campaign_config(Path(campaign.path)).harness['script'].model_dump(mode='json', exclude_unset=True),
        **definition,
    }
    with db.session() as s:
        s.get_one(Campaign, campaign.id).config = load_campaign_config(
            Path(campaign.path), plugins={'harness': {'script': script}}
        )
        s.commit()


def context_of(brief: str) -> str:
    """The text of a brief's context section."""
    start = brief.index('## Context\n\n') + len('## Context\n\n')
    end = brief.find('\n\n## ', start)
    return brief[start : None if end < 0 else end]


def test_brief_is_role_prompt_campaign_brief_guides_then_the_context_section(campaign: Campaign, db: Database) -> None:
    with db.session() as s:
        brief = brief_in(s, s.get_one(Campaign, campaign.id), lines=('- extra line',))
    assert WatchdogRole.PROMPT is not None
    assert brief.startswith(WatchdogRole.PROMPT.read_text()[:40])
    assert "The orchestrator's procedures" not in brief and 'piloop pause' not in brief, 'only `piloop schema` has them'
    assert (
        brief.index('# Watchdog')
        < brief.index('# Demo')
        < brief.index('## Working with the piloop')
        < brief.index('## Context')
    )
    assert '# Guides' not in brief and '\n# Context' not in brief, 'every section is a ## heading'
    assert context_of(brief) == (
        f"""- You are watchdog#7, the watchdog for campaign#1 (demo).
- Campaign repo: {campaign.path}, on the daemon's machine. Your checkout can read every candidate's version.
- Sign what you write as 'watchdog#7' (comments.author, ideas.created_by).
- Why this agent started (context, not a scope): timer
- piloop: `{PILOOP_HINT}` is this daemon's piloop. Run every `piloop` command of this brief with it.
- SQL tool: `{PILOOP_HINT} sql -`, with the statement on stdin from a quoted heredoc (`<<'EOF'`).
- You may write: comments(escalation, finding, note), lessons on the campaign once it stopped, seeds via idea seed.
- `{PILOOP_HINT} schema <guide>` prints another guide: links, research_commands, provenance, queries, orchestration, long_jobs, workspace.
- File a seed: `{PILOOP_HINT} idea seed demo '<hunch, one line>' --from '<pointers: ids, paths, numbers>'` (`--base\
 <candidate>` when it is about that tree): research checks it against the existing work and develops it into an idea,\
 or rejects it with the reason.
- extra line"""
    )


def test_a_brief_has_its_roles_guides_before_the_context_and_names_the_others(campaign: Campaign, db: Database) -> None:
    with db.session() as s:
        p = s.get_one(Campaign, campaign.id)
        research = brief_in(s, p, RESEARCH)
        resumed = brief_in(s, p, RESEARCH, resumed=True)
    guides = research[research.index('## Working with the piloop database') : research.index('## Context')]
    assert [line for line in guides.splitlines() if line.startswith('## ')] == [
        '## Working with the piloop database',
        '## The model',
        '## Judge results',
        '## Talk',
        '## Search',
        '## Remember past campaigns',
        '## Link ideas',
    ]
    assert (
        f'- `{PILOOP_HINT} schema <guide>` prints another guide: priorities, research_commands, provenance, queries, orchestration, long_jobs, workspace.'
        in (context_of(research))
    )
    assert resumed.startswith('You continue this session as a new agent, research#7.')
    assert '## Working with the piloop database' not in resumed and '## Context' in resumed, (
        'the resumed session has the guides already'
    )


def test_the_context_is_a_section_that_a_list_moves_and_its_lines_configure(campaign: Campaign, db: Database) -> None:
    """A line whose field is empty is left out: the scout writes nothing that its brief lists."""
    plugins = {
        'section': {'context': {'lines': ['- You are {agent}, in {repo}.', '- You may write: {writes}.']}},
        'role': {'scout': {'writes': [], 'sections': ['goal', 'context', 'database']}},
    }
    with db.session() as s:
        p = s.get_one(Campaign, campaign.id)
        p.config = load_campaign_config(Path(campaign.path), plugins=plugins)
        brief = brief_in(s, p, SCOUT, lines=('- the type',))
    assert brief.index('## Goal') < brief.index('## Context') < brief.index('## Working with the piloop database')
    with db.session() as s:
        status = Rendering(s, utcnow(), s.get_one(Campaign, campaign.id), Path('/no/workspace'), cas_of(s))
        assert Context().render(status) is None, 'no agent reads piloop status'
    context = context_of(brief).splitlines()
    assert (context[0], context[-1]) == (f'- You are scout#7, in {campaign.path}.', '- the type')
    assert not any(line.startswith('- You may write') for line in context)
    with pytest.raises(ConfigError, match=r"'- \{who\}' has the field who: use agent, role,"):
        load_campaign_config(Path(campaign.path), plugins={'section': {'context': {'lines': ['- {who}']}}})


def test_a_list_without_the_context_shows_it_after_its_guides(campaign: Campaign, db: Database) -> None:
    """A list that 0.3 wrote: the guides, the context and the rest, in the order of 0.3's briefs."""
    with db.session() as s:
        p = s.get_one(Campaign, campaign.id)
        p.config = load_campaign_config(
            Path(campaign.path), plugins={'role': {'scout': {'sections': ['goal', 'database', 'search']}}}
        )
        brief = brief_in(s, p, SCOUT)
    assert (
        brief.index('## Working with the piloop database')
        < brief.index('## Search')
        < brief.index('## Context')
        < brief.index('## Goal')
    )


def test_the_example_queries_of_a_brief_read_its_own_campaign_and_primary(campaign: Campaign, db: Database) -> None:
    with db.session() as s:
        first = s.get_one(Campaign, campaign.id)
        second = Campaign(name='second', path=first.path, config=first.config)
        s.add(second)
        s.flush()
        brief = brief_in(s, second)
    assert second.id == 2
    assert 'i.campaign_id = 2 ' in brief and 'c.campaign_id = 2 ' in brief and 'WHERE id = 2)' in brief
    assert "json_extract(m.metrics, '$.score')" in brief, 'the primary of the campaign, not of another example'
    procedures = brief[brief.index('## Working with the piloop database') : brief.index('## Context')]
    assert 'campaign_id = 1' not in procedures and '<campaign id>' not in procedures, procedures


@pytest.mark.parametrize('kind', [IMPLEMENTER, WATCHDOG, RESEARCH, SCOUT])
@pytest.mark.parametrize('incident', [False, True])
def test_a_brief_says_git_or_commit_only_in_the_vcs_providers_parts(
    campaign: Campaign, db: Database, mocker: MockerFixture, kind: RoleName, incident: bool
) -> None:
    def marked(self: Git, part: Part) -> str:
        return f'<{part}>'

    mocker.patch.object(Git, 'prompt', marked)
    with db.session() as s:
        brief = brief_in(s, s.get_one(Campaign, campaign.id), kind, incident=incident)
    for phrase, _ in ALLOWED_WORDS.values():
        brief = brief.replace(phrase, '')
    assert git_words(brief) == []


def test_a_roles_addendum_follows_the_campaign_brief_in_that_roles_briefs_only(
    campaign: Campaign, db: Database
) -> None:
    (Path(campaign.path) / 'research.md').write_text('# Search wide first\n')
    with db.session() as s:
        p = s.get_one(Campaign, campaign.id)
        p.config = load_campaign_config(
            Path(campaign.path), plugins={'role': {'research': {'addendum': 'research.md'}}}
        )
        research = brief_in(s, p, RESEARCH)
        watchdog = brief_in(s, p)
    assert (
        research.index('# Demo') < research.index('# Search wide first') < research.index('## Working with the piloop')
    )
    assert '# Search wide first' not in watchdog


def test_a_roles_first_message_names_its_brief(
    campaign: Campaign, db: Database, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    use_harness(db, campaign, HARNESS_PROMPT_TO_FILE.replace('prompt.txt', '../prompt.txt'))
    with db.session() as s:
        s.get_one(Campaign, campaign.id).config = load_campaign_config(
            Path(campaign.path), plugins={'role': {'watchdog': {'first_message': 'Read {brief}, then work.'}}}
        )
        s.commit()
    exit_of(launch(db, runtime, tmp_path, request(db, campaign)))
    assert (tmp_path / 'w' / 'prompt.txt').read_text() == f'Read {tmp_path / "w" / "brief.md"}, then work.'
    with pytest.raises(ConfigError, match=r"first_message = 'Read it': 'Read it' names no \{brief\}"):
        load_campaign_config(Path(campaign.path), plugins={'role': {'watchdog': {'first_message': 'Read it'}}})
    resumes = load_campaign_config(
        Path(campaign.path), plugins={'role': {'watchdog': {'resume_message': 'Go, {agent}.'}}}
    )
    assert resumes.role(WATCHDOG).resume_message == 'Go, {agent}.'
    with pytest.raises(ConfigError, match=r"'\{who\} goes on' has who: only \{agent\} is filled in"):
        load_campaign_config(Path(campaign.path), plugins={'role': {'watchdog': {'resume_message': '{who} goes on'}}})


def test_launch_runs_the_row_through_its_lifecycle(
    campaign: Campaign,
    db: Database,
    runtime: ScriptRuntime,
    tmp_path: Path,
    tracer: Tracer,
    spans: InMemorySpanExporter,
) -> None:
    use_harness(db, campaign, HARNESS_PROMPT_TO_FILE.replace('prompt.txt', '../prompt.txt'))
    agent_session_id = request(db, campaign)
    outcome = launch(db, runtime, tmp_path, agent_session_id, tracer)
    process = exit_of(outcome)
    assert (process.status, process.exit_code, process.error) == (ExitStatus.EXITED, 0, None)
    w = agent_session(db, agent_session_id)
    assert (w.state, w.harness, w.model, w.runtime, w.log_dir, w.error) == (
        AgentSessionState.EXITED,
        'script',
        'watchdog-model',
        'subprocess',
        str(tmp_path / 'w'),
        None,
    )
    assert w.handle['pid'] and w.ended_at is not None and w.ended_at >= w.started_at
    assert (tmp_path / 'w' / 'prompt.txt').read_text() == FIRST_MESSAGE.format(brief=tmp_path / 'w' / 'brief.md')
    assert (tmp_path / 'w' / 'brief.md').read_text().startswith('# Watchdog')
    assert (tmp_path / 'w' / 'transcript.txt').is_file()
    assert runtime.killed, 'the runtime is told to release the implementer'
    with db.session() as s:
        events = [(e.target_id, e.payload) for e in s.scalars(select(Event).order_by(Event.id))]
    assert [e for e in events if not isinstance(e[1], JobEnded)] == [
        (1, AgentSessionRequested(role=WATCHDOG, campaign=campaign.id, reason='test')),
        (1, LeaseWorkerJoined(worker=SERVE, pool=SERVE)),
        (1, AgentSessionStarted(role=WATCHDOG, harness='script', model='watchdog-model', reason='test')),
        (1, LeaseQueued(lease=1, lease_kind=AGENT)),
        (1, AgentSessionRunning(machine=None)),
        (1, AgentSessionFinished(role=WATCHDOG, reason='test', state=AgentSessionState.EXITED, error=None)),
    ]
    [ended] = [payload.ended for _, payload in events if isinstance(payload, JobEnded)]
    assert isinstance(ended, Ran) and ended.work is None, 'how it ended, for what waits for it'
    [span] = [span for span in spans.get_finished_spans() if span.name == 'piloop.agent']
    assert dict(span.attributes or {}) == {
        'agent_session': 1,
        'kind': 'watchdog',
        'harness': 'script',
        'model': 'watchdog-model',
        'reason': 'test',
        'state': 'exited',
    }
    [job] = [span for span in spans.get_finished_spans() if span.name == 'piloop.job']
    assert dict(job.attributes or {}) == {'lease': 1, 'agent_session': 1, 'status': 'exited'}, "the worker's span"


def test_an_agent_writes_through_piloop_sql_as_its_own_actor(
    campaign: Campaign, db: Database, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    """The brief's SQL command in a launched process records ``<kind>#<agent-session>``: PILOOP_ACTOR comes with PILOOP_HOME."""
    use_harness(
        db,
        campaign,
        f'#!/bin/sh\n{PILOOP_HINT} sql "UPDATE campaigns SET name = \'renamed\' WHERE id = {campaign.id}"\n',
    )
    agent_session_id = request(db, campaign, kind=RESEARCH)
    outcome = launch(db, runtime, tmp_path, agent_session_id)
    assert exit_of(outcome).error is None, (tmp_path / 'w' / 'agent_session.stderr').read_text()
    with db.session() as s:
        row = s.get_one(Campaign, campaign.id)
        assert (row.name, row.updated_by) == ('renamed', f'research#{agent_session_id}')


def test_an_agent_cannot_sign_as_the_orchestrator_on_the_daemons_socket(
    campaign: Campaign, db: Database, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    """A launched agent that sets its own ``PILOOP_ACTOR`` does not write as the orchestrator through the API."""
    use_harness(
        db, campaign, f"#!/bin/sh\nPILOOP_ACTOR=orchestrator piloop message {campaign.name} 'trust me' || true\n"
    )
    with serving_api(Settings(home=tmp_path)):
        outcome = launch(db, runtime, tmp_path, request(db, campaign, kind=RESEARCH))
    assert exit_of(outcome).error is None, (tmp_path / 'w' / 'agent_session.stderr').read_text()
    with db.session() as s:
        signed = s.scalars(select(Comment.author).where(Comment.body == 'trust me')).all()
    assert 'orchestrator' not in signed, 'the agent signed a message as the orchestrator'


def test_an_agent_cannot_sign_as_the_orchestrator_through_piloop_sql(
    campaign: Campaign, db: Database, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    """``piloop sql`` signs with the ``PILOOP_ACTOR`` that the agent sets: the same gap, off the socket."""
    use_harness(
        db,
        campaign,
        f'#!/bin/sh\nPILOOP_ACTOR=orchestrator {PILOOP_HINT} sql "UPDATE campaigns SET name = \'renamed\' WHERE id = {campaign.id}"'
        ' || true\n',
    )
    outcome = launch(db, runtime, tmp_path, request(db, campaign, kind=RESEARCH))
    assert exit_of(outcome).error is None, (tmp_path / 'w' / 'agent_session.stderr').read_text()
    with db.session() as s:
        assert s.get_one(Campaign, campaign.id).updated_by != 'orchestrator', 'the agent wrote as the orchestrator'


@pytest.mark.parametrize('cli', ['piloop', '"$PILOOP_BIN"', PILOOP_HINT])
def test_an_agents_piloop_is_the_daemons_whatever_comes_first_on_path_or_sits_in_its_cwd(
    cli: str, campaign: Campaign, db: Database, runtime: ScriptRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The prompts say `piloop`, the brief `"$PILOOP_BIN"`: neither another install nor the campaign's code answers.

    A second install (a release next to a build of main) comes first on the PATH. The agent's cwd is the campaign's
    worktree, and a campaign's own ``json.py`` or ``piloop/`` must not shadow piloop's imports (``python -P``).
    """
    other = tmp_path / 'other-bin'
    other.mkdir()
    (other / 'piloop').write_text('#!/bin/sh\necho "another piloop" >&2\nexit 9\n')
    (other / 'piloop').chmod(0o755)
    monkeypatch.setenv('PATH', f'{other}:{os.environ["PATH"]}')
    cwd = Path(campaign.path)
    (cwd / 'piloop').mkdir(parents=True)
    for module in (cwd / 'json.py', cwd / 'piloop' / '__init__.py'):
        module.write_text("raise SystemExit('SHADOWED')\n")
    use_harness(
        db, campaign, f'#!/bin/sh\n{cli} sql "UPDATE campaigns SET name = \'renamed\' WHERE id = {campaign.id}"\n'
    )

    agent_session_id = request(db, campaign, kind=RESEARCH)
    outcome = launch(db, runtime, tmp_path, agent_session_id)
    assert exit_of(outcome).error is None, (tmp_path / 'w' / 'agent_session.stderr').read_text()
    with db.session() as s:
        assert s.get_one(Campaign, campaign.id).name == 'renamed'


def test_harness_exit_code_is_recorded_not_raised(
    campaign: Campaign, db: Database, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    use_harness(db, campaign, HARNESS_FAILS)
    agent_session_id = request(db, campaign)
    outcome = launch(db, runtime, tmp_path, agent_session_id)
    w = agent_session(db, agent_session_id)
    assert (exit_of(outcome).error, exit_of(outcome).exit_code, w.state, w.error) == (
        'agent exit 3',
        3,
        AgentSessionState.EXITED,
        'agent exit 3',
    )


def test_an_agent_that_exits_non_zero_says_its_last_output_bounded_and_without_a_secret_value(
    campaign: Campaign, db: Database, runtime: ScriptRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A harness that dies says why on its last lines: the row's error, and so the escalation of a pause, carries them."""
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    dies = '#!/bin/sh\necho starting\necho\necho "401 Unauthorized: key $GATEWAY_KEY rejected"\necho "check the key"\nexit 1\n'
    counted = {'headless': ['--session-id', '{session_id}', '{prompt}'], **READS_PI}
    use_harness(db, campaign, dies, env={'GATEWAY_KEY': {'secret': 'TEAM_KEY'}}, **counted)
    put_secret(db, campaign.id, 'TEAM_KEY', 'sk-live-123')
    agent_session_id = request(db, campaign)
    ended = launch(db, runtime, tmp_path, agent_session_id, agents=AgentSettings(exit_output_chars=60))
    said = '…Unauthorized: key [secret TEAM_KEY] rejected | check the key'
    assert (ended, agent_session(db, agent_session_id).error) == (
        NotStarted(f'agent exit 1: {said}'),
        f'agent exit 1: {said}',
    )
    assert [a.error for _, a in anomalies(db)] == [f'agent exit 1: {said}'], 'what the escalation of a pause lists'
    assert 'sk-live-123' in (tmp_path / 'w' / 'transcript.txt').read_text(), 'the transcript keeps what it printed'


def test_an_agent_that_ends_early_with_exit_0_and_nothing_done_says_its_last_output(
    campaign: Campaign, db: Database, runtime: ScriptRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A helper binary missing from the image: the agent says so and exits 0, and its error must say it too."""
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    dies = '#!/bin/sh\necho starting\necho "error: helper binary not found at $GATEWAY_KEY"\nexit 0\n'
    counted = {'headless': ['--session-id', '{session_id}', '{prompt}'], **READS_PI}
    use_harness(db, campaign, dies, env={'GATEWAY_KEY': {'secret': 'TEAM_KEY'}}, **counted)
    put_secret(db, campaign.id, 'TEAM_KEY', 'sk-live-123')
    agent_session_id = request(db, campaign)
    ended = launch(db, runtime, tmp_path, agent_session_id, agents=AgentSettings(exit_output_chars=200))
    said = f'{NEVER}: starting | error: helper binary not found at [secret TEAM_KEY]'
    assert [a.error for _, a in anomalies(db)] == [said], 'what the escalation of a pause lists'
    assert (ended, agent_session(db, agent_session_id).error) == (NotStarted(said), None), 'the agent itself exited 0'


def test_an_agent_that_works_and_exits_0_has_no_error(
    campaign: Campaign, db: Database, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    use_harness(db, campaign, '#!/bin/sh\necho done\necho line >> solve.txt\n')
    agent_session_id = request(db, campaign)
    launch(db, runtime, tmp_path, agent_session_id)
    assert agent_session(db, agent_session_id).error is None, 'its last output is not an error'


def test_timeout_kills_and_records(campaign: Campaign, db: Database, runtime: ScriptRuntime, tmp_path: Path) -> None:
    write_toml(Path(campaign.path), HARNESS_SLEEPS)
    cfg = load_campaign_config(Path(campaign.path), plugins={'role': {'watchdog': {'timeout': 0.3}}})
    with db.session() as s:
        s.get_one(Campaign, campaign.id).config = cfg
        s.commit()
    agent_session_id = request(db, campaign)
    outcome = launch(db, runtime, tmp_path, agent_session_id)
    w = agent_session(db, agent_session_id)
    assert (exit_of(outcome).status, w.state, w.error) == (
        ExitStatus.TIMED_OUT,
        AgentSessionState.KILLED,
        'agent timeout after 0.3s',
    )


def test_runtime_failure_is_isolated(
    campaign: Campaign,
    db: Database,
    runtime: ScriptRuntime,
    tmp_path: Path,
    mocker: MockerFixture,
    tracer: Tracer,
    spans: InMemorySpanExporter,
) -> None:
    mocker.patch.object(runtime, 'start', side_effect=RuntimeError('no server'))
    agent_session_id = request(db, campaign)
    outcome = launch(db, runtime, tmp_path, agent_session_id, tracer)
    w = agent_session(db, agent_session_id)
    assert (outcome, w.state, w.handle, w.error) == (
        NotStarted('RuntimeError: no server'),
        AgentSessionState.KILLED,
        {},
        'RuntimeError: no server',
    )
    [job] = [span for span in spans.get_finished_spans() if span.name == 'piloop.job']
    assert job.status.status_code is StatusCode.ERROR, "the worker's span has the error"
    assert span_attributes(spans, 'piloop.agent', agent_session_id)['state'] == 'killed'
    with db.session() as s:
        [(_, finished)] = in_order(s, AgentSessionFinished)
    assert (finished.role, finished.error) == (WATCHDOG, 'RuntimeError: no server')


def anomalies(db: Database) -> list[tuple[int, StartupAnomaly]]:
    with db.session() as s:
        return [(e.target_id, anomaly) for e, anomaly in in_order(s, StartupAnomaly)]


def test_an_agent_that_never_got_going_is_an_anomaly(
    campaign: Campaign,
    db: Database,
    runtime: ScriptRuntime,
    tmp_path: Path,
    mocker: MockerFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A start that fails, or an agent that ends at once without work, is a startup anomaly, not a finished agent.

    A missing harness fails the start. A harness that cannot work (no quota, broken tools) ends the agent quickly, as
    if it had finished.
    """
    mocker.patch.object(runtime, 'start', side_effect=RuntimeError('no such harness'))
    dialog = request(db, campaign)
    outcome = launch(db, runtime, tmp_path, dialog)
    assert (isinstance(outcome, NotStarted), agent_session(db, dialog).state) == (True, AgentSessionState.KILLED)
    [(on, anomaly)] = anomalies(db)
    assert (on, anomaly.model_dump(exclude={'kind'})) == (
        dialog,
        {
            'role': 'watchdog',
            'harness': 'script',
            'subject': None,
            'seconds': IsFloat(ge=0),
            'error': 'RuntimeError: no such harness',
        },
    )
    mocker.stopall()

    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    counted = {'headless': ['--session-id', '{session_id}', '{prompt}'], **READS_PI}
    use_harness(db, campaign, HARNESS_PROMPT_TO_FILE, **counted)
    idle_at_once = request(db, campaign)
    outcome = launch(db, runtime, tmp_path, idle_at_once)
    assert (outcome, agent_session(db, idle_at_once).state) == (NotStarted(NEVER), AgentSessionState.EXITED)
    assert [w for w, _ in anomalies(db)] == [dialog, idle_at_once], 'ended within a minute without a token'

    worked = AgentSettings(startup_seconds=0)
    assert not isinstance(launch(db, runtime, tmp_path, request(db, campaign), agents=worked), NotStarted), (
        'past the startup window'
    )
    monkeypatch.undo()
    use_harness(db, campaign, HARNESS_PROMPT_TO_FILE)
    assert not isinstance(launch(db, runtime, tmp_path, request(db, campaign)), NotStarted), (
        'a harness whose usage is not read cannot tell "spent nothing" from "unknown"'
    )
    assert len(anomalies(db)) == 2


def test_an_agent_told_to_wrap_up_that_stops_with_nothing_is_not_a_failed_start(
    campaign: Campaign, db: Database, runtime: ScriptRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cancel or a stop with the wrap-up prompt: the agent stopped because it was told to, not at startup."""
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    counted = {'headless': ['--session-id', '{session_id}', '{prompt}'], **READS_PI}
    use_harness(db, campaign, HARNESS_PROMPT_TO_FILE, **counted)
    told = request(db, campaign)
    with db.session() as s:
        emit(s, WrapUp(reason='cancelled'), TargetRef(TargetKind.AGENT_SESSION, told), System.DAEMON)
        s.commit()
    outcome = launch(db, runtime, tmp_path, told)
    assert (isinstance(outcome, NotStarted), agent_session(db, told).state, anomalies(db)) == (
        False,
        AgentSessionState.EXITED,
        [],
    )


def test_a_cwd_that_cannot_be_made_is_a_failed_start(
    campaign: Campaign, db: Database, runtime: ScriptRuntime, tmp_path: Path, mocker: MockerFixture
) -> None:
    """The worker makes the artifact (a worktree) as part of the start: when it fails, the agent did not start to work."""
    start = mocker.spy(runtime, 'start')
    agent_session_id = request(db, campaign)
    brief = compose_brief(role_in(campaign), campaign, '## Context')
    mocker.patch('piloop.plugins.vcs.git.commands.add_worktree', side_effect=OSError(28, 'No space left on device'))
    lc = launcher(db, runtime, tmp_path)
    artifact = Source(vcs=Git(), repo=RepoId(campaign.id), revision=Repo(campaign.path).head.commit.hexsha)
    outcome = lc.launch(
        agent_session_id, brief=home_cas(db).put(brief.encode()), place='w', artifact=artifact, timeout=60
    )
    w = agent_session(db, agent_session_id)
    assert (isinstance(outcome, NotStarted), w.state, w.error) == (
        True,
        AgentSessionState.KILLED,
        'OSError: [Errno 28] No space left on device',
    )
    assert [target for target, _ in anomalies(db)] == [agent_session_id] and not start.called


def test_a_brief_that_the_cas_lacks_is_a_failed_start(
    campaign: Campaign, db: Database, runtime: ScriptRuntime, tmp_path: Path, mocker: MockerFixture
) -> None:
    start = mocker.spy(runtime, 'start')
    agent_session_id = request(db, campaign)
    missing = Cid.of(b'a brief that nobody stored')
    outcome = launcher(db, runtime, tmp_path).launch(
        agent_session_id, brief=missing, place='w', artifact=checkout(campaign), timeout=60
    )
    w = agent_session(db, agent_session_id)
    assert (isinstance(outcome, NotStarted), w.error) == (True, f'LookupError: the CAS lacks the brief {missing}')
    assert not start.called
    with pytest.raises(LookupError, match='the CAS lacks the brief'):
        read_brief(home_cas(db), missing)


def test_nothing_starts_after_cancel(campaign: Campaign, db: Database, runtime: ScriptRuntime, tmp_path: Path) -> None:
    agent_session_id = request(db, campaign)
    lc = launcher(db, runtime, tmp_path)
    lc.cancel()
    outcome = lc.launch(
        agent_session_id, brief=home_cas(db).put(b'x'), place='w', artifact=checkout(campaign), timeout=60
    )
    w = agent_session(db, agent_session_id)
    assert (outcome, w.state) == (DaemonStopped(), AgentSessionState.REQUESTED), 'stop() closes rows'
    assert not (tmp_path / 'w').exists()


def test_a_harness_with_a_usage_spec_gets_a_minted_session_id_in_its_argv_and_on_its_row(
    campaign: Campaign,
    db: Database,
    runtime: ScriptRuntime,
    tmp_path: Path,
    tracer: Tracer,
    spans: InMemorySpanExporter,
) -> None:
    use_harness(db, campaign, HARNESS_PROMPT_TO_FILE)
    plain = request(db, campaign)
    launch(db, runtime, tmp_path, plain)
    assert agent_session(db, plain).session_id is None, 'no usage spec: nothing needs the id, none minted'
    use_harness(db, campaign, '#!/bin/sh\nprintf %s "$2" > ../session.txt\n', **MINTED_HARNESS)
    agent_session_id = request(db, campaign)
    launch(db, runtime, tmp_path, agent_session_id, tracer)
    w = agent_session(db, agent_session_id)
    assert w.session_id is not None and UUID.match(w.session_id), 'a uuid4, as pi and claude --session-id take'
    assert (tmp_path / 'w' / 'session.txt').read_text() == w.session_id
    assert dict(span_attributes(spans, 'piloop.agent', agent_session_id))['session_id'] == w.session_id


def test_a_harness_that_prints_its_session_id_has_it_read_back_by_the_runtime(
    campaign: Campaign,
    db: Database,
    runtime: ScriptRuntime,
    tmp_path: Path,
    tracer: Tracer,
    spans: InMemorySpanExporter,
) -> None:
    codex = each_provider()['codex']
    use_harness(db, campaign, CODEX_LIKE, **NO_USAGE_FILE, session_id_pattern=codex.session_id_pattern)
    agent_session_id = request(db, campaign)
    launch(db, runtime, tmp_path, agent_session_id, tracer)
    assert agent_session(db, agent_session_id).session_id == CODEX_ID
    assert span_attributes(spans, 'piloop.job', agent_session_id)['session_id'] == CODEX_ID
    use_harness(db, campaign, HARNESS_PROMPT_TO_FILE, **NO_USAGE_FILE, session_id_pattern=codex.session_id_pattern)
    silent = request(db, campaign)
    launch(db, runtime, tmp_path, silent, tracer)
    assert agent_session(db, silent).session_id is None
    assert events(spans, 'agent_session.session_unknown') == [{'agent_session': silent, 'runtime': 'subprocess'}], (
        'nothing to attach, now or later'
    )


def test_a_session_reported_late_is_asked_for_again_before_the_agent_is_released(
    campaign: Campaign,
    db: Database,
    runtime: ScriptRuntime,
    tmp_path: Path,
    tracer: Tracer,
    spans: InMemorySpanExporter,
    mocker: MockerFixture,
) -> None:
    """A harness slow to start reports no session before the wait. It is asked again before its pane closes.

    A failure of that last ask is recorded, and the agent is still released.
    """
    codex = each_provider()['codex']
    use_harness(db, campaign, CODEX_LIKE, **NO_USAGE_FILE, session_id_pattern=codex.session_id_pattern)
    mocker.patch.object(runtime, 'session_id', side_effect=[None, CODEX_ID])
    late = request(db, campaign)
    launch(db, runtime, tmp_path, late, tracer)
    assert agent_session(db, late).session_id == CODEX_ID
    assert events(spans, 'agent_session.session_unknown') == [], 'found on the second ask'

    mocker.patch.object(runtime, 'session_id', side_effect=[None, RuntimeError('server_unavailable')])
    lost = request(db, campaign)
    launch(db, runtime, tmp_path, lost, tracer)
    assert agent_session(db, lost).session_id is None and len(runtime.killed) == 2
    assert events(spans, 'agent_session.session_unknown') == [
        {'agent_session': lost, 'runtime': 'subprocess', 'error': 'server_unavailable'}
    ]


def test_a_session_reported_late_goes_back_in_the_report_not_only_in_a_heartbeat(
    campaign: Campaign, db: Database, runtime: ScriptRuntime, tmp_path: Path, mocker: MockerFixture
) -> None:
    """The agent can end and report before the next heartbeat would carry the session id that the last ask found."""
    codex = each_provider()['codex']
    use_harness(db, campaign, CODEX_LIKE, **NO_USAGE_FILE, session_id_pattern=codex.session_id_pattern)
    heartbeat = Leases.heartbeat

    def no_session_ids(
        self: Leases, worker: str, sessions: dict[int, HarnessSession], host: HostSample
    ) -> dict[int, Answer]:
        return heartbeat(self, worker, dict.fromkeys(sessions, HarnessSession()), host)

    mocker.patch.object(Leases, 'heartbeat', no_session_ids)
    mocker.patch.object(runtime, 'session_id', side_effect=[None, CODEX_ID])
    late = request(db, campaign)
    launch(db, runtime, tmp_path, late, Tracer(None))
    assert agent_session(db, late).session_id == CODEX_ID


def test_the_agents_session_file_goes_into_the_cas_and_its_usage_is_summed_from_it_when_it_ends(
    campaign: Campaign,
    db: Database,
    runtime: ScriptRuntime,
    tmp_path: Path,
    tracer: Tracer,
    spans: InMemorySpanExporter,
    monkeypatch: pytest.MonkeyPatch,
    mocker: MockerFixture,
) -> None:
    home = tmp_path / 'home'
    monkeypatch.setenv('HOME', str(home))
    folder = home / '.pi' / 'agent' / 'sessions' / dash_separators_wrapped(Path(campaign.path))
    writes_session = (
        f'#!/bin/sh\nmkdir -p {folder}\ncp {SESSIONS / "pi.jsonl"} "{folder}/2026-09-09T05-48-14-300Z_$2.jsonl"\n'
    )
    minted = {'headless': ['--session-id', '{session_id}', '{prompt}'], **READS_PI}
    use_harness(db, campaign, writes_session, **minted)
    agent_session_id = request(db, campaign)
    launch(db, runtime, tmp_path, agent_session_id, tracer)
    w = agent_session(db, agent_session_id)
    assert w.session_id is not None, 'a usage spec mints the id'
    assert w.usage == Usage(
        input=4,
        output=993,
        cache_read=6763,
        cache_write=8718,
        total=16478,
        turns=2,
    ), "pi's store is keyed by the agent's cwd, the worktree; read before the caller removes it"
    left = (SESSIONS / 'pi.jsonl').read_bytes()
    assert w.session_file is not None and read(home_cas(db), w.session_file) == left, 'the whole file, as it was left'
    with db.session() as s:
        saved = s.scalars(select(Event.payload).where(Event.kind == 'agent_session.session_saved')).all()
    assert saved == [SessionSaved(session_file=w.session_file, session_id=w.session_id)]
    assert span_attributes(spans, 'piloop.job', agent_session_id)['tokens'] == 16478
    assert events(spans, 'agent_session.usage_unknown') == []
    assert anomalies(db) == [], 'it spent tokens: it started, however fast it ended'

    use_harness(db, campaign, HARNESS_PROMPT_TO_FILE, **minted)
    silent = request(db, campaign)
    launch(db, runtime, tmp_path, silent, tracer)
    w = agent_session(db, silent)
    assert (w.state, w.error, w.usage, w.session_file) == (AgentSessionState.EXITED, None, None, None), (
        'the row is fine'
    )
    assert events(spans, 'agent_session.usage_unknown') == [
        {
            'agent_session': silent,
            'error': f'no session file for {w.session_id}: nothing matches'
            f' {home}/.pi/agent/sessions/*/*_{w.session_id}.jsonl',
        }
    ]
    use_harness(db, campaign, HARNESS_PROMPT_TO_FILE)
    plain = request(db, campaign)
    launch(db, runtime, tmp_path, plain, tracer)
    assert agent_session(db, plain).usage is None and len(events(spans, 'agent_session.usage_unknown')) == 1, (
        'no usage spec: nothing to read and nothing to say'
    )
    mocker.patch.object(Launcher, '_prepare', side_effect=RuntimeError('no config'))
    broken = request(db, campaign)
    launch(db, runtime, tmp_path, broken, tracer)
    assert (agent_session(db, broken).state, agent_session(db, broken).usage) == (AgentSessionState.KILLED, None)
    assert len(events(spans, 'agent_session.usage_unknown')) == 1, 'an agent that never ran has no session to read'


def test_a_running_agents_session_file_is_snapshotted_onto_its_row_and_the_last_snapshot_is_the_whole_file(
    campaign: Campaign, db: Database, runtime: ScriptRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, hold = tmp_path / 'home', tmp_path / 'hold'
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setattr(jobs, 'POLL_SECONDS', 0.05)
    monkeypatch.setattr(sessions, 'QUIET_SECONDS', 0.1)
    folder = home / '.pi' / 'agent' / 'sessions' / dash_separators_wrapped(Path(campaign.path))
    whole = (SESSIONS / 'pi.jsonl').read_bytes()
    first = whole[: whole.index(b'\n') + 1]
    (tmp_path / 'started.jsonl').write_bytes(first + b'{"type":"mess')
    hold.touch()
    session = f'"{folder}/2026-09-09T05-48-14-300Z_$2.jsonl"'
    writes_then_waits = (
        f'#!/bin/sh\nmkdir -p {folder}\ncp {tmp_path / "started.jsonl"} {session}\n'
        f'while [ -f {hold} ]; do sleep 0.05; done\ncp {SESSIONS / "pi.jsonl"} {session}\n'
    )
    use_harness(db, campaign, writes_then_waits, headless=['--session-id', '{session_id}', '{prompt}'], **READS_PI)
    agent = request(db, campaign)
    launching = threading.Thread(target=launch, args=(db, runtime, tmp_path, agent))
    launching.start()
    cas = home_cas(db)

    def stored() -> bytes | None:
        session_file = agent_session(db, agent).session_file
        return None if session_file is None else read(cas, session_file)

    wait_until(lambda: stored() == first, 'a snapshot of its whole lines, once the file was quiet')
    hold.unlink()
    launching.join(30)
    assert stored() == whole, 'the last one, when it ended'
    with db.session() as s:
        saved = s.scalars(select(Event.payload).where(Event.kind == 'agent_session.session_saved')).all()
    assert [read(cas, e.session_file) for e in saved if isinstance(e, SessionSaved)] == [first, whole], (
        'one event for each new snapshot: an unchanged file is not saved again'
    )


def test_a_snapshot_that_fails_is_tried_again_and_a_late_session_id_is_asked_for_before_each(
    campaign: Campaign,
    db: Database,
    runtime: ScriptRuntime,
    tmp_path: Path,
    tracer: Tracer,
    spans: InMemorySpanExporter,
    monkeypatch: pytest.MonkeyPatch,
    mocker: MockerFixture,
) -> None:
    monkeypatch.setattr(jobs, 'POLL_SECONDS', 0.05)
    codex = each_provider()['codex']
    hold = tmp_path / 'hold'
    hold.touch()
    held = f'#!/bin/sh\necho \'{{"type":"thread.started","thread_id":"{CODEX_ID}"}}\'\nwhile [ -f {hold} ]; do sleep 0.05; done\n'
    use_harness(db, campaign, held, **NO_USAGE_FILE, session_id_pattern=codex.session_id_pattern)
    asked = mocker.patch.object(runtime, 'session_id', side_effect=[None, CODEX_ID])
    mocker.patch.object(SessionFile, 'due', return_value=True)

    def fails(now: float) -> None:
        hold.unlink(missing_ok=True)  # the agent ends after the first timed snapshot, however late that comes
        raise OSError('the file went')

    mocker.patch.object(SessionFile, 'snapshot', side_effect=fails)
    late = request(db, campaign)
    launch(db, runtime, tmp_path, late, tracer)
    w = agent_session(db, late)
    assert (w.session_id, w.session_file, asked.call_count) == (CODEX_ID, None, 2), (
        'a timed snapshot found the id: the end asks no more'
    )
    unsaved = events(spans, 'agent_session.session_unsaved')
    assert len(unsaved) >= 2 and unsaved[-1] == {'agent_session': late, 'error': 'OSError: the file went'}, (
        'each timed one and the last one: the report goes without it'
    )


def test_a_killed_agents_usage_is_read_too(
    campaign: Campaign, db: Database, runtime: ScriptRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The usage of an agent killed at its timeout is read too: it spent its whole budget."""
    home = tmp_path / 'home'
    monkeypatch.setenv('HOME', str(home))
    folder = home / '.pi' / 'agent' / 'sessions' / dash_separators_wrapped(Path(campaign.path))
    writes_then_hangs = (
        f'#!/bin/sh\nmkdir -p {folder}\ncp {SESSIONS / "pi.jsonl"} "{folder}/2026-09-09T05-48-14-300Z_$2.jsonl"\n'
        'sleep 30 &\necho $! > ../child.pid\nwait\n'
    )
    use_harness(db, campaign, writes_then_hangs, headless=['--session-id', '{session_id}', '{prompt}'], **READS_PI)
    with db.session() as s:
        p = s.get_one(Campaign, campaign.id)
        config = p.config.model_dump(mode='json')
        config['plugins']['role']['watchdog']['timeout'] = 0.5
        p.config = CampaignConfig.model_validate(config)
        s.commit()
    agent_session_id = request(db, campaign)
    outcome = launch(db, runtime, tmp_path, agent_session_id)
    w = agent_session(db, agent_session_id)
    assert (exit_of(outcome).status, w.state, w.error) == (
        ExitStatus.TIMED_OUT,
        AgentSessionState.KILLED,
        'agent timeout after 0.5s',
    )
    assert w.usage is not None and (w.usage.total, w.usage.turns) == (16478, 2)


def span_attributes(spans: InMemorySpanExporter, name: str, agent_session_id: AgentSessionId) -> dict[str, object]:
    [span] = [
        s
        for s in spans.get_finished_spans()
        if s.name == name and (s.attributes or {}).get('agent_session') == agent_session_id
    ]
    return dict(span.attributes or {})


def test_fill_keeps_literal_braces() -> None:
    assert fill('{"k": "{model}"}', model='m') == '{"k": "m"}'


def test_the_argv_gets_the_agents_cwd(campaign: Campaign, db: Database, runtime: ScriptRuntime, tmp_path: Path) -> None:
    use_harness(db, campaign, RECORDS_ARGV_AND_ENV, headless=['--cd', '{cwd}', '{prompt}'])
    launch(db, runtime, tmp_path, request(db, campaign))
    assert (tmp_path / 'w' / 'argv.txt').read_text().splitlines()[:2] == ['--cd', str(worktree(tmp_path))]


def test_codex_trusts_the_agents_worktree_and_gives_its_tool_shells_the_agents_env() -> None:
    """The ``-c`` values parse the way Codex reads them: a dotted key split on every ``.``, and a TOML value."""
    cwd = '/home/me/.piloop/runs/7/worktree'
    codex = each_provider()['codex']
    for template in (codex.headless, codex.resume):
        args = [fill(a, cwd=cwd, model='m', prompt='p', session_id='s') for a in template]
        flags = [value.partition('=') for flag, value in zip(args, args[1:], strict=False) if flag == '-c']
        parsed = {key: tomllib.loads(f'v = {value}')['v'] for key, _, value in flags}
        assert parsed['projects'] == {cwd: {'trust_level': 'trusted'}}, 'this launch only; no dialog, no config write'
        assert {k: v for k, v in parsed.items() if k.startswith('shell_environment_policy.')} == {
            'shell_environment_policy.inherit': 'all',
            'shell_environment_policy.ignore_default_excludes': True,
            'shell_environment_policy.include_only': [],
        }, 'PILOOP_PROCESS_TAG and PILOOP_AGENT_TOKEN reach every command; the MCP renderer excludes the keys'


@pytest.mark.parametrize('user_config', list(UserConfig))
def test_each_harness_resumes_with_the_model_effort_and_config_of_its_first_turn(user_config: UserConfig) -> None:
    """A resumed turn that changed the model, the effort or a config flag would miss the prompt cache of the session:
    the two argvs differ only in how they name the session and in the prompt."""
    for name, default in each_provider().items():
        harness = type(default).model_validate({'user_config': user_config})
        session = {'resume', '--resume', '--session-id', SESSION_ID, PROMPT}
        first, resumed = (
            [a for a in harness.expanded(argv) if a not in session] for argv in (harness.headless, harness.resume)
        )
        assert first == resumed, name


def test_a_brief_past_the_limit_of_one_argument_reaches_the_agent_as_a_file(
    campaign: Campaign, db: Database, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    """A brief longer than Linux's limit on one argument (``MAX_ARG_STRLEN``, 128 KiB) reaches the agent as a file.

    The agent's first prompt names the file instead of holding the brief.
    """
    use_harness(db, campaign, RECORDS_ARGV_AND_ENV)
    brief = 'x' * 200 * 1024 + '\n'
    outcome = launcher(db, runtime, tmp_path).launch(
        request(db, campaign),
        brief=home_cas(db).put(brief.encode()),
        place='w',
        artifact=checkout(campaign),
        timeout=60,
    )
    assert (exit_of(outcome).status, exit_of(outcome).error) == (ExitStatus.EXITED, None)
    assert (tmp_path / 'w' / 'argv.txt').read_text().splitlines() == [
        FIRST_MESSAGE.format(brief=tmp_path / 'w' / 'brief.md')
    ]
    assert (tmp_path / 'w' / 'brief.md').read_text() == brief


def give_watchdog_servers(db: Database, campaign: Campaign, servers: dict[str, object]) -> None:
    """``[plugins.mcp_server.*]`` tables in the stored config, all listed by ``[plugins.role.watchdog] mcp``."""
    with db.session() as s:
        p = s.get_one(Campaign, campaign.id)
        p.config = reconfigured(p.config, plugins={'mcp_server': servers, 'role': {WATCHDOG: {'mcp': list(servers)}}})
        s.commit()


RECORDS_ARGV_AND_ENV = '#!/bin/sh\nprintf "%s\\n" "$@" > ../argv.txt\nenv > ../env.txt\n'


def test_the_mcp_element_expands_to_the_roles_servers_and_their_env_reaches_the_agent(
    campaign: Campaign, db: Database, runtime: ScriptRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_harness(
        db,
        campaign,
        RECORDS_ARGV_AND_ENV,
        provider='pi',
        headless=['--no-extensions', MCP, '{prompt}'],
        session_id_pattern='(never)',
    )
    give_watchdog_servers(db, campaign, {'probe': {'provider': 'stdio', 'command': ['cat'], 'env': ['PROBE_KEY']}})
    monkeypatch.delenv('PROBE_KEY', raising=False)
    put_secret(db, None, 'PROBE_KEY', 'probe-value')
    specs: list[AgentSpec] = []
    start = runtime.start
    monkeypatch.setattr(runtime, 'start', lambda spec: specs.append(spec) or start(spec))
    agent_session_id = request(db, campaign)
    launch(db, runtime, tmp_path, agent_session_id)
    argv = (tmp_path / 'w' / 'argv.txt').read_text().splitlines()
    config = tmp_path / 'w' / 'mcp.json'
    assert argv[:5] == ['--no-extensions', '-e', 'npm:pi-mcp-adapter@4.0.0', '--mcp-config', str(config)]
    assert json.loads(config.read_text()) == {
        'mcpServers': {'probe': {'command': 'cat', 'args': [], 'env': {'PROBE_KEY': '${PROBE_KEY}'}}}
    }
    [spec] = specs
    assert spec.env['PROBE_KEY'] == 'probe-value', 'each variable that a server names is the secret of its name'
    assert 'probe-value' not in ' '.join(spec.args) and 'probe-value' not in config.read_text()
    assert 'PI_MCP_CONFIG_MODE=exclusive' in (tmp_path / 'w' / 'env.txt').read_text().splitlines()
    assert agent_session(db, agent_session_id).error is None


def test_an_agent_knows_when_its_timeout_ends(
    campaign: Campaign, db: Database, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    """``PILOOP_DEADLINE`` is the Unix time at which the agent's role timeout ends it."""
    use_harness(db, campaign, RECORDS_ARGV_AND_ENV)
    before = time.time()
    launch(db, runtime, tmp_path, request(db, campaign))
    env = dict(line.split('=', 1) for line in (tmp_path / 'w' / 'env.txt').read_text().splitlines() if '=' in line)
    timeout = load_campaign_config(Path(campaign.path)).role(WATCHDOG).timeout
    assert before + timeout - 1 <= int(env['PILOOP_DEADLINE']) <= time.time() + timeout


def test_a_secret_that_the_home_lacks_fails_the_launch_naming_it_and_until_0_5_comes_from_the_environment(
    campaign: Campaign, db: Database, runtime: ScriptRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_harness(
        db,
        campaign,
        RECORDS_ARGV_AND_ENV,
        provider='pi',
        headless=['--no-extensions', MCP, '{prompt}'],
        session_id_pattern='(never)',
    )
    give_watchdog_servers(
        db, campaign, {'probe': {'provider': 'http', 'url': 'https://x', 'bearer_token_env': 'PROBE_TOKEN'}}
    )
    monkeypatch.delenv('PROBE_TOKEN', raising=False)
    agent_session_id = request(db, campaign)
    launch(db, runtime, tmp_path, agent_session_id)
    w = agent_session(db, agent_session_id)
    assert w.state is AgentSessionState.KILLED and w.error is not None
    assert w.error.startswith('MissingSecrets: the secrets PROBE_TOKEN are not set: `piloop secret set <NAME>`')
    assert not (tmp_path / 'w' / 'argv.txt').exists(), 'never started'
    monkeypatch.setenv('PROBE_TOKEN', 'from-the-environment')
    launch(db, runtime, tmp_path, request(db, campaign))
    assert 'PROBE_TOKEN=from-the-environment' in (tmp_path / 'w' / 'env.txt').read_text().splitlines()


def test_a_harness_env_gives_values_and_secrets_and_a_campaigns_secret_wins_over_the_homes(
    campaign: Campaign, db: Database, runtime: ScriptRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = {
        'GATEWAY_URL': 'https://gw.example.com',
        'GATEWAY_KEY': {'secret': 'TEAM_KEY'},
        'PILOOP_ACTOR': 'spoofed',
    }
    use_harness(db, campaign, RECORDS_ARGV_AND_ENV, env=env)
    monkeypatch.delenv('TEAM_KEY', raising=False)
    put_secret(db, None, 'TEAM_KEY', 'home-key')
    put_secret(db, campaign.id, 'TEAM_KEY', 'campaign-key')
    other = put_secret(db, None, 'OTHER_KEY', 'unasked')
    agent_session_id = request(db, campaign)
    launch(db, runtime, tmp_path, agent_session_id)
    seen = (tmp_path / 'w' / 'env.txt').read_text().splitlines()
    assert {'GATEWAY_URL=https://gw.example.com', 'GATEWAY_KEY=campaign-key'} <= set(seen)
    assert f'PILOOP_ACTOR=watchdog#{agent_session_id}' in seen, "piloop's own variables win"
    assert other not in ' '.join(seen) and 'TEAM_KEY=' not in ' '.join(seen), 'only what the job names, by variable'
    with db.session() as s:
        job = read_job(CAS.selected({}).open(tmp_path), *s.execute(select(Lease.id, Lease.job_cid)).one())
    assert isinstance(job, AgentJob) and job.secrets == {'GATEWAY_KEY': 'TEAM_KEY'}
    assert 'campaign-key' not in job.model_dump_json(), 'the job in the CAS names secrets, never their values'


def test_a_harness_expands_its_own_elements_of_the_argv_before_the_launch(
    campaign: Campaign, db: Database, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    """Codex's ``config`` table: each key is a ``-c`` flag where its argv has ``{config}``."""
    config = {'model_provider': 'gateway', 'model_providers': {'gateway': {'base_url': 'https://gw.example.com/v1'}}}
    use_harness(
        db,
        campaign,
        RECORDS_ARGV_AND_ENV,
        provider='codex',
        headless=['{config}', '{prompt}'],
        resume=[],
        config=config,
    )
    launch(db, runtime, tmp_path, request(db, campaign))
    assert (tmp_path / 'w' / 'argv.txt').read_text().splitlines()[:4] == [
        '-c',
        'model_provider="gateway"',
        '-c',
        'model_providers={gateway = {base_url = "https://gw.example.com/v1"}}',
    ]


def test_servers_that_cannot_be_rendered_fail_the_launch_on_the_row(
    campaign: Campaign, db: Database, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    use_harness(db, campaign, RECORDS_ARGV_AND_ENV, provider='codex', headless=[MCP, '{prompt}'], resume=[])
    give_watchdog_servers(db, campaign, {'probe': {'provider': 'stdio', 'command': ['cat']}})
    agent_session_id = request(db, campaign)
    launch(db, runtime, tmp_path, agent_session_id)
    w = agent_session(db, agent_session_id)
    assert w.state is AgentSessionState.KILLED and w.error is not None and w.error.startswith('McpError: ')
    assert 'printed no server list' in w.error, 'the stand-in is no codex: `mcp list --json` printed nothing'


def test_a_subprocess_agent_gets_the_arguments_that_resume_its_session(
    campaign: Campaign, db: Database, runtime: ScriptRuntime, tmp_path: Path, tracer: Tracer, mocker: MockerFixture
) -> None:
    """``{prompt}`` stays for the queued messages; ``{session_id}`` stays when the harness reports its own id."""
    use_harness(
        db, campaign, HARNESS_PROMPT_TO_FILE, resume=['resume', '{session_id}', '--model', '{model}', '{prompt}']
    )
    start = mocker.spy(runtime, 'start')
    launch(db, runtime, tmp_path, request(db, campaign), tracer)
    [spec] = [c.args[0] for c in start.call_args_list]
    assert spec.resume_args == ['resume', '{session_id}', '--model', 'watchdog-model', '{prompt}']


def test_an_agent_whose_start_cannot_be_recorded_is_killed_and_waited_for(
    campaign: Campaign, db: Database, runtime: ScriptRuntime, tmp_path: Path, mocker: MockerFixture
) -> None:
    """When the record of the start failed, the agent ran on with nothing that watched it, and nothing waited for its
    process. Python then warned ``subprocess N is still running`` when it collected the ``Popen``. The worker ends
    it, and reports a failed start."""
    use_harness(db, campaign, '#!/bin/sh\nsleep 30\n')
    agent_session_id = request(db, campaign)
    mocker.patch('piloop.agents.results.CampaignResults.started', side_effect=RuntimeError('database closed'))
    outcome = launch(db, runtime, tmp_path, agent_session_id, Tracer(None))
    [proc] = runtime._procs.values()
    assert proc.returncode is not None, 'killed and waited for, not left running unwatched'
    assert outcome == NotStarted('RuntimeError: database closed')


EFFORT_FLAG = {'claude': ('--effort', '{}'), 'codex': ('-c', 'model_reasoning_effort="{}"')}
"""How each built-in harness takes the role's ``effort``."""


def research_argv(
    db: Database,
    campaign: Campaign,
    runtime: ScriptRuntime,
    tmp_path: Path,
    mocker: MockerFixture,
    harness: str,
    **research: object,
) -> list[str]:
    """The argv that a research agent starts on the built-in ``harness``, with ``research`` over its role's table."""
    write_toml(Path(campaign.path), HARNESS_PROMPT_TO_FILE)
    cfg = load_campaign_config(
        Path(campaign.path),
        plugins={
            'harness': {
                'use': [harness],
                harness: {
                    'executable': str(Path(campaign.path).parent / 'harness.sh'),
                    'headless': [a for a in each_provider()[harness].headless if a != MCP],
                    'resume': [],
                    'model': 'the-campaigns-model',
                },
                'script': {'model': 'm'},
            },
            'role': {'research': research},
        },
    )
    assert cfg.role_models(IMPLEMENTER)[harness] == 'the-campaigns-model'
    with db.session() as s:
        s.get_one(Campaign, campaign.id).config = cfg
        s.commit()
    start = mocker.spy(runtime, 'start')
    launch(db, runtime, tmp_path, request(db, campaign, kind=RESEARCH, harness=harness))
    [spec] = [c.args[0] for c in start.call_args_list]
    return spec.args


@pytest.mark.parametrize(('harness', 'model'), [('claude', 'claude-opus-5-5'), ('codex', 'gpt-6.1-sol')])
def test_research_defaults_to_its_models_at_high_effort(
    campaign: Campaign,
    db: Database,
    runtime: ScriptRuntime,
    tmp_path: Path,
    mocker: MockerFixture,
    harness: str,
    model: str,
) -> None:
    args = research_argv(db, campaign, runtime, tmp_path, mocker, harness)
    flag, value = EFFORT_FLAG[harness]
    assert {('--model', model), (flag, value.format('high'))} <= set(zip(args, args[1:], strict=False))


@pytest.mark.parametrize('harness', ['claude', 'codex'])
def test_the_research_table_overrides_its_model_and_effort(
    campaign: Campaign, db: Database, runtime: ScriptRuntime, tmp_path: Path, mocker: MockerFixture, harness: str
) -> None:
    args = research_argv(db, campaign, runtime, tmp_path, mocker, harness, model={harness: 'mine'}, effort='low')
    flag, value = EFFORT_FLAG[harness]
    assert {('--model', 'mine'), (flag, value.format('low'))} <= set(zip(args, args[1:], strict=False))


def put_secret(db: Database, owner: CampaignId | None, name: str, value: str) -> str:
    """Set a secret as the human does (``piloop secret set``), and return its value."""
    with db.session() as s:
        set_secret(s, SECRETS.selected({}), System.HUMAN, owner, name, SecretStr(value))
        s.commit()
    return value
