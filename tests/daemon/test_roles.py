"""Roles as data: a sweeping role and a role that does runs that piloop.toml defines, new triggers for a built-in role,
and the roles, triggers and runtimes that packages add through entry points."""

import os
import re
import time
from collections.abc import Iterator
from importlib.metadata import EntryPoint
from pathlib import Path

import pytest
from covalence import Cas as Covalence
from sqlalchemy import select

from piloop import settings as settings_module
from piloop.actor import System
from piloop.agents.results import PromptOutcome
from piloop.campaign import ops
from piloop.campaign.stop import left
from piloop.cli import common, main as cli
from piloop.cli.plugins import selections
from piloop.config import ConfigError, load_campaign_config
from piloop.daemon.main import Daemon
from piloop.daemon.scheduler import Scheduler
from piloop.db.events import in_order
from piloop.db.models import AgentSession, AgentSessionState, Campaign, Comment, Event, utcnow
from piloop.db.session import Database
from piloop.ids import AgentSessionId, CampaignStatus, CommentKind, Ending, RoleName, TargetKind, TargetRef
from piloop.otel import Tracer
from piloop.plugins import host as plugins_module
from piloop.plugins.campaign_type.consumers import awake_roles, quiet_phase
from piloop.plugins.campaign_type.kit.backlog.ids import IdeaId, IdeaStatus
from piloop.plugins.campaign_type.kit.backlog.tables import Idea
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.evaluation.tables import Measurement
from piloop.plugins.campaign_type.kit.runs.controller import RunController
from piloop.plugins.campaign_type.kit.runs.ids import RunEnd, RunStatus
from piloop.plugins.campaign_type.kit.runs.tables import Run, working_on
from piloop.plugins.cas.contract import CAS
from piloop.plugins.escalation.contract import DECISION
from piloop.plugins.executor.workers import Workers
from piloop.plugins.harness.contract import HARNESSES
from piloop.plugins.host import Capability, PluginError, compose
from piloop.plugins.lineage.contract import LINEAGE
from piloop.plugins.role.contract import ROLES
from piloop.plugins.runtime.contract import RUNTIMES, Runtime, RuntimeConfig, RuntimeFactory
from piloop.plugins.section.contract import SECTIONS
from piloop.plugins.trigger.contract import TRIGGERS, Quiet
from piloop.settings import Settings
from tests.conftest import (
    END_RUN,
    HARNESS_APPEND,
    RESEARCH,
    SETTLE,
    WATCHDOG,
    ScriptRuntime,
    campaigns_of,
    console_lines,
    example,
    package,
    piloop_entry_points,
    queued,
    reconfigured,
    run_all,
    serving,
    store_config,
    wait_until,
    write_toml,
)
from tests.daemon.test_scheduler import add_ideas
from tests.daemon.test_triggers import BLOCKING, agent_sessions, fail_a_run, reported_events, wait_for

REVIEW = "SELECT id FROM comments WHERE kind = 'decision' AND created_at > :since"
PLUGIN = 'tests.fixtures.plugin'
TRIGGER_GROUP, ROLE_GROUP, RUNTIME_GROUP = TRIGGERS.group, ROLES.group, RUNTIMES.group


def recording(runtime: ScriptRuntime, kinds: list[str]) -> RuntimeFactory:
    """A factory that runs every agent in ``runtime`` and records the kind of runtime that was asked for."""

    def make(kind: str) -> Runtime:
        kinds.append(kind)
        return runtime

    return make


def scheduler(db: Database, make: RuntimeFactory, tmp_path: Path) -> Scheduler:
    tracer = Tracer(None)
    return Scheduler(db, serving(RunController(db, tracer, tmp_path, make, executor=Workers())), tracer, tmp_path, make)


REVIEWED: dict[str, object] = {'reviewed': {'provider': 'sql', 'when': REVIEW}}
"""``[plugins.trigger.reviewed]``: a ``sql`` trigger on a new decision (``REVIEW``)."""


