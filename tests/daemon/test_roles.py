"""Roles as data: a sweeping role and a role that does runs that piloop.toml defines, new triggers for a built-in role,
and the roles, triggers and runtimes that packages add through entry points."""

import os
import re
import warnings
from collections.abc import Iterator
from datetime import timedelta
from importlib.metadata import EntryPoint
from pathlib import Path

import pytest
from covalence import Cas as Covalence
from sqlalchemy import select

from piloop.actor import System
from piloop.agents.results import PromptOutcome
from piloop.campaign import ops
from piloop.campaign.stop import CampaignClosed, left
from piloop.cli import common, main as cli
from piloop.cli.plugins import selections
from piloop.config import ConfigError, load_campaign_config
from piloop.daemon.main import Daemon
from piloop.daemon.scheduler import Scheduler
from piloop.db.events import emit, in_order, latest
from piloop.db.models import AgentSession, AgentSessionState, Campaign, Comment, Event, utcnow
from piloop.db.session import Database
from piloop.deprecated import PiloopDeprecationWarning
from piloop.ids import CampaignStatus, CommentKind, RoleName, TargetKind, TargetRef
from piloop.otel import Tracer
from piloop.plugins import host as plugins_module
from piloop.plugins.campaign_type.consumers import awake_roles, quiet_phase
from piloop.plugins.campaign_type.kit.backlog.ids import IdeaId, IdeaStatus
from piloop.plugins.campaign_type.kit.backlog.tables import Idea
from piloop.plugins.campaign_type.kit.candidates.ids import CANDIDATE
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.evaluation.tables import Measurement
from piloop.plugins.campaign_type.kit.pool.scored import CandidateScored
from piloop.plugins.campaign_type.kit.runs.controller import RunController
from piloop.plugins.campaign_type.kit.runs.ids import RunEnd, RunStatus
from piloop.plugins.campaign_type.kit.runs.tables import Run, working_on
from piloop.plugins.cas.contract import CAS
from piloop.plugins.executor.workers import Workers
from piloop.plugins.host import Capability, PluginError, compose
from piloop.plugins.lineage.contract import LINEAGE
from piloop.plugins.role.contract import ImplementerRole, Quiet, new_role
from piloop.plugins.runtime.contract import RUNTIMES, Runtime, RuntimeConfig, RuntimeFactory
from piloop.plugins.section.contract import SECTIONS
from piloop.settings import Settings
from tests.conftest import (
    AUDITOR,
    END_RUN,
    HARNESS_APPEND,
    PLUGIN,
    ROLE_GROUP,
    SETTLE,
    ScriptRuntime,
    campaigns_of,
    example,
    forget_loaded,
    install_plugins,
    package,
    queued,
    reconfigured,
    run_all,
    serving,
    settle,
    store_config,
    wait_until,
    write_toml,
)
from tests.daemon.test_scheduler import add_ideas
from tests.fixtures.plugin import Ticker

RUNTIME_GROUP = RUNTIMES.group


def recording(runtime: ScriptRuntime, kinds: list[str]) -> RuntimeFactory:
    """A factory that runs every agent in ``runtime`` and records the kind of runtime that was asked for."""

    def make(kind: str) -> Runtime:
        kinds.append(kind)
        return runtime

    return make


def scheduler(db: Database, make: RuntimeFactory, tmp_path: Path) -> Scheduler:
    tracer = Tracer(None)
    return Scheduler(db, serving(RunController(db, tracer, tmp_path, make, executor=Workers())), tracer, tmp_path, make)


def configure(
    db: Database,
    campaign: Campaign,
    harness_body: str,
    **roles: dict[str, object],
) -> None:
    """Rewrite the campaign's harness script and role settings (merged over the test toml)."""
    write_toml(Path(campaign.path), harness_body)
    with db.session() as s:
        cfg = load_campaign_config(Path(campaign.path), plugins={'role': roles})
        s.get_one(Campaign, campaign.id).config = cfg
        s.commit()


