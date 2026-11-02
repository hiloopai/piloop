"""Each trigger in the roles docs fires exactly when the docs say, and requests launch within ``max_concurrent``."""

import sqlite3
import time
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from piloop.actor import System
from piloop.config import CampaignConfig, load_campaign_config
from piloop.daemon.main import ACTOR
from piloop.daemon.scheduler import Scheduler
from piloop.db import sql
from piloop.db.events import AnomalyReported, in_order
from piloop.db.models import AgentSession, AgentSessionState, Campaign, Comment, utcnow
from piloop.db.session import Database
from piloop.ids import AgentSessionId, CommentKind, TargetKind, TargetRef
from piloop.otel import Tracer
from piloop.plugins.campaign_type.kit.backlog.ids import IdeaStatus
from piloop.plugins.campaign_type.kit.backlog.tables import Idea
from piloop.plugins.campaign_type.kit.candidates.ids import Outcome
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.evaluation.tables import Measurement
from piloop.plugins.campaign_type.kit.runs.controller import RunController
from piloop.plugins.campaign_type.kit.runs.ids import IMPLEMENTER, RUN, RunId, RunStatus
from piloop.plugins.campaign_type.kit.runs.role.watchdog import WatchdogRole
from piloop.plugins.campaign_type.kit.runs.tables import Run
from piloop.plugins.campaign_type.optimization.planner import NO_RUN_ERROR
from piloop.plugins.executor.workers import Workers
from piloop.plugins.trigger.cron import CronTrigger
from tests.conftest import (
    HARNESS_NOOP,
    HARNESS_PROMPT_TO_FILE,
    SETTLE,
    WATCHDOG,
    ScriptRuntime,
    campaigns_of,
    end_of,
    factory,
    role_of,
    run_all,
    serving,
    write_toml,
)

BLOCKING = '#!/bin/sh\nwhile [ ! -f {release} ]; do sleep 0.05; done\n'


pytestmark = pytest.mark.usefixtures('no_fresh_runs')


def scheduler(db: Database, runtime: ScriptRuntime, tmp_path: Path) -> Scheduler:
    controller = serving(RunController(db, Tracer(None), tmp_path, factory(runtime), executor=Workers()))
    return Scheduler(db, controller, Tracer(None), tmp_path, factory(runtime))