def configure(
    db: Database,
    campaign: Campaign,
    harness_body: str,
    *,
    trigger: dict[str, object] | None = None,
    **roles: dict[str, object],
) -> None:
    """Rewrite the campaign's harness script, role settings and ``trigger`` tables (merged over the test toml)."""
    write_toml(Path(campaign.path), harness_body)
    with db.session() as s:
        cfg = load_campaign_config(Path(campaign.path), plugins={'role': roles, 'trigger': trigger or {}})
        s.get_one(Campaign, campaign.id).config = cfg
        s.commit()


def decide(db: Database, campaign: Campaign, body: str) -> None:
    with db.session() as s:
        s.add(
            Comment(
                target=TargetRef(TargetKind.CAMPAIGN, campaign.id),
                author='human',
                kind=DECISION,
                body=body,
            )
        )
        s.commit()
    time.sleep(0.01)


@pytest.mark.usefixtures('no_fresh_runs')
def test_a_role_that_piloop_toml_defines_runs_on_its_sql_trigger_with_its_prompt_and_runtime(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path, typed: None
) -> None:
    """A ``[plugins.role.reviewer]`` table with a ``sql`` trigger: each new decision requests one reviewer.

    The reviewer runs with its own prompt, in a worktree of its own, under its own runtime, one at a time.
    """
    release = tmp_path / 'release'
    (Path(campaign.path) / 'reviewer.md').write_text('# Review\nRead the latest decision and say if it holds.\n')
    harness = f'#!/bin/sh\npwd > {tmp_path}/cwd.txt\n' + BLOCKING.format(release=release).removeprefix('#!/bin/sh\n')
    reviewer: dict[str, object] = {
        'prompt': 'reviewer.md',
        'triggers': ['reviewed'],
        'runtime': 'typed',
        'harnesses': ['script'],
        'max_concurrent': 1,
    }
    configure(db, campaign, harness, reviewer=reviewer, trigger=REVIEWED)
    kinds: list[str] = []
    sched = scheduler(db, recording(runtime, kinds), tmp_path)
    assert run_all(sched) == [], 'no decision yet: the query returns no row'

    decide(db, campaign, 'keep the cache')
    assert run_all(sched) == [1]
    wait_for(db, AgentSessionId(1), AgentSessionState.RUNNING)
    assert run_all(sched) == [], 'the decision is older than the running reviewer'
    decide(db, campaign, 'drop the cache')
    assert run_all(sched) == [], 'max_concurrent = 1: the second reviewer waits'
    assert run_all(sched) == [], 'one pending request is enough'
    reason = f'sql: {REVIEW}'
    assert agent_sessions(db, 'reviewer') == [
        (1, AgentSessionState.RUNNING, reason),
        (2, AgentSessionState.REQUESTED, reason),
    ]
    release.touch()
    sched.wait(SETTLE)
    assert run_all(sched) == [2], 'the slot is free'
    sched.wait(SETTLE)
    sched.stop(timeout=5)

    brief = (tmp_path / 'agent_sessions' / '1' / 'brief.md').read_text()
    assert brief.startswith('# Review\nRead the latest decision')
    assert '- You are reviewer#1, the reviewer for campaign#1 (demo).' in brief
    assert f'- Why this agent started (context, not a scope): {reason}' in brief
    assert 'You file no ideas.' in brief and '- You may write:' not in brief, (
        'a new role files no ideas and states no writes'
    )
    last = tmp_path / 'agent_sessions' / '2' / 'worktree'
    assert Path((tmp_path / 'cwd.txt').read_text().strip()) == last.resolve()
    assert not last.exists(), 'its worktree goes when it ends'
    with db.session() as s:
        rows = s.scalars(select(AgentSession).where(AgentSession.kind == 'reviewer')).all()
        assert [(w.state, w.runtime, w.harness) for w in rows] == [(AgentSessionState.EXITED, 'typed', 'script')] * 2
    assert 'typed' in kinds