HARNESS_SIGNS = '#!/bin/sh\necho "$PILOOP_ACTOR" >> solve.txt\ngit commit -qam "sign"\n' + END_RUN
"""Commit a tree of one's own and end the run: an agent of another role that runs at the same time makes another
candidate."""
QUICK: dict[str, object] = {
    'runs': True,
    'prompt': 'quick.md',
    'timeout': 60,
    'max_concurrent': 1,
}


def runs_by_role(db: Database) -> list[tuple[int, str, int | None, str]]:
    """Return each run as ``(id, role, idea, kind of its agent)``."""
    with db.session() as s:
        return [
            (
                run.id,
                run.role,
                run.idea_id,
                s.scalars(select(AgentSession.kind).where(working_on(run.id))).one(),
            )
            for run in s.scalars(select(Run).order_by(Run.id))
        ]


def queue(db: Database, sched: Scheduler, campaign: Campaign) -> None:
    """Queue runs for the free slots, as a tick does, without launching their agents."""
    with db.session() as s:
        p = s.get_one(Campaign, campaign.id)
        campaigns_of(sched)._planner(p).queue(s, p, left(s, p, p.config))
        s.commit()


@pytest.mark.usefixtures('daemon_api')
def test_a_role_that_does_runs_works_with_its_own_prompt_and_its_submission_is_scored(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    """``[plugins.role.quick]`` with ``runs = true`` gets a fresh run of its own, as the implementer does.

    The quick agent works with its own prompt in a new worktree at the best, and its submission is measured and scored.
    """
    (Path(campaign.path) / 'quick.md').write_text('# Quick\nTry the idea in a few minutes.\n')
    configure(db, campaign, HARNESS_SIGNS, quick=QUICK)
    (deep,) = add_ideas(db, campaign, 'deep', priority=1)
    (cheap,) = add_ideas(db, campaign, 'cheap', priority=3)
    sched = scheduler(db, recording(runtime, []), tmp_path)
    assert queued(db, sched, campaign.id, IdeaId(cheap), IdeaId(deep)) == [1, 2]
    assert sorted(run_all(sched)) == [1, 2]
    settle(sched, db)
    sched.stop(timeout=5)
    assert runs_by_role(db) == [(1, 'quick', cheap, 'quick'), (2, 'implementer', deep, 'implementer')]
    assert (tmp_path / 'runs' / '1' / 'brief.md').read_text().startswith('# Quick\nTry the idea')
    assert (tmp_path / 'runs' / '2' / 'brief.md').read_text().startswith('# Implementer\n')
    with db.session() as s:
        assert s.get_one(Run, 1).status is RunStatus.SUCCEEDED
        candidate = s.scalars(select(Candidate).where(Candidate.run_id == 1)).one()
        assert latest(s, CandidateScored, Event.target == TargetRef(CANDIDATE, candidate.id)) is not None
        assert s.scalars(select(Measurement).where(Measurement.candidate_id == candidate.id)).all() != []


def test_each_role_that_does_runs_fills_its_own_slots_with_fresh_runs_that_claim_no_idea(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    """Quick has one slot and the implementer one: each gets a fresh run, in the order of the roles, and no idea."""
    configure(db, campaign, HARNESS_APPEND, quick={**QUICK, 'prompt': None})
    ideas = [add_ideas(db, campaign, t, priority=p)[0] for t, p in (('cheap', 3), ('cheaper', 4))]
    sched = scheduler(db, recording(runtime, []), tmp_path)
    queue(db, sched, campaign)
    assert runs_by_role(db) == [(1, 'quick', None, 'quick'), (2, 'implementer', None, 'implementer')]
    with db.session() as s:
        assert [s.get_one(Idea, i).status for i in ideas] == [IdeaStatus.OPEN] * 2, 'a worker claims its own'
    sched.stop(timeout=5)


def test_a_worker_on_an_empty_board_is_told_to_propose_its_own_idea_and_nothing_in_its_brief_forbids_it(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    """With no open idea, a worker proposes one and claims it: its brief says how, and no line of it says that the
    worker files no ideas, which made workers on an empty board escalate instead."""
    configure(db, campaign, HARNESS_APPEND)
    sched = scheduler(db, recording(runtime, []), tmp_path)
    assert run_all(sched) == [1]
    sched.wait(SETTLE)
    sched.stop(timeout=5)
    brief = (tmp_path / 'runs' / '1' / 'brief.md').read_text()
    assert "`piloop idea propose '<title>'" in brief
    assert not re.search(r'files? no ideas?\b(?! for others)', brief, re.IGNORECASE), brief


def test_a_queued_run_of_a_role_that_piloop_toml_no_longer_has_fails_and_its_claim_ends(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    (Path(campaign.path) / 'quick.md').write_text('# Quick\n')
    configure(db, campaign, HARNESS_APPEND, quick=QUICK)
    (cheap,) = add_ideas(db, campaign, 'cheap', priority=3)
    sched = scheduler(db, recording(runtime, []), tmp_path)
    assert queued(db, sched, campaign.id, IdeaId(cheap)) == [1, 2], "quick's run claims the idea"
    configure(db, campaign, HARNESS_APPEND)
    assert run_all(sched) == [2], "the implementer's run launches"
    with db.session() as s:
        run, agent_session, idea = s.get_one(Run, 1), s.get_one(AgentSession, 1), s.get_one(Idea, cheap)
        assert (run.status, run.end) == (RunStatus.FAILED, RunEnd.ROLE_REMOVED)
        assert (agent_session.state, agent_session.error) == (
            AgentSessionState.KILLED,
            'no [plugins.role.quick] in piloop.toml',
        )
        assert idea.status is IdeaStatus.OPEN, 'the removed role was not an attempt at the idea'
    sched.wait(SETTLE)
    sched.stop(timeout=5)


@pytest.mark.usefixtures('no_fresh_runs')
def test_a_request_for_a_role_that_piloop_toml_no_longer_has_is_refused(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    configure(db, campaign, '#!/bin/sh\ntrue\n', reviewer={'runs': True})
    with db.session() as s:
        s.add(AgentSession(kind='reviewer', campaign_id=campaign.id, reason='by hand'))
        s.commit()
    configure(db, campaign, '#!/bin/sh\ntrue\n')
    sched = scheduler(db, recording(runtime, []), tmp_path)
    assert run_all(sched) == []
    sched.stop(timeout=5)
    with db.session() as s:
        row = s.get_one(AgentSession, 1)
    assert (row.state, row.error) == (AgentSessionState.KILLED, 'no [plugins.role.reviewer] in piloop.toml')


@pytest.mark.usefixtures('auditor', 'no_fresh_runs')
def test_a_message_to_an_agent_that_has_not_started_is_in_its_brief_and_is_not_sent_again(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    configure(db, campaign, '#!/bin/sh\ntrue\n', auditor={'sections': ['context']})
    with db.session() as s:
        s.add(AgentSession(kind=AUDITOR, campaign_id=campaign.id, reason='by hand'))
        s.flush()
        on_it = TargetRef(TargetKind.AGENT_SESSION, 1)
        s.add(Comment(target=on_it, author='human', kind=CommentKind.MESSAGE, body='look at run#4 first'))
        s.add(Comment(target=on_it, author='human', kind=CommentKind.NOTE, body='a note is on the board only'))
        s.commit()
    sched = scheduler(db, recording(runtime, []), tmp_path)
    assert run_all(sched) == [1]
    sched.wait(SETTLE)
    sched.stop(timeout=5)
    brief = (tmp_path / 'agent_sessions' / '1' / 'brief.md').read_text()
    assert '\nlook at run#4 first\n' in brief and 'a note is on the board only' not in brief
    with db.session() as s:
        got = in_order(s, PromptOutcome, Event.target == on_it)
        assert [(o.kind, o.comment) for _, o in got] == [('comment.delivered', 1)]


@pytest.mark.usefixtures('auditor', 'no_fresh_runs')
def test_a_hand_over_leaves_an_agent_without_a_run_running_and_the_next_scheduler_re_attaches_to_it(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    hold = tmp_path / 'hold'
    hold.touch()
    harness = f'#!/bin/sh\necho $$ > {tmp_path}/agent.pid\nwhile [ -f {hold} ]; do sleep 0.05; done\n'
    configure(db, campaign, harness)
    with db.session() as s:
        s.add(AgentSession(kind=AUDITOR, campaign_id=campaign.id, reason='by hand'))
        s.commit()
    first = scheduler(db, recording(runtime, []), tmp_path)
    assert run_all(first) == [1]
    wait_until((tmp_path / 'agent.pid').exists)
    first.hand_over(timeout=5)
    second = scheduler(db, recording(ScriptRuntime(), []), tmp_path)
    second.reap()
    hold.unlink()
    second.wait(SETTLE)
    first.wait(SETTLE)
    with db.session() as s:
        agent = s.get_one(AgentSession, 1)
    assert (agent.state, agent.error) == (AgentSessionState.EXITED, None), 'the next daemon watched it to its end'
    assert Path(campaign.path).is_dir()
    second.stop(timeout=5)


ENTRY_POINTS = [
    EntryPoint('quick', f'{PLUGIN}:Quick', ROLE_GROUP),
    EntryPoint('typed', f'{PLUGIN}:Typed', RUNTIME_GROUP),
]


@pytest.fixture
def typed(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Install a package that adds only the ``typed`` runtime."""
    install_plugins(monkeypatch, [EntryPoint('typed', f'{PLUGIN}:Typed', RUNTIME_GROUP)])
    yield
    forget_loaded()


@pytest.fixture
def plugins(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    install_plugins(monkeypatch, ENTRY_POINTS)
    yield
    forget_loaded()


@pytest.mark.usefixtures('auditor')
@pytest.mark.parametrize(('awake', 'launches'), [(list(Quiet), True), ([], False)])
def test_a_role_that_starts_its_agents_launches_while_its_campaign_is_paused_only_if_it_wakes_in_the_phase(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path, awake: list[Quiet], launches: bool
) -> None:
    """A requested row of a role launches while its campaign is paused, held or stopped only if the role wakes in that
    phase (``StartsItsAgents.wakes_while``); else it waits."""
    configure(db, campaign, '#!/bin/sh\ntrue\n', auditor={'awake': awake})
    with db.session() as s:
        ops.pause(s, campaign.id, System.HUMAN, evals=False, reason='a person reads the results')
        s.add(AgentSession(kind=AUDITOR, campaign_id=campaign.id, reason='by hand'))
        s.commit()
    sched = scheduler(db, recording(runtime, []), tmp_path)
    assert run_all(sched) == ([1] if launches else [])
    sched.wait(SETTLE)
    sched.stop(timeout=5)
    with db.session() as s:
        row = s.get_one(Campaign, campaign.id)
        assert awake_roles(row.config, Quiet.HELD) == ({AUDITOR} if launches else frozenset())
        assert (quiet_phase(s, row, None), quiet_phase(s, row, 'at the spend limit')) == (Quiet.PAUSED, Quiet.HELD)
        row.paused = row.paused_reason = row.paused_by = None
        assert (quiet_phase(s, row, None), awake_roles(row.config, None)) == (None, frozenset())
        row.status, row.stopped_reason = CampaignStatus.STOPPED, 'by the test'
        assert quiet_phase(s, row, None) is None, 'stopped, not closed yet'
        emit(s, CampaignClosed(), TargetRef(TargetKind.CAMPAIGN, row.id), System.DAEMON)
        assert quiet_phase(s, row, None) is Quiet.STOPPED
        row.status, row.stopped_reason = CampaignStatus.RETIRED, 'by the test'
        with pytest.raises(ValueError, match='demo is retired'):
            quiet_phase(s, row, None)


MINIMAL = """
name = "demo"
evaluate = ["true"]

[[metrics]]
name = "score"
direction = "higher"
role = "primary"

[plugins.harness.claude]
model = "m"

[plugins.harness.codex]
model = "m"

{roles}
"""


def test_a_package_role_takes_its_defaults_and_a_roles_value_that_is_not_a_table_fails(
    campaign_path: Path, plugins: None
) -> None:
    toml = campaign_path / 'piloop.toml'
    toml.write_text(MINIMAL.format(roles='[plugins.role.quick]'))
    assert load_campaign_config(campaign_path).roles.doing_runs()[RoleName('quick')].timeout == 60, (
        'an empty table: the defaults'
    )
    toml.write_text(MINIMAL.format(roles='[plugins.role]\nquick = 3'))
    with pytest.raises(ConfigError, match=r'plugins\.role\.quick = 3: a role is a table'):
        load_campaign_config(campaign_path)
    toml.write_text(MINIMAL.format(roles='[plugins.role]\nmine = 3'))
    with pytest.raises(ConfigError, match=r'plugins\.role\.mine = 3: a role is a table'):
        load_campaign_config(campaign_path)
    toml.write_text(MINIMAL.format(roles='[plugins]\nrole = 3'))
    with pytest.raises(ConfigError, match=r'\n  plugins\.role = 3: a table of tables'):
        load_campaign_config(campaign_path)


@pytest.mark.parametrize(
    ('entry', 'error'),
    [
        (
            EntryPoint('auditor', f'{PLUGIN}:NOT_A_ROLE', ROLE_GROUP),
            'is not a subclass of piloop.plugins.role.contract.RoleConfig',
        ),
        (EntryPoint('implementer', f'{PLUGIN}:Quick', ROLE_GROUP), "'implementer' is a role already, from piloop "),
        (
            EntryPoint('typed', f'{PLUGIN}:NOT_A_RUNTIME', RUNTIME_GROUP),
            'is not a subclass of piloop.plugins.runtime.contract.RuntimeConfig',
        ),
        (EntryPoint('subprocess', f'{PLUGIN}:Typed', RUNTIME_GROUP), "'subprocess' is a runtime already"),
    ],
)
def test_an_entry_point_of_the_wrong_kind_fails_with_its_name(
    monkeypatch: pytest.MonkeyPatch, entry: EntryPoint, error: str
) -> None:
    """The daemon's composition fails before it starts anything, and registers nothing."""
    install_plugins(monkeypatch, [entry])
    try:
        with pytest.raises(PluginError, match=f'entry point {entry.name} = {entry.value} in {entry.group}.*{error}'):
            compose(*plugins_module.declared())
        assert not any(c.active for c in plugins_module.declared())
    finally:
        forget_loaded()


def test_a_package_adds_a_role_that_does_runs(campaign_path: Path, plugins: None) -> None:
    cfg = load_campaign_config(campaign_path)
    assert list(cfg.roles.doing_runs()) == ['quick', 'implementer']
    quick = cfg.roles.doing_runs()[RoleName('quick')]
    assert (quick.timeout, quick.max_concurrent) == (60, 1)


TICKERS = [
    EntryPoint('ticker', f'{PLUGIN}:Ticker', ROLE_GROUP),
    EntryPoint('watchdog', f'{PLUGIN}:Ticker', ROLE_GROUP),
    EntryPoint('research', f'{PLUGIN}:Ticker', ROLE_GROUP),
]
"""A package of roles that start their agents on a timer, two of them under a name that a role of piloop's had
before 0.6."""


@pytest.fixture
def tickers(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    install_plugins(monkeypatch, TICKERS)
    Ticker.woken.clear()
    yield
    forget_loaded()


@pytest.mark.usefixtures('tickers')
def test_a_package_role_that_starts_its_agents_wakes_on_the_timer_that_its_request_returns(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    """``StartsItsAgents``: each pass over the campaign asks the role (``request``), and the time that it returns is
    the loop's next due. A package's role called ``watchdog`` is its own, and so is any role's ``interval``."""
    with warnings.catch_warnings():
        warnings.simplefilter('error', PiloopDeprecationWarning)
        configure(db, campaign, '#!/bin/sh\ntrue\n', ticker={'interval': 3}, watchdog={'interval': 4})
    with db.session() as s:
        starting = s.get_one(Campaign, campaign.id).config.roles.starting()
        assert {n: r.interval for n, r in starting.items() if isinstance(r, Ticker)} == {
            'ticker': 3,
            'watchdog': 4,
            'research': 60,
        }
    sched = scheduler(db, recording(runtime, []), tmp_path)
    now = utcnow()
    run_all(sched, now)
    sched.wait(SETTLE)
    sched.stop(timeout=5)
    assert sorted(Ticker.woken) == [('research', now), ('ticker', now), ('watchdog', now)]
    assert sched.loop.next_due() == now + timedelta(seconds=3)


HARNESS_NICENESS = '#!/bin/sh\nps -o nice= -p $$ | tr -d " " > "$PILOOP_HOME/niceness.txt"\n'
"""A harness that writes its own niceness, so a test sees which runtime started it."""


@pytest.mark.usefixtures('no_fresh_runs')
def test_example_plugins_replace_the_built_in_runtime_and_cas_through_the_configuration(
    db: Database, campaign: Campaign, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The example plugins ``piloop-nice`` and ``piloop-sqlite-cas`` are installed, with a package role ``fast``.

    ``runtime = "nice"`` in piloop.toml and ``[plugins.cas] provider = "sqlite"`` in config.toml bind them in place of
    ``subprocess`` and of ``disk``. The daemon composes them before it starts, its agents start through ``nice`` at
    the niceness of ``[plugins.runtime.nice]``, and ``piloop plugins`` shows each binding with its package, its version and
    where the choice comes from.
    """
    example(tmp_path / 'site', 'nice-runtime')
    example(tmp_path / 'site', 'sqlite-cas')
    package(tmp_path / 'site', 'piloop-fast-role', '1.0', f'[piloop.roles]\nfast = {PLUGIN}:Fast\n')
    package(tmp_path / 'site', 'piloop-auditor', '1.0', f'[piloop.roles]\n{AUDITOR} = {PLUGIN}:Auditor\n')
    monkeypatch.syspath_prepend(tmp_path / 'site')
    monkeypatch.setenv('PILOOP_HOME', str(tmp_path))  # where the harness writes its niceness
    forget_loaded()
    write_toml(Path(campaign.path), HARNESS_NICENESS)
    with db.session() as s:
        cfg = load_campaign_config(
            Path(campaign.path), plugins={'runtime': {'use': 'nice'}, 'role': {AUDITOR: {'runtime': 'nice'}}}
        )
        s.get_one(Campaign, campaign.id).config = cfg
        s.flush()  # the config first: a row's kind must be a role of its campaign's
        s.add(AgentSession(kind=AUDITOR, campaign_id=campaign.id, reason='by hand'))
        s.commit()
    cas = f'[plugins.cas]\nprovider = "sqlite"\npath = "{tmp_path / "blobs.sqlite"}"\n'
    (tmp_path / 'config.toml').write_text(f'{cas}[plugins.runtime.nice]\nniceness = 5\n')
    home = Settings(home=tmp_path)
    assert isinstance(CAS.selected(home.providers).open(home.home), Covalence)
    daemon = Daemon(home)
    try:
        daemon.scheduler.reap()
        assert run_all(daemon.scheduler) == [1]
        daemon.scheduler.wait(SETTLE)
    finally:
        daemon.shutdown(timeout=5)
    with db.session() as s:
        agent_session = s.get_one(AgentSession, 1)
        assert (agent_session.state, agent_session.runtime) == (AgentSessionState.EXITED, 'nice')
    assert (tmp_path / 'niceness.txt').read_text().strip() == str(min(os.nice(0) + 5, 19))
    monkeypatch.setattr(common, 'settings', home)
    printed: list[str] = []
    monkeypatch.setattr(common.console, 'print', lambda text, **_: printed.append(text))
    cli.app(['plugins'], result_action='return_value', exit_on_error=False)
    lines = printed[0].splitlines()
    assert [line for line in lines if not line.startswith(' ')] == [
        *(f'{c.noun}s ({c.tier} tier, {c.group}):' for c in plugins_module.declared()),
        'selected:',
    ]
    assert '  nice        piloop-nice 0.1.0, entry point nice = piloop_nice:Nice' in lines
    toml, config = Path(campaign.path) / 'piloop.toml', tmp_path / 'config.toml'
    store = f'CAS provider sqlite, from piloop-sqlite-cas 0.1.0 ([plugins.cas] provider, in {config})'
    stored = f'in the config of demo that the last `piloop init` stored from {toml}'
    nice = 'runtime nice, from piloop-nice 0.1.0'
    selected = lines[lines.index('selected:') + 1 :]
    by_list = ('  demo: section ', '  demo: fact source ')
    assert (
        f'  demo: section goal, from {SECTIONS.sources()["goal"].release} ([plugins.section] use, {stored})' in selected
    )
    *listed, fast, auditor = [line for line in selected if not line.startswith(by_list)]
    assert listed == [
        f'  {store}',
        f'  demo: lineage provider optimization, from {LINEAGE.sources()["optimization"].release} ([plugins.lineage]'
        f' use, {stored})',
        f'  demo: {nice} ([plugins.runtime] use, {stored})',
    ]
    assert {fast, auditor} == {
        f'  demo: {nice} ([plugins.role.fast] runtime, {stored})',
        f'  demo: {nice} ([plugins.role.auditor] runtime, {stored})',
    }, 'two packages add the roles: they come in the order in which their entry points are found'
    with db.session() as s:
        row = s.get_one(Campaign, campaign.id)
        row.config = reconfigured(row.config, plugins={'runtime': {'use': 'subprocess'}})
        s.commit()
    built_in = RUNTIMES.sources()['subprocess'].release
    assert f'demo: runtime subprocess, from {built_in} ([plugins.runtime] use, {stored})' in selections(home)
    store_config(db, campaign.id, {})
    assert selections(home) == [store], 'a stored config that does not load binds nothing; piloop doctor says why'
    forget_loaded()


def keyless(capability: Capability[RuntimeConfig]) -> list[str]:
    """The providers that are instances without a table: those whose keys all have defaults (``docker`` needs one)."""
    return [
        n for n, p in capability.providers().items() if not any(f.is_required() for f in p.model.model_fields.values())
    ]


def test_a_runtime_that_is_not_registered_fails_with_the_registered_ones(campaign_path: Path, plugins: None) -> None:
    with pytest.raises(
        ConfigError,
        match=re.escape(
            f"plugins.runtime.use[0] = 'tmux': no [plugins.runtime.tmux]; there are {', '.join(keyless(RUNTIMES))}"
        ),
    ):
        load_campaign_config(campaign_path, plugins={'runtime': {'use': 'tmux'}})
    with pytest.raises(
        ConfigError, match=re.escape("plugins.role.implementer.runtime = 'tmux': no [plugins.runtime.tmux]")
    ):
        load_campaign_config(campaign_path, plugins={'role': {'implementer': {'runtime': 'tmux'}}})


def test_a_table_of_a_new_name_does_runs() -> None:
    with pytest.raises(ValueError, match=r'a role of your own does runs: set runs = true in \[plugins.role.mine\]'):
        new_role('mine', {})
    assert new_role('mine', {'runs': True}) is ImplementerRole