def configure(
    db: Database,
    campaign: Campaign,
    harness_body: str = HARNESS_NOOP,
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


def agent_sessions(db: Database, kind: str) -> list[tuple[int, AgentSessionState, str | None]]:
    with db.session() as s:
        rows = s.scalars(select(AgentSession).where(AgentSession.kind == kind).order_by(AgentSession.id)).all()
        return [(w.id, w.state, w.reason) for w in rows]


def fail_a_run(db: Database, campaign: Campaign, title: str, status: RunStatus = RunStatus.FAILED) -> RunId:
    with db.session() as s:
        idea = Idea(
            track='score',
            campaign_id=campaign.id,
            title=title,
            hypothesis=title,
            created_by='t',
            status=IdeaStatus.DONE,
        )
        s.add(idea)
        s.flush()
        run = Run(
            campaign_id=idea.campaign_id,
            idea_id=idea.id,
            attempt=1,
            base_candidate_id=1,
            harness='h',
            model='m',
            status=status,
            end=end_of(status),
            finished_at=utcnow(),
        )
        s.add(run)
        s.commit()
        return run.id


def finish_a_run(db: Database, campaign: Campaign, title: str, verdict: Outcome = Outcome.DISCARD) -> RunId:
    """Add a run that succeeded just now with a measured candidate, a discard by default.

    The watchdog trigger sees no failure.
    """
    time.sleep(0.01)
    run_id = fail_a_run(db, campaign, title, RunStatus.SUCCEEDED)
    with db.session() as s:
        candidate = Candidate(
            campaign_id=campaign.id,
            run_id=run_id,
            parent_id=1,
            tree_sha=f'tree{run_id}',
            revision=f'commit{run_id}',
            verdict=verdict,
        )
        s.add(candidate)
        s.flush()
        evaluator_id = s.get_one(Campaign, campaign.id).evaluator_id or 0
        s.add(Measurement(candidate_id=candidate.id, evaluator_id=evaluator_id, metrics={'score': 0.0}))
        s.commit()
    return run_id


def wait_for(db: Database, agent_session_id: AgentSessionId, state: AgentSessionState) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with db.session() as s:
            if s.get_one(AgentSession, agent_session_id).state is state:
                return
        time.sleep(0.02)
    raise AssertionError(f'implementer#{agent_session_id} never reached {state}')


def backdate(db: Database, agent_session_id: AgentSessionId, seconds: float) -> None:
    with db.session() as s:
        w = s.get_one(AgentSession, agent_session_id)
        w.started_at = w.started_at - timedelta(seconds=seconds)
        if w.ended_at is not None:
            w.ended_at = w.ended_at - timedelta(seconds=seconds)
        s.commit()


def reported_events(db: Database) -> list[tuple[int, AnomalyReported]]:
    with db.session() as s:
        return [(e.target_id, p) for e, p in in_order(s, AnomalyReported)]


def test_an_anomaly_requests_an_incident_sweep_once_and_collapses_while_one_is_pending(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    release = tmp_path / 'release'
    configure(db, campaign, BLOCKING.format(release=release))
    sched = scheduler(db, runtime, tmp_path)
    assert run_all(sched) == [], 'nothing went wrong yet'

    fail_a_run(db, campaign, 'first')
    assert run_all(sched) == [1], 'requested and launched on the same tick'
    assert run_all(sched) == [], 'the same failure does not fire twice'
    [(agent_session, payload)] = reported_events(db)
    assert (agent_session, payload.subject, payload.trigger) == (1, TargetRef(RUN, 1), 'run_failed')
    line = payload.line
    assert line.startswith('- run#1 failed, ') and line.endswith(' ago: failed on h (idea#1): no error recorded')

    fail_a_run(db, campaign, 'second', RunStatus.TIMED_OUT)
    assert run_all(sched) == [], 'max_concurrent=1: the sweep for the second failure waits'
    fail_a_run(db, campaign, 'third')
    run_all(sched)
    wait_for(db, AgentSessionId(1), AgentSessionState.RUNNING)
    assert agent_sessions(db, WATCHDOG) == [
        (1, AgentSessionState.RUNNING, 'run_failed: run#1 failed'),
        (2, AgentSessionState.REQUESTED, 'run_failed: run#2 failed'),
    ], 'one pending request absorbs later anomalies'

    release.touch()
    sched.wait(SETTLE)
    assert run_all(sched) == [2], 'the slot frees; the pending request launches'
    sched.wait(SETTLE)
    assert run_all(sched) == [3], 'the anomaly that came while a request was pending gets its own sweep'
    assert [(w, str(p.subject), p.trigger) for w, p in reported_events(db)] == [
        (1, 'run#1', 'run_failed'),
        (2, 'run#2', 'run_failed'),
        (3, 'run#3', 'run_failed'),
    ]
    sched.wait(SETTLE)
    sched.stop(timeout=5)
    again = scheduler(db, runtime, tmp_path)
    assert run_all(again) == [], 'the events are the record: a restarted daemon replays nothing'
    again.stop(timeout=5)
    assert [w[1] for w in agent_sessions(db, WATCHDOG)] == [AgentSessionState.EXITED] * 3


def test_an_anomaly_takes_the_tick_and_does_not_reset_the_timer(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    """When a timer is due and a new failure arrives in the same tick, the incident sweep starts first."""
    configure(db, campaign, HARNESS_PROMPT_TO_FILE, watchdog={'interval': 30})
    with db.session() as s:
        s.get_one(Campaign, campaign.id).created_at = utcnow() - timedelta(seconds=31)
        s.commit()
    fail_a_run(db, campaign, 'broken')
    sched = scheduler(db, runtime, tmp_path)
    assert run_all(sched) == [1]
    sched.wait(SETTLE)
    assert run_all(sched) == [2], 'the incident sweep is not a progress review: the timer is still due'
    sched.wait(SETTLE)
    assert agent_sessions(db, WATCHDOG) == [
        (1, AgentSessionState.EXITED, 'run_failed: run#1 failed'),
        (2, AgentSessionState.EXITED, 'timer: 30s'),
    ]
    assert run_all(sched) == [], 'the timer counts from the progress review'

    incident = (tmp_path / 'agent_sessions' / '1' / 'brief.md').read_text()
    assert incident.index('# Watchdog') < incident.index('## This sweep: an incident') < incident.index('# Demo')
    assert '## Incidents to investigate\n\n- run#1 failed' in incident
    assert '## Goal' in incident and '## Research pack' not in incident
    progress = (tmp_path / 'agent_sessions' / '2' / 'brief.md').read_text()
    assert '## This sweep: a progress review' in progress and '## This sweep: an incident' not in progress
    assert '## Incidents to investigate' not in progress
    assert '## Goal' in progress and '## Research pack' not in progress, 'a review judges the work, it proposes none'
    sched.stop(timeout=5)


def test_a_watchdog_brief_shows_a_sweep_s_sections_without_the_research_pack_unless_its_list_names_it() -> None:
    shown = WatchdogRole().shown()
    assert 'goal' in shown and 'pack' not in shown
    assert WatchdogRole(sections=['goal', 'pack']).shown() == ['goal', 'pack']


def test_an_idle_queue_requests_no_sweep(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    """An idle queue is a cost that the progress review weighs (the ``idle_queue`` section), not an incident."""
    sched = scheduler(db, runtime, tmp_path)
    assert run_all(sched) == []
    assert agent_sessions(db, WATCHDOG) == []
    sched.stop(timeout=1)


def test_cron_fires_once_per_interval_when_the_role_has_news(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    """A routine sweep with nothing new to read finds nothing: the timer waits for news, up to ``max_interval``."""
    configure(db, campaign, watchdog={'interval': 30})
    sched = scheduler(db, runtime, tmp_path)
    assert run_all(sched) == [], 'the campaign was registered a moment ago'
    with db.session() as s:
        s.get_one(Campaign, campaign.id).created_at = utcnow() - timedelta(seconds=31)
        s.commit()
    assert run_all(sched) == [1], 'the first review needs no news'
    assert agent_sessions(db, WATCHDOG)[0][2] == 'timer: 30s'
    sched.wait(SETTLE)
    assert run_all(sched) == [], 'not due again'
    backdate(db, AgentSessionId(1), 31)
    assert run_all(sched) == [], 'due, but nothing is new since the last review'
    with db.session() as s:
        p = s.get_one(Campaign, campaign.id)
        cfg = CampaignConfig.model_validate(p.config)
        on = campaigns_of(sched)._pass(s, p, cfg, WATCHDOG, role_of(cfg, WatchdogRole), utcnow())
        due = CronTrigger().due_in(on)
        assert due is not None and timedelta(hours=3, minutes=59) < due < timedelta(hours=4), (
            'max_interval, unless news'
        )

    with db.session() as s:
        s.add(
            Comment(
                target=TargetRef(TargetKind.CAMPAIGN, campaign.id),
                author='human',
                kind=CommentKind.MESSAGE,
                priority=2,
                body='look at idea#1',
            )
        )
        s.commit()
    assert run_all(sched) == [2]
    assert agent_sessions(db, WATCHDOG)[1][2] == 'timer: 30s; news: 1 items (comment)'
    sched.wait(SETTLE)
    backdate(db, AgentSessionId(2), 31)
    assert run_all(sched) == [], 'the review took that news'
    backdate(db, AgentSessionId(2), 4 * 3600)
    assert run_all(sched) == [3], 'after max_interval, a stuck campaign is still looked at'
    assert agent_sessions(db, WATCHDOG)[2][2] == 'timer: no news since agent_session#2'
    sched.wait(SETTLE)

    configure(db, campaign, watchdog={'interval': 30}, trigger={'cron': {'max_interval': 0}})
    backdate(db, AgentSessionId(3), 31)
    assert run_all(sched) == [4], 'max_interval at or below interval: the timer alone'
    sched.wait(SETTLE)

    configure(db, campaign, watchdog={'interval': 0})
    backdate(db, AgentSessionId(4), 5 * 3600)
    assert run_all(sched) == [], 'interval 0 turns the timer off'
    sched.stop(timeout=5)


def test_a_sweep_whose_launch_the_stop_rule_stops_never_starts_and_the_stop_closes_its_row(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    """The rule answers ``Stop`` between the pass that requested a sweep and its launch: the run that it waited on
    settled. The sweep never starts, so it is not killed after 0 seconds; the pass that stops the campaign closes it,
    as every requested row."""
    cfg = load_campaign_config(
        Path(campaign.path), plugins={'stop_rule': {'max_runs': 1}, 'role': {'watchdog': {'triggers': []}}}
    )
    with db.session() as s:
        s.get_one(Campaign, campaign.id).config = cfg
        row = AgentSession(kind=WATCHDOG, campaign_id=campaign.id, reason='by hand')
        s.add(row)
        s.flush()
        requested = (row.id, AgentSessionState.REQUESTED, 'by hand')
        row.state, row.harness = AgentSessionState.STARTING, 'script'  # the pass took it to launch
        s.commit()
    finish_a_run(db, campaign, 'one')
    sched = scheduler(db, runtime, tmp_path)
    campaigns_of(sched)._sweep(AgentSessionId(requested[0]))
    assert agent_sessions(db, WATCHDOG) == [requested], 'never started: requested again'
    run_all(sched)
    sched.wait(SETTLE)
    sched.stop(timeout=5)
    with db.session() as s:
        stopped = s.get_one(Campaign, campaign.id).stopped_reason
        w = s.get_one(AgentSession, requested[0])
        assert (stopped, w.state, w.error, w.session_id, w.log_dir) == (
            'max_runs: 1 runs',
            AgentSessionState.KILLED,
            'campaign_stopped',
            None,
            None,
        ), 'closed as every requested row of a stopped campaign: no session, no place'


def test_ideas_updated_at_is_bumped_by_raw_sql_and_orm_updates_alike(
    db: Database, campaign: Campaign, tmp_path: Path
) -> None:
    with db.session() as s:
        idea = Idea(track='score', campaign_id=campaign.id, title='t', hypothesis='h', created_by='human')
        s.add(idea)
        s.commit()
        created = idea.updated_at
        assert idea.updated_by is None, 'no update yet'

    def stamp() -> tuple[datetime, str | None]:
        with db.session() as s:
            row = s.get_one(Idea, idea.id)
            return row.updated_at, row.updated_by

    time.sleep(0.01)
    with closing(sql.connect(tmp_path / 'piloop.db', actor=System.HUMAN)) as con:
        sql.run(con, f"UPDATE ideas SET status = 'dropped' WHERE id = {idea.id}")
    raw, by = stamp()
    assert raw > created and by == 'human'

    time.sleep(0.01)
    with db.session() as s:
        same = s.get_one(Idea, idea.id)
        same.priority = 0
        s.commit()
        orm, by = stamp()
        assert orm > raw and by == ACTOR.signature
        assert (same.updated_at, same.updated_by) == (orm, ACTOR.signature), (
            'the instance re-reads what the triggers wrote'
        )

    pinned = created - timedelta(days=1)
    with closing(sql.connect(tmp_path / 'piloop.db', actor=System.HUMAN)) as con:
        sql.run(
            con,
            f"UPDATE ideas SET status = 'done', updated_at = '{pinned:%Y-%m-%d %H:%M:%S.%f}' WHERE id = {idea.id}",
        )
    assert stamp() == (pinned, 'human'), 'a statement that sets updated_at itself is left alone'


def test_manual_requests_are_launched_under_max_concurrent(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    release = tmp_path / 'release'
    configure(db, campaign, BLOCKING.format(release=release), watchdog={'triggers': []})
    with closing(sql.connect(tmp_path / 'piloop.db', actor=System.HUMAN)) as con:
        for _ in range(2):
            sql.run(
                con,
                f"INSERT INTO agent_sessions (kind, campaign_id, state) VALUES ('watchdog', {campaign.id}, 'requested')",
            )
    sched = scheduler(db, runtime, tmp_path)
    assert run_all(sched) == [1]
    assert run_all(sched) == []
    wait_for(db, AgentSessionId(1), AgentSessionState.RUNNING)
    assert agent_sessions(db, WATCHDOG) == [
        (1, AgentSessionState.RUNNING, None),
        (2, AgentSessionState.REQUESTED, None),
    ]
    release.touch()
    sched.wait(SETTLE)
    assert run_all(sched) == [2]
    sched.wait(SETTLE)
    sched.stop(timeout=5)
    with db.session() as s:
        second = s.get_one(AgentSession, 2)
    assert (second.state, second.harness, second.model) == (AgentSessionState.EXITED, 'script', 'watchdog-model')
    assert (
        'Why this agent started (context, not a scope): requested'
        in (tmp_path / 'agent_sessions' / '2' / 'brief.md').read_text()
    )


def test_an_implementer_request_without_a_run_is_refused(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    with db.session() as s:
        s.add(AgentSession(kind=IMPLEMENTER, campaign_id=campaign.id, reason='by hand'))
        s.commit()
    sched = scheduler(db, runtime, tmp_path)
    assert run_all(sched) == []
    with db.session() as s:
        w = s.get_one(AgentSession, 1)
    assert (w.state, w.error) == (AgentSessionState.KILLED, NO_RUN_ERROR) and w.ended_at is not None
    sched.stop(timeout=1)


def test_requests_go_through_the_agent_sql_path(db: Database, campaign: Campaign, tmp_path: Path) -> None:
    """The SQL statement that the roles docs give agents passes the authorizer and inserts a request."""
    with closing(sql.connect(tmp_path / 'piloop.db', actor=System.HUMAN)) as con:
        result = sql.run(
            con,
            f"INSERT INTO agent_sessions (kind, campaign_id, state) VALUES ('watchdog', {campaign.id}, 'requested')",
        )
        assert result.last_insert_rowid == 1
        row = sql.run(con, 'SELECT kind, state, harness, reason FROM agent_sessions').rows
    assert row == [('watchdog', 'requested', None, None)]
    with (
        closing(sql.connect(tmp_path / 'piloop.db', actor=System.HUMAN)) as con,
        pytest.raises(sqlite3.IntegrityError),
    ):
        sql.run(con, "INSERT INTO agent_sessions (kind, campaign_id, state) VALUES ('janitor', 1, 'requested')")