@pytest.mark.usefixtures('no_fresh_runs')
def test_a_role_adds_its_incident_prompt_to_an_incident_sweep_and_its_routine_prompt_to_any_other(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    """The keys that give the watchdog its two kinds of sweep work for a role that ``piloop.toml`` defines."""
    repo = Path(campaign.path)
    for name, text in (('triage.md', '# Triage'), ('incident.md', '## An incident'), ('routine.md', '## A routine')):
        (repo / name).write_text(text + '\n')
    triage: dict[str, object] = {
        'prompt': 'triage.md',
        'triggers': ['run_failed'],
        'incident_prompt': 'incident.md',
        'routine_prompt': 'routine.md',
    }
    configure(db, campaign, '#!/bin/sh\ntrue\n', triage=triage, watchdog={'triggers': []})
    sched = scheduler(db, recording(runtime, []), tmp_path)
    fail_a_run(db, campaign, 'broken')
    assert run_all(sched) == [1]
    sched.wait(SETTLE)
    with db.session() as s:
        s.add(AgentSession(kind='triage', campaign_id=campaign.id, reason='by hand'))
        s.commit()
    assert run_all(sched) == [2]
    sched.wait(SETTLE)
    sched.stop(timeout=5)
    incident, routine = ((tmp_path / 'agent_sessions' / str(w) / 'brief.md').read_text() for w in (1, 2))
    assert incident.startswith('# Triage\n\n## An incident\n\n# Demo') and '## Incidents to investigate' in incident
    assert routine.startswith('# Triage\n\n## A routine\n\n# Demo') and '## Research pack' in routine
    with pytest.raises(ConfigError, match=r"plugins\.role\.triage\.routine_prompt = 'gone\.md': no such file"):
        load_campaign_config(repo, plugins={'role': {'triage': triage | {'routine_prompt': 'gone.md'}}})


@pytest.mark.usefixtures('no_fresh_runs')
def test_a_role_that_defines_ideas_is_told_how_many_its_sweep_may_add(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    """``low_water`` counts on ``max_ideas`` from each sweep of the role, so the brief of the sweep states it."""
    (Path(campaign.path) / 'ideator.md').write_text('# Ideate\n')
    ideator: dict[str, object] = {
        'prompt': 'ideator.md',
        'triggers': ['ideator_low_water'],
        'files': 'ideas',
        'max_ideas': 2,
    }
    configure(
        db,
        campaign,
        '#!/bin/sh\ntrue\n',
        ideator=ideator,
        research={'triggers': []},
        watchdog={'triggers': []},
        trigger={'ideator_low_water': {'provider': 'low_water', 'buffer': 1}},
    )
    sched = scheduler(db, recording(runtime, []), tmp_path)
    assert run_all(sched) == [1], 'no runnable idea: the backlog is short'
    sched.wait(SETTLE)
    sched.stop(timeout=5)
    brief = (tmp_path / 'agent_sessions' / '1' / 'brief.md').read_text()
    assert (
        '- New ideas of your own in this sweep: at most 2 (`max_ideas`), each with a plan of at most 150 words' in brief
    )
    [(_, _, reason)] = agent_sessions(db, 'ideator')
    assert reason is not None and reason.startswith('1 free implementer slots without a runnable idea')


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
def test_a_role_that_does_runs_works_with_its_own_prompt_and_its_commits_are_scored(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    """``[plugins.role.quick]`` with ``runs = true`` gets a fresh run of its own, as the implementer does.

    The quick agent works with its own prompt in a new worktree at the head, and its commit is measured and judged.
    """
    (Path(campaign.path) / 'quick.md').write_text('# Quick\nTry the idea in a few minutes.\n')
    configure(db, campaign, HARNESS_SIGNS, quick=QUICK)
    (deep,) = add_ideas(db, campaign, 'deep', priority=1)
    (cheap,) = add_ideas(db, campaign, 'cheap', priority=3)
    sched = scheduler(db, recording(runtime, []), tmp_path)
    assert queued(db, sched, campaign.id, IdeaId(cheap), IdeaId(deep)) == [1, 2]
    assert sorted(run_all(sched)) == [1, 2]
    sched.wait(SETTLE)
    sched.stop(timeout=5)
    assert runs_by_role(db) == [(1, 'quick', cheap, 'quick'), (2, 'implementer', deep, 'implementer')]
    assert (tmp_path / 'runs' / '1' / 'brief.md').read_text().startswith('# Quick\nTry the idea')
    assert (tmp_path / 'runs' / '2' / 'brief.md').read_text().startswith('# Implementer\n')
    with db.session() as s:
        assert s.get_one(Run, 1).status is RunStatus.SUCCEEDED
        candidate = s.scalars(select(Candidate).where(Candidate.run_id == 1)).one()
        assert candidate.verdict is not None
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
def test_a_built_in_role_takes_other_triggers(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    """The watchdog without its ``anomaly`` trigger ignores a failure; its timer still fires at its ``interval``."""
    configure(
        db,
        campaign,
        '#!/bin/sh\ntrue\n',
        watchdog={'triggers': ['cron'], 'interval': 30},
        research={'triggers': ['reviewed']},
        trigger=REVIEWED,
    )
    sched = scheduler(db, recording(runtime, []), tmp_path)
    fail_a_run(db, campaign, 'broken')
    assert run_all(sched) == [], 'no anomaly trigger, and the timer is not due'
    with db.session() as s:
        s.get_one(Campaign, campaign.id).created_at = utcnow().replace(year=2000)
        s.commit()
    decide(db, campaign, 'look again')
    assert run_all(sched) == [1, 2]
    sched.wait(SETTLE)
    sched.stop(timeout=5)
    assert agent_sessions(db, RESEARCH) == [(1, AgentSessionState.EXITED, f'sql: {REVIEW}')]
    assert agent_sessions(db, WATCHDOG) == [(2, AgentSessionState.EXITED, 'timer: 30s')], 'the roles in their order'
    assert reported_events(db) == [], 'the watchdog got no anomaly'


@pytest.mark.usefixtures('no_fresh_runs')
def test_a_request_for_a_role_that_piloop_toml_no_longer_has_is_refused(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    (Path(campaign.path) / 'reviewer.md').write_text('# Review\n')
    configure(db, campaign, '#!/bin/sh\ntrue\n', reviewer={'prompt': 'reviewer.md', 'triggers': []})
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


@pytest.mark.usefixtures('no_fresh_runs')
def test_a_message_to_a_sweep_that_has_not_started_is_in_its_brief_and_is_not_sent_again(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    configure(db, campaign, '#!/bin/sh\ntrue\n', watchdog={'triggers': []})
    with db.session() as s:
        s.add(AgentSession(kind=WATCHDOG, campaign_id=campaign.id, reason='by hand'))
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
    assert '\n# Notes\nlook at run#4 first\n' in brief and 'a note is on the board only' not in brief
    with db.session() as s:
        got = in_order(s, PromptOutcome, Event.target == on_it)
        assert [(o.kind, o.comment) for _, o in got] == [('comment.delivered', 1)]


@pytest.mark.usefixtures('no_fresh_runs')
def test_a_hand_over_leaves_a_sweep_running_and_the_next_scheduler_re_attaches_to_it(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    hold = tmp_path / 'hold'
    hold.touch()
    harness = f'#!/bin/sh\necho $$ > {tmp_path}/agent.pid\nwhile [ -f {hold} ]; do sleep 0.05; done\n'
    configure(db, campaign, harness, watchdog={'triggers': []})
    with db.session() as s:
        s.add(AgentSession(kind=WATCHDOG, campaign_id=campaign.id, reason='by hand'))
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
        sweep = s.get_one(AgentSession, 1)
    assert (sweep.state, sweep.error) == (AgentSessionState.EXITED, None), 'the next daemon watched it to its end'
    assert Path(campaign.path).is_dir()
    second.stop(timeout=5)


ENTRY_POINTS = [
    EntryPoint('every_pass', f'{PLUGIN}:EveryPass', TRIGGER_GROUP),
    EntryPoint('awake', f'{PLUGIN}:Awake', TRIGGER_GROUP),
    EntryPoint('broken', f'{PLUGIN}:Broken', TRIGGER_GROUP),
    EntryPoint('auditor', f'{PLUGIN}:Auditor', ROLE_GROUP),
    EntryPoint('quick', f'{PLUGIN}:Quick', ROLE_GROUP),
    EntryPoint('typed', f'{PLUGIN}:Typed', RUNTIME_GROUP),
]


def forget_loaded() -> None:
    """Forget the loaded plugins, and the instances of ``config.toml`` that the process validated with them."""
    plugins_module.close()
    vars(settings_module.settings).pop('providers', None)


def installed(monkeypatch: pytest.MonkeyPatch, entries: list[EntryPoint]) -> None:
    """Make piloop's own entry points and ``entries`` the installed ones, and forget the ones that were loaded."""

    def entry_points(*, group: str) -> list[EntryPoint]:
        return [*piloop_entry_points(group), *(ep for ep in entries if ep.group == group)]

    monkeypatch.setattr(plugins_module, 'entry_points', entry_points)
    forget_loaded()


@pytest.fixture
def typed(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Install a package that adds only the ``typed`` runtime."""
    installed(monkeypatch, [EntryPoint('typed', f'{PLUGIN}:Typed', RUNTIME_GROUP)])
    yield
    forget_loaded()


@pytest.fixture
def plugins(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    installed(monkeypatch, ENTRY_POINTS)
    yield
    forget_loaded()


@pytest.mark.usefixtures('no_fresh_runs')
def test_a_package_adds_a_role_and_triggers_through_entry_points(
    db: Database,
    campaign: Campaign,
    runtime: ScriptRuntime,
    tmp_path: Path,
    plugins: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``piloop init`` stores the package's role with its defaults, under the table of ``piloop.toml``.

    The daemon runs it on the package's trigger. A trigger that raises is traced, and the role's other triggers fire.
    """
    cfg = load_campaign_config(Path(campaign.path))
    auditor = cfg.roles.sweeping()[RoleName('auditor')]
    assert (auditor.timeout, auditor.triggers) == (5, ['every_pass'])
    toml = Path(campaign.path) / 'piloop.toml'
    toml.write_text(toml.read_text() + '\n[plugins.role.auditor]\ntimeout = 9\n')
    assert load_campaign_config(Path(campaign.path)).roles.sweeping()[RoleName('auditor')].timeout == 9, (
        'the table goes over'
    )
    configure(
        db,
        campaign,
        '#!/bin/sh\ntrue\n',
        auditor={'triggers': ['broken', 'every_pass']},
    )
    sched = scheduler(db, recording(runtime, []), tmp_path)
    assert run_all(sched) == [1]
    sched.wait(SETTLE)
    sched.stop(timeout=5)
    assert agent_sessions(db, 'auditor') == [(1, AgentSessionState.EXITED, 'every pass for auditor')]
    brief = (tmp_path / 'agent_sessions' / '1' / 'brief.md').read_text()
    assert brief.startswith('# Audit\n')
    lines = console_lines(capsys.readouterr().err)
    assert 'trigger.failed Ns campaign=demo role=auditor trigger=broken error=the probe is down' in lines


def test_only_a_trigger_that_names_the_quiet_phase_fires_and_launches_while_its_campaign_is_paused_or_stopped(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path, plugins: None
) -> None:
    configure(db, campaign, '#!/bin/sh\ntrue\n', auditor={'triggers': ['every_pass', 'awake']})
    with db.session() as s:
        ops.pause(s, campaign.id, evals=False, reason='a person reads the results')
        s.add(AgentSession(kind=WATCHDOG, campaign_id=campaign.id, reason='by hand'))
        s.commit()
    sched = scheduler(db, recording(runtime, []), tmp_path)
    [audit] = run_all(sched)
    sched.wait(SETTLE)
    assert agent_sessions(db, 'auditor') == [(audit, AgentSessionState.EXITED, 'every pass for auditor (paused)')], (
        'every_pass sleeps through the pause, and awake fires alone'
    )
    assert [state for _, state, _ in agent_sessions(db, WATCHDOG)] == [AgentSessionState.REQUESTED], (
        'a role without such a trigger launches nothing while paused'
    )
    with db.session() as s:
        ops.resume(s, campaign.id)
        ops.stop(s, campaign.id, System.HUMAN, reason='done', ending=Ending.WRAP_UP)
        s.commit()
    run_all(sched)
    sched.wait(SETTLE)
    run_all(sched)
    sched.wait(SETTLE)
    sched.stop(timeout=5)
    reasons = [reason for _, _, reason in agent_sessions(db, 'auditor')]
    assert 'every pass for auditor (stopped)' in reasons, 'checked again once the campaign is closed'
    with db.session() as s:
        row = s.get_one(Campaign, campaign.id)
        assert (quiet_phase(s, row, None), quiet_phase(s, row, 'at the spend limit')) == (Quiet.STOPPED,) * 2
        row.status, row.paused = CampaignStatus.ACTIVE, None
        assert (quiet_phase(s, row, None), quiet_phase(s, row, 'at the spend limit')) == (None, Quiet.HELD)
        row.status = CampaignStatus.RETIRED
        with pytest.raises(ValueError, match='demo is retired'):
            quiet_phase(s, row, None)
        row.status = CampaignStatus.ACTIVE
        assert awake_roles(row.config, None) == frozenset()
        assert awake_roles(row.config, Quiet.HELD) == {RoleName('auditor')}


MINIMAL = """
name = "demo"
evaluate = ["true"]

[[metrics]]
name = "score"
direction = "higher"
role = "primary"

[plugins.harness.pi]
model = "m"

{roles}
"""


def test_a_package_role_takes_its_defaults_and_a_roles_value_that_is_not_a_table_fails(
    campaign_path: Path, plugins: None
) -> None:
    toml = campaign_path / 'piloop.toml'
    toml.write_text(MINIMAL.format(roles='[plugins.role.auditor]'))
    assert load_campaign_config(campaign_path).roles.sweeping()[RoleName('auditor')].timeout == 5, (
        'an empty table: the defaults'
    )
    toml.write_text(MINIMAL.format(roles='[plugins.role]\nauditor = 3'))
    with pytest.raises(ConfigError, match=r'plugins\.role\.auditor = 3: a role is a table'):
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
            EntryPoint('not_a_trigger', f'{PLUGIN}:NOT_A_TRIGGER', TRIGGER_GROUP),
            'is not a subclass of piloop.plugins.trigger.contract.Trigger',
        ),
        (EntryPoint('cron', f'{PLUGIN}:EveryPass', TRIGGER_GROUP), "'cron' is a trigger already, from piloop "),
        (
            EntryPoint('auditor', f'{PLUGIN}:NOT_A_ROLE', ROLE_GROUP),
            'is not a subclass of piloop.plugins.role.contract.RoleConfig',
        ),
        (EntryPoint('research', f'{PLUGIN}:Auditor', ROLE_GROUP), "'research' is a role already, from piloop "),
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
    installed(monkeypatch, [EntryPoint('every_pass', f'{PLUGIN}:EveryPass', TRIGGER_GROUP), entry])
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
    monkeypatch.syspath_prepend(tmp_path / 'site')
    monkeypatch.setenv('PILOOP_HOME', str(tmp_path))  # where the harness writes its niceness
    forget_loaded()
    write_toml(Path(campaign.path), HARNESS_NICENESS)
    with db.session() as s:
        cfg = load_campaign_config(
            Path(campaign.path), plugins={'runtime': {'use': 'nice'}, 'role': {'watchdog': {'runtime': 'nice'}}}
        )
        s.get_one(Campaign, campaign.id).config = cfg
        s.add(AgentSession(kind=WATCHDOG, campaign_id=campaign.id, reason='by hand'))
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
    research_model = f'[plugins.role.research] model, {stored}'
    selected = lines[lines.index('selected:') + 1 :]
    by_list = ('  demo: section ', '  demo: trigger ', '  demo: fact source ')
    assert (
        f'  demo: trigger cron, from {TRIGGERS.sources()["cron"].release} ([plugins.role.watchdog] triggers, {stored})'
        in selected
    )
    assert (
        f'  demo: section goal, from {SECTIONS.sources()["goal"].release} ([plugins.section] use, {stored})' in selected
    )
    assert [line for line in selected if not line.startswith(by_list)] == [
        f'  {store}',
        *(
            f'  demo: harness provider {h}, from {HARNESSES.sources()[h].release} ({research_model})'
            for h in ('claude', 'codex')
        ),
        f'  demo: lineage provider optimization, from {LINEAGE.sources()["optimization"].release} ([plugins.lineage]'
        f' use, {stored})',
        f'  demo: {nice} ([plugins.runtime] use, {stored})',
        f'  demo: {nice} ([plugins.role.watchdog] runtime, {stored})',
        f'  demo: {nice} ([plugins.role.fast] runtime, {stored})',
    ]
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
        ConfigError, match=re.escape("plugins.role.research.runtime = 'tmux': no [plugins.runtime.tmux]")
    ):
        load_campaign_config(campaign_path, plugins={'role': {'research': {'runtime': 'tmux'}}})
