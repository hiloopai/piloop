import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import traceback
from collections import Counter
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass, field
from datetime import timedelta
from importlib.metadata import EntryPoints
from pathlib import Path
from unittest.mock import call

import pytest
from dirty_equals import IsStr
from git import Repo
from inline_snapshot import snapshot
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from pytest_mock import MockerFixture
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from piloop import proc
from piloop.actor import Agent, System
from piloop.agents.results import (
    AgentEnded,
    AgentSessionFinished,
    CampaignResults,
    PromptOutcome,
    StartupAnomaly,
    WrapUp,
    extend,
)
from piloop.agents.roles import PILOOP_HINT, compose_brief
from piloop.campaign import campaigns, ops
from piloop.campaign.campaigns import sync
from piloop.campaign.stop import left, request_cancel, request_stop
from piloop.config import CampaignConfig, load_campaign_config
from piloop.daemon.loop import Turn
from piloop.daemon.scheduler import Scheduler
from piloop.db import sql
from piloop.db.events import has, in_order, payload_of
from piloop.db.models import (
    OPTIMIZATION,
    AgentSession,
    AgentSessionState,
    Campaign,
    Comment,
    Event,
    EventModel,
    Lease,
    LeaseState,
    Pause,
    utcnow,
)
from piloop.db.requests import answer, pending
from piloop.db.session import Database
from piloop.db.triggers import DAEMON_ACTOR
from piloop.ids import AgentSessionId, CampaignStatus, CommentId, Ending, RoleName, TargetKind, TargetRef
from piloop.otel import Tracer
from piloop.plugins import host as plugins
from piloop.plugins.campaign_type.contract import CAMPAIGNS, Take
from piloop.plugins.campaign_type.kit.backlog import ops as backlog_ops
from piloop.plugins.campaign_type.kit.backlog.ideas import claimable
from piloop.plugins.campaign_type.kit.backlog.ids import IDEA, IdeaId, IdeaStatus, LinkKind
from piloop.plugins.campaign_type.kit.backlog.role.research import ResearchRole
from piloop.plugins.campaign_type.kit.backlog.tables import Idea, IdeaLink
from piloop.plugins.campaign_type.kit.candidates.ids import CandidateId, Origin, Outcome
from piloop.plugins.campaign_type.kit.candidates.references import import_reference
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.evaluation.jobs import EVALUATION
from piloop.plugins.campaign_type.kit.evaluation.measure import RemeasureRequested, current_evaluator, request_remeasure
from piloop.plugins.campaign_type.kit.evaluation.tables import Evaluation, Measurement
from piloop.plugins.campaign_type.kit.runs.controller import RunController, close_run, wrap_up_lead
from piloop.plugins.campaign_type.kit.runs.ids import IMPLEMENTER, RUN, RunEnd, RunEnding, RunId, RunStatus
from piloop.plugins.campaign_type.kit.runs.ops import RunEndRequested, end_run
from piloop.plugins.campaign_type.kit.runs.stop import release, runs
from piloop.plugins.campaign_type.kit.runs.tables import Run, latest_agent_session, run_of
from piloop.plugins.campaign_type.kit.runs.trigger.run_failed import RunFailed
from piloop.plugins.campaign_type.kit.runs.trigger.stalled import StalledTrigger
from piloop.plugins.campaign_type.kit.verdicts.judge import due, head
from piloop.plugins.campaign_type.optimization.planner import OptimizationPlanner, RunQueued
from piloop.plugins.executor.contract import AGENT, AgentKey, Capacity, Prompt
from piloop.plugins.executor.kit.capacity import Pool
from piloop.plugins.executor.kit.leases import SERVE
from piloop.plugins.executor.workers import Workers
from piloop.plugins.fact_source.facts import Facts
from piloop.plugins.host import Plugins
from piloop.plugins.launch_policy.fewest_live import FewestLive
from piloop.plugins.role.consumers import Agents
from piloop.plugins.role.contract import ImplementerRole
from piloop.plugins.runtime.contract import Handle
from piloop.plugins.trigger.dead_at_startup import DeadAtStartup
from piloop.plugins.trigger.sql import SqlTrigger
from piloop.plugins.trigger.stopped import StoppedTrigger
from piloop.plugins.vcs.git import PILOOP, UNCOMMITTED, GitRepository, commands as git
from piloop.settings import AgentSettings, DaemonSettings
from tests.conftest import (
    END_RUN,
    EVALUATE,
    HARNESS_APPEND,
    HARNESS_SLEEPS,
    HARNESS_TWO_COMMITS,
    RESEARCH,
    SETTLE,
    WATCHDOG,
    Gate,
    ScriptRuntime,
    active,
    campaigns_of,
    details,
    disk,
    escalation_sql,
    events,
    factory,
    home_cas,
    pinned,
    reconfigured,
    register,
    reports,
    role_of,
    run_all,
    serving,
    span_named,
    store_config,
    wait_for_agent_session_handle,
    wait_until,
    workers_of,
    write_toml,
)
from tests.test_actor import ACTORS
from tests.usage.test_usage import PI_ID, store


def make_scheduler(
    db: Database,
    runtime: ScriptRuntime,
    tmp_path: Path,
    tracer: Tracer | None = None,
    plugins: Plugins | None = None,
    *,
    claims: bool = True,
) -> Scheduler:
    """A scheduler whose workers each claim the most urgent claimable idea as their run is queued (``claims``).

    The fake harnesses claim nothing themselves, so ``claims`` stands in for their ``piloop idea claim``.
    """
    tracer = tracer or Tracer(None)
    controller = serving(RunController(db, tracer, tmp_path, factory(runtime), executor=Workers()))
    scheduler = Scheduler(db, controller, tracer, tmp_path, factory(runtime), plugins=plugins)
    if claims:
        planner = campaigns_of(scheduler)._types[OPTIMIZATION]
        object.__setattr__(planner, 'queue', ClaimingQueue(planner.queue))
    return scheduler


@dataclass(frozen=True)
class ClaimingQueue:
    """A planner's ``queue``, then each queued run's worker claims the most urgent claimable idea, as it would."""

    queue: Callable[[Session, Campaign, Facts], None]

    def __call__(self, s: Session, campaign: Campaign, left: Facts) -> None:
        self.queue(s, campaign, left)
        claim_for_queued(s, campaign)


def claim_for_queued(s: Session, campaign: Campaign) -> list[tuple[RunId, int]]:
    """Each queued run of ``campaign`` that claimed nothing claims the most urgent claimable idea, as its worker.

    Returns the runs and the ideas that they claimed, oldest run first.
    """
    claimed: list[tuple[RunId, int]] = []
    unclaimed = select(Run).where(Run.campaign_id == campaign.id, Run.status == RunStatus.QUEUED, Run.idea_id.is_(None))
    for run in s.scalars(unclaimed.order_by(Run.id)).all():
        board = select(Idea).where(Idea.campaign_id == campaign.id, claimable()).order_by(Idea.priority, Idea.id)
        idea, worker = s.scalars(board.limit(1)).first(), latest_agent_session(s, run.id)
        if idea is None or worker is None:
            break
        backlog_ops.claim(s, idea.id, actor=Agent(role=worker.kind, session=worker.id))
        claimed.append((run.id, idea.id))
    return claimed


def request_watchdog(db: Database, campaign: Campaign) -> AgentSessionId:
    with db.session() as s:
        agent_session = AgentSession(kind=WATCHDOG, campaign_id=campaign.id, reason='by hand')
        s.add(agent_session)
        s.commit()
        return agent_session.id


def add_ideas(
    db: Database,
    campaign: Campaign,
    *titles: str,
    priority: int = 2,
    status: IdeaStatus = IdeaStatus.OPEN,
    after: tuple[int, ...] = (),
    track: str = 'score',
) -> list[int]:
    with db.session() as s:
        ideas = [
            Idea(
                track=track,
                campaign_id=campaign.id,
                title=t,
                hypothesis=t,
                priority=priority,
                status=status,
                created_by='test',
                links=[IdeaLink(target_id=a, kind=LinkKind.AFTER) for a in after],
            )
            for t in titles
        ]
        s.add_all(ideas)
        s.commit()
        return [i.id for i in ideas]


def occupy_the_run_slot(db: Database, campaign: Campaign) -> RunId:
    """A running run of no agent that this daemon knows: it holds the one implementer slot, so no fresh run starts."""
    with db.session() as s:
        run = Run(
            campaign_id=campaign.id,
            base_candidate_id=CandidateId(1),
            harness='script',
            model='m',
            status=RunStatus.RUNNING,
        )
        s.add(run)
        s.commit()
        return run.id


def statuses(db: Database) -> list[IdeaStatus]:
    with db.session() as s:
        return [i.status for i in s.scalars(select(Idea).order_by(Idea.id))]


def timings(db: Database, campaign: Campaign, **seconds: float) -> None:
    """Set the campaign's ``[agents]`` timings, in seconds, over its stored config."""
    with db.session() as s:
        stored = s.get_one(Campaign, campaign.id)
        stored.config = reconfigured(stored.config, agents=seconds)
        s.commit()


def set_config(db: Database, campaign: Campaign, **overrides: object) -> None:
    with db.session() as s:
        s.get_one(Campaign, campaign.id).config = load_campaign_config(Path(campaign.path), **overrides)
        s.commit()


def test_agents_launch_across_campaigns_within_the_executors_agent_slots_fewest_live_first(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path, mocker: MockerFixture
) -> None:
    """Two campaigns of four implementers each, and three agent slots: two go to one campaign, one to the other.

    The slots are free slots: core subtracts only what it took and has not submitted yet, never what the executor runs.
    """
    other_path = tmp_path / 'other' / 'campaign'
    shutil.copytree(campaign.path, other_path)
    for path in (Path(campaign.path), other_path):
        write_toml(path, HARNESS_SLEEPS, max_implementers=4)
    toml = other_path / 'piloop.toml'
    toml.write_text(toml.read_text().replace('name = "demo"', 'name = "other"'))
    Repo(other_path).git.commit('-qam', 'other')
    other = register(db, other_path)
    set_config(db, campaign)
    scheduler = make_scheduler(db, runtime, tmp_path)
    workers_of(scheduler._controller).leases.pools = {SERVE: Pool(agents=3)}
    add_ideas(db, campaign, 'a1', 'a2', 'a3')
    add_ideas(db, other, 'b1', 'b2', 'b3')

    launched = run_all(scheduler)
    with db.session() as s:
        by = Counter(s.get_one(AgentSession, i).campaign_id for i in launched)
    assert sorted(by.values()) == [1, 2], by
    assert run_all(scheduler) == [], 'every slot is taken, by agents not submitted yet or by their leases'
    wait_until(lambda: not scheduler._controller.launcher.unsubmitted)
    assert run_all(scheduler) == [], 'the executor counts them once submitted'
    mocker.patch.object(
        scheduler._controller.executor, 'capacity', return_value=Capacity(agents=1, slots={EVALUATION: None})
    )
    assert len(run_all(scheduler)) == 1, 'one free slot: core subtracts nothing that the executor counts'
    scheduler.stop(timeout=5)


def test_a_job_submitted_while_the_scheduler_counts_its_slots_still_takes_one(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path, mocker: MockerFixture
) -> None:
    """The job leaves core's count as the executor counts it: between the two reads, it must not be in neither."""
    scheduler = make_scheduler(db, runtime, tmp_path)
    unsubmitted = scheduler._controller.launcher.unsubmitted
    unsubmitted.add(AgentSessionId(99))  # an agent that a launch thread is about to submit
    add_ideas(db, campaign, 'a1')

    def submitted_meanwhile() -> Capacity:
        if any(frame.name == '_launch' for frame in traceback.extract_stack()):  # the agents' count, not evaluations'
            unsubmitted.discard(AgentSessionId(99))  # its submit lands after the executor's read: no lease of it there
        return Capacity(agents=1, slots={EVALUATION: None})

    mocker.patch.object(scheduler._controller.executor, 'capacity', side_effect=submitted_meanwhile)
    assert run_all(scheduler) == [], 'its one slot is taken'
    scheduler.stop(timeout=5)


@pytest.mark.usefixtures('submitting')
def test_the_workers_of_free_slots_take_the_open_ideas_most_urgent_first(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    scheduler = make_scheduler(db, runtime, tmp_path)
    (low,) = add_ideas(db, campaign, 'low', priority=3)
    (high,) = add_ideas(db, campaign, 'high', priority=0)

    assert run_all(scheduler) == [1]
    with db.session() as s:
        run = s.get_one(Run, 1)
        assert (run.idea_id, run.attempt, run.harness, run.model) == (high, 1, 'script', 'test-model')
        assert s.get_one(Idea, high).status is IdeaStatus.CLAIMED
        agent_session = s.get_one(AgentSession, 1)
        assert (
            agent_session.kind,
            run_of(agent_session),
            agent_session.harness,
            agent_session.model,
            agent_session.reason,
        ) == (
            IMPLEMENTER,
            1,
            'script',
            'test-model',
            'a free slot',
        )
    assert run_all(scheduler) == [], 'max_concurrent=1: the second idea waits'
    scheduler.wait(SETTLE)

    assert run_all(scheduler) == [2]
    scheduler.wait(SETTLE)
    scheduler.stop(timeout=1)
    with db.session() as s:
        assert [(i.id, i.status) for i in s.scalars(select(Idea).order_by(Idea.id))] == [
            (low, IdeaStatus.DONE),
            (high, IdeaStatus.DONE),
        ]
        assert [m.metrics['score'] for m in s.scalars(select(Measurement).order_by(Measurement.id))] == [1.0, 2.0, 3.0]
        second = s.get_one(Run, 2)
        assert second.base_candidate_id == 2, 'the second run builds on the first kept result'
    assert active(scheduler) == []


def queued_runs(db: Database, scheduler: Scheduler, campaign: Campaign) -> list[Run]:
    """Run one ``queue`` of the campaign's planner, and return every run, oldest first."""
    with db.session() as s:
        p = s.get_one(Campaign, campaign.id)
        campaigns_of(scheduler)._planner(p).queue(s, p, left(s, p, p.config))
        s.commit()
    with db.session() as s:
        found = s.scalars(select(Run).order_by(Run.id)).all()
        s.expunge_all()
        return list(found)


def test_free_slots_start_fresh_runs_from_the_head_on_the_rotation_within_the_limits_and_leave_the_board(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    """The scheduler assigns no idea: each free slot gets a run with none, from the head, whose worker claims one."""
    set_config(
        db,
        campaign,
        plugins={
            'harness': {'use': ['script', 'pi'], 'script': {'model': 'm'}, 'pi': {'model': 'p'}},
            'role': {'implementer': {'max_concurrent': 3}},
            'stop_rule': {'max_runs': 2},
        },
    )
    ideas = add_ideas(db, campaign, 'a', 'b', 'c')
    scheduler = make_scheduler(db, runtime, tmp_path, claims=False)
    runs = queued_runs(db, scheduler, campaign)
    assert [(r.idea_id, r.attempt, r.base_candidate_id, r.branch, r.harness, r.model) for r in runs] == [
        (None, None, 1, False, 'script', 'm'),
        (None, None, 1, False, 'pi', 'p'),
    ], 'three free slots, but [stop] max_runs leaves two runs'
    with db.session() as s:
        queued = s.scalars(select(Event).where(has(RunQueued)).order_by(Event.id)).all()
        assert [(e.target_id, payload_of(e, RunQueued)) for e in queued] == [
            (runs[0].id, RunQueued(harness='script')),
            (runs[1].id, RunQueued(harness='pi')),
        ]
        workers = s.scalars(select(AgentSession).order_by(AgentSession.id)).all()
        assert [(run_of(w), w.state, w.harness, w.reason) for w in workers] == [
            (runs[0].id, AgentSessionState.REQUESTED, 'script', 'a free slot'),
            (runs[1].id, AgentSessionState.REQUESTED, 'pi', 'a free slot'),
        ]
        assert [s.get_one(Idea, i).status for i in ideas] == [IdeaStatus.OPEN] * 3, 'the board is untouched'
    assert len(queued_runs(db, scheduler, campaign)) == 2, 'the stop rule leaves no more runs: nothing more'


def test_no_run_starts_without_a_head_or_while_the_head_waits_for_a_measurement_by_a_new_evaluator(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    add_ideas(db, campaign, 'an idea')
    scheduler = make_scheduler(db, runtime, tmp_path, claims=False)
    with db.session() as s:
        s.get_one(Candidate, 1).verdict = Outcome.DISCARD
        s.commit()
    assert queued_runs(db, scheduler, campaign) == [], 'no keep: no head to start from'
    with db.session() as s:
        s.get_one(Candidate, 1).verdict = Outcome.KEEP
        p = s.get_one(Campaign, campaign.id)
        cfg = load_campaign_config(Path(campaign.path), evaluate=['sh', '-c', reports(score='5', size='1')])
        p.config = cfg
        evaluator = current_evaluator(s, p, cfg)
        s.commit()
    assert queued_runs(db, scheduler, campaign) == [], 'the head has no score from the new evaluator yet'
    with db.session() as s:
        s.add(Measurement(candidate_id=1, evaluator_id=evaluator.id, metrics={'score': 5, 'size': 1}))
        s.commit()
    [run] = queued_runs(db, scheduler, campaign)
    assert (run.idea_id, run.base_candidate_id) == (None, 1)


@pytest.mark.usefixtures('submitting')
def test_a_run_that_claimed_nothing_ends_and_settles_no_idea(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    """Its worker saved a version without a claim: the version is judged, and no idea changes."""
    (idea,) = add_ideas(db, campaign, 'never claimed')
    scheduler = make_scheduler(db, runtime, tmp_path, claims=False)
    assert run_all(scheduler) == [1]
    scheduler.wait(SETTLE)
    scheduler.stop(timeout=5)
    with db.session() as s:
        run = s.get_one(Run, 1)
        assert (run.idea_id, run.end, run.scoring) == (None, RunEnd.FINISHED, False)
        assert [c.verdict for c in s.scalars(select(Candidate).where(Candidate.run_id == 1))] == [Outcome.KEEP]
        assert s.get_one(Idea, idea).status is IdeaStatus.OPEN
        assert s.scalars(select(Comment)).all() == [], 'nothing to settle, so nothing to say'


@pytest.mark.parametrize(
    ('end', 'error', 'judged', 'after', 'note'),
    [
        (RunEnd.FINISHED, None, True, IdeaStatus.DONE, None),
        (
            RunEnd.FINISHED,
            None,
            False,
            IdeaStatus.OPEN,
            'open: run#{run} gave the idea back; it finished, but nothing that it saved got a verdict',
        ),
        (RunEnd.FAILED, 'exit 1', True, IdeaStatus.OPEN, 'open: run#{run} gave the idea back; it ended failed: exit 1'),
        (RunEnd.TIMED_OUT, None, False, IdeaStatus.OPEN, 'open: run#{run} gave the idea back; it ended timed_out'),
        (
            RunEnd.GAVE_UP,
            'the window does not fit in memory',
            True,
            IdeaStatus.OPEN,
            'open: run#{run} gave the idea back; it ended gave_up: the window does not fit in memory',
        ),
        (
            RunEnd.DAEMON_STOPPED,
            None,
            False,
            IdeaStatus.OPEN,
            'open: run#{run} gave the idea back; it ended daemon_stopped',
        ),
        (
            RunEnd.CANCELLED,
            None,
            False,
            IdeaStatus.OPEN,
            'open: run#{run} was cancelled; drop the idea to take it off the board',
        ),
    ],
    ids=['finished', 'finished-unjudged', 'failed', 'timed-out', 'gave-up', 'stopped', 'cancelled'],
)
def test_a_claim_ends_with_its_run(
    db: Database,
    campaign: Campaign,
    end: RunEnd,
    error: str | None,
    judged: bool,
    after: IdeaStatus,
    note: str | None,
) -> None:
    """A finished run with a real verdict makes its idea done. Any other end reopens it for another worker, and a note
    on the idea says why."""
    (idea,) = add_ideas(db, campaign, 'claimed')
    with db.session() as s:
        run = Run(campaign_id=campaign.id, base_candidate_id=CandidateId(1), harness='script', model='m')
        s.add(run)
        s.flush()
        worker = AgentSession(kind=IMPLEMENTER, campaign_id=campaign.id, subject=TargetRef(RUN, run.id))
        s.add(worker)
        s.flush()
        backlog_ops.claim(s, IdeaId(idea), actor=Agent(role=IMPLEMENTER, session=worker.id))
        assert s.get_one(Idea, idea).status is IdeaStatus.CLAIMED
        last = Candidate(
            campaign_id=campaign.id,
            run_id=run.id,
            tree_sha='t',
            revision='r',
            verdict=Outcome.DISCARD if judged else None,
        )
        s.add(last)
        s.flush()
        release(s, close_run(s, run, end, error), end, last)
        s.commit()
        assert s.get_one(Idea, idea).status is after
        notes = [c.body for c in s.scalars(select(Comment).where(Comment.target_id == idea))]
        assert notes == ([] if note is None else [note.format(run=run.id)])
        release(s, run, end, last)
        assert s.get_one(Idea, idea).status is after, 'an idea that the run no longer holds stays as it is'


def test_a_sweep_checks_out_the_head(db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path) -> None:
    scheduler = make_scheduler(db, runtime, tmp_path)
    add_ideas(db, campaign, 'more')
    assert run_all(scheduler) == [1]
    scheduler.wait(SETTLE)
    with db.session() as s:
        best = s.get_one(Candidate, 2)
        assert best.verdict is Outcome.KEEP and best.revision != s.get_one(Candidate, 1).revision
        best_sha = best.revision
    write_toml(Path(campaign.path), '#!/bin/sh\ngit rev-parse HEAD > workspace/head.txt\n')
    set_config(db, campaign)
    agent_session_id = request_watchdog(db, campaign)
    assert agent_session_id in run_all(scheduler), 'beside a fresh run in the free implementer slot'
    scheduler.wait(SETTLE)
    head = tmp_path / 'projects' / str(campaign.id) / 'workspace' / 'head.txt'
    assert head.read_text().strip() == best_sha
    assert (
        'detached checkout of candidate#2, the head (score=2.0)'
        in (tmp_path / 'agent_sessions' / str(agent_session_id) / 'brief.md').read_text()
    )
    # the executor removes it once the sweep's result is in, after what waits for the sweep returned
    wait_until(lambda: git.worktrees_under(Repo(campaign.path), tmp_path / 'agent_sessions') == [])
    hold = tmp_path / 'hold'
    hold.touch()  # The tick measures the head again under the new evaluator: held until the sweep has its brief.
    evaluate = f'while [ -f {hold} ]; do sleep 0.05; done; {reports(score="9", size="9")}'
    with db.session() as s:
        cfg = load_campaign_config(Path(campaign.path), evaluate=['sh', '-c', evaluate])
        s.get_one(Campaign, campaign.id).config = cfg
        current_evaluator(s, s.get_one(Campaign, campaign.id), cfg)
        s.commit()
    again = request_watchdog(db, campaign)
    assert again in run_all(scheduler)
    brief = tmp_path / 'agent_sessions' / str(again) / 'brief.md'
    wait_until(brief.exists)
    hold.unlink()
    scheduler.wait(SETTLE)
    scheduler.stop(timeout=1)
    assert head.read_text().strip() == best_sha, (
        'a new evaluator: nothing is measured under it yet, and the head stays the latest trunk keep'
    )
    assert 'detached checkout of candidate#2, the head (score=unmeasured)' in brief.read_text()
    with db.session() as s:
        measured = s.scalars(select(Measurement).where(Measurement.candidate_id == 2).order_by(Measurement.id))
        assert [m.metrics['score'] for m in measured] == [2.0, 9.0], 'then the head is measured under the new evaluator'


def test_runs_start_on_the_harnesses_of_the_rotation_in_turn(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    write_toml(Path(campaign.path), '#!/bin/sh\ntrue\n')
    cfg = load_campaign_config(
        Path(campaign.path),
        plugins={
            'harness': {'use': ['script', 'pi'], 'script': {'model': 'm'}, 'pi': {'model': 'p'}},
            'role': {'implementer': {'max_concurrent': 3}},
        },
    )
    with db.session() as s:
        s.get_one(Campaign, campaign.id).config = cfg
        s.commit()
    add_ideas(db, campaign, 'a', 'b', 'c')
    scheduler = make_scheduler(db, runtime, tmp_path)
    assert run_all(scheduler) == [1, 2, 3]
    scheduler.wait(SETTLE)
    scheduler.stop(timeout=1)
    with db.session() as s:
        assert [(r.harness, r.model) for r in s.scalars(select(Run).order_by(Run.id))] == snapshot(
            [('script', 'm'), ('pi', 'p'), ('script', 'm')]
        )


def test_a_harness_at_its_cap_passes_its_turn(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    """A harness at its ``max_concurrent`` cap passes its turn.

    ``max_concurrent = {claude = N}`` limits a campaign to N Claude agents while other harnesses fill the free slots.
    """
    set_config(
        db,
        campaign,
        plugins={
            'harness': {
                'use': ['script', 'pi'],
                'script': {'model': 'm', 'max_concurrent': 1},
                'pi': {'model': 'p', 'max_concurrent': 2},
            },
            'role': {'implementer': {'max_concurrent': 5}},
        },
    )
    a, b, c, d = add_ideas(db, campaign, 'a', 'b', 'c', 'd')
    with db.session() as s:
        s.add(
            AgentSession(kind=IMPLEMENTER, campaign_id=campaign.id, harness='script', state=AgentSessionState.RUNNING)
        )
        s.commit()
    scheduler = make_scheduler(db, runtime, tmp_path)
    with db.session() as s:
        p = s.get_one(Campaign, campaign.id)
        campaigns_of(scheduler)._planner(p).queue(s, p, left(s, p, p.config))
        s.commit()
    with db.session() as s:
        assert [(r.idea_id, r.harness) for r in s.scalars(select(Run).order_by(Run.id))] == [(a, 'pi'), (b, 'pi')], (
            "script's turn went to pi; once pi was full too nothing more was queued"
        )
        assert [s.get_one(Idea, i).status for i in (c, d)] == [IdeaStatus.OPEN] * 2


def test_a_requested_worker_whose_harness_reached_its_cap_since_its_run_was_queued_waits(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    set_config(db, campaign, plugins={'harness': {'use': ['script'], 'script': {'model': 'm', 'max_concurrent': 1}}})
    scheduler = make_scheduler(db, runtime, tmp_path, claims=False)
    (run,) = queued_runs(db, scheduler, campaign)
    with db.session() as s:
        s.add(AgentSession(kind=WATCHDOG, campaign_id=campaign.id, harness='script', state=AgentSessionState.RUNNING))
        s.commit()
    assert run_all(scheduler) == [], 'the watchdog took the last script slot first'
    with db.session() as s:
        assert s.get_one(Run, run.id).status is RunStatus.QUEUED
        [worker] = s.scalars(select(AgentSession).where(AgentSession.subject_id == run.id)).all()
        assert worker.state is AgentSessionState.REQUESTED
        assert campaigns_of(scheduler)._planner(s.get_one(Campaign, campaign.id)).take(s, worker, set()) is Take.LAUNCH


def test_a_sweep_passes_over_a_harness_at_its_cap_and_a_pinned_one_stays_requested(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    set_config(
        db,
        campaign,
        plugins={
            'harness': {'use': ['script', 'pi'], 'script': {'model': 'm', 'max_concurrent': 1}, 'pi': {'model': 'p'}},
            'role': {'watchdog': {'harnesses': ['script', 'pi'], 'max_concurrent': 4}},
        },
    )
    with db.session() as s:
        rows = [AgentSession(kind=WATCHDOG, campaign_id=campaign.id, reason='by hand') for _ in range(3)]
        rows.append(AgentSession(kind=WATCHDOG, campaign_id=campaign.id, harness='script', reason='pinned'))
        s.add_all(rows)
        s.commit()
    scheduler = make_scheduler(db, runtime, tmp_path)
    with db.session() as s:
        p = s.get_one(Campaign, campaign.id)
        taken = campaigns_of(scheduler)._take(s, p, CampaignConfig.model_validate(p.config), utcnow())
        s.commit()
    assert [w for w, _ in taken] == [1, 2, 3]
    with db.session() as s:
        assert [(w.harness, w.state) for w in s.scalars(select(AgentSession).order_by(AgentSession.id))] == [
            ('script', AgentSessionState.STARTING),
            ('pi', AgentSessionState.STARTING),
            ('pi', AgentSessionState.STARTING),
            ('script', AgentSessionState.REQUESTED),
        ], "the third sweep's turn was script's, at its cap"


def test_a_run_starts_from_the_best_as_of_its_start_not_as_of_the_idea(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    """A run starts from the head at the time the run starts, not at the time its idea was written."""
    (idea,) = add_ideas(db, campaign, 'written while the baseline was best')
    with db.session() as s:
        s.add(Candidate(campaign_id=campaign.id, tree_sha='b', revision='b', verdict=Outcome.KEEP, verdict_by='human'))
        s.flush()
        s.add(Measurement(candidate_id=2, evaluator_id=1, metrics={'score': 9, 'size': 9}))
        s.commit()
    scheduler = make_scheduler(db, runtime, tmp_path)
    with db.session() as s:
        p = s.get_one(Campaign, campaign.id)
        campaigns_of(scheduler)._planner(p).queue(s, p, left(s, p, p.config))
        s.commit()
    with db.session() as s:
        assert (s.get_one(Run, 1).idea_id, s.get_one(Run, 1).base_candidate_id) == (idea, 2)


@pytest.mark.usefixtures('submitting')
def test_a_held_idea_waits_for_every_prerequisite_then_starts_from_the_head(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    (first,) = add_ideas(db, campaign, 'first', priority=3)
    (second,) = add_ideas(db, campaign, 'second', priority=4)
    (third,) = add_ideas(db, campaign, 'third', priority=0, after=(first, second))
    scheduler = make_scheduler(db, runtime, tmp_path)

    assert run_all(scheduler) == [1]
    with db.session() as s:
        assert s.get_one(Run, 1).idea_id == first, 'the urgent idea is held, so the lower priorities run first'
        assert s.get_one(Idea, third).status is IdeaStatus.OPEN, 'skipped, not touched'
    scheduler.wait(SETTLE)
    assert run_all(scheduler) == [2]
    with db.session() as s:
        assert s.get_one(Run, 2).idea_id == second, 'one prerequisite done is not enough'
        assert s.get_one(Idea, third).status is IdeaStatus.OPEN
    scheduler.wait(SETTLE)
    with db.session() as s:
        assert [s.get_one(Candidate, c).verdict for c in (2, 3)] == [Outcome.KEEP] * 2, 'scores 2 and 3'
        # a human keeps a commit outside any run: the head is now candidate#4, which no prerequisite made
        sha = s.get_one(Candidate, 3).revision
        s.add(Candidate(campaign_id=campaign.id, tree_sha='h', revision=sha, verdict=Outcome.KEEP, verdict_by='human'))
        s.flush()
        s.add(Measurement(candidate_id=4, evaluator_id=1, metrics={'score': 9, 'size': 9}))
        s.commit()

    assert run_all(scheduler) == [3]
    scheduler.wait(SETTLE)
    scheduler.stop(timeout=1)
    with db.session() as s:
        run = s.get_one(Run, 3)
        assert (run.idea_id, run.base_candidate_id) == (third, 4), 'the head, which holds what the prerequisites kept'
        assert s.get_one(Idea, third).status is IdeaStatus.DONE


def test_an_after_link_holds_an_idea_until_its_prerequisite_is_done_or_dropped_and_parks_nothing(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    """A prerequisite that is done, kept or not, or dropped frees its dependents. One that is claimed or open holds
    them, through a chain too. Nothing changes their status, and nothing asks the lead."""
    (dropped, nothing, claimed) = add_ideas(db, campaign, 'dropped', 'kept nothing', 'claimed')
    with db.session() as s:
        for idea_id, status in (
            (dropped, IdeaStatus.DROPPED),
            (nothing, IdeaStatus.DONE),
            (claimed, IdeaStatus.CLAIMED),
        ):
            s.get_one(Idea, idea_id).status = status
        s.commit()
    stop_by_hand(tmp_path, campaign)  # bookkeeping happens, nothing launches
    (after_dropped, after_claimed) = add_ideas(db, campaign, 'after dropped', 'after claimed')
    (after_nothing,) = add_ideas(db, campaign, 'after kept nothing', after=(nothing,))
    (chained,) = add_ideas(db, campaign, 'after after dropped', after=(after_dropped,))
    with db.session() as s:
        s.add(IdeaLink(idea_id=after_dropped, target_id=dropped, kind=LinkKind.AFTER))
        s.add(IdeaLink(idea_id=after_claimed, target_id=claimed, kind=LinkKind.AFTER))
        s.commit()
    scheduler = make_scheduler(db, runtime, tmp_path)

    assert run_all(scheduler) == []
    with db.session() as s:
        dependents = (after_dropped, after_claimed, after_nothing, chained)
        assert [s.get_one(Idea, i).status for i in dependents] == [IdeaStatus.OPEN] * 4, 'nothing is parked'
        assert set(s.scalars(select(Idea.id).where(claimable()))) == {after_dropped, after_nothing}, (
            'a dropped or a done prerequisite frees its dependents; a claimed one, or an open one up the chain, holds'
        )
        assert s.scalars(select(Comment)).all() == [], 'and nothing is escalated'


def test_low_water_counts_held_ideas_as_backlog(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    """A held idea is on the board: research adds no idea for its slot."""
    set_config(db, campaign, plugins={'role': {'research': {'max_ideas': 1, 'max_concurrent': 1, 'timeout': 20}}})
    (gate,) = add_ideas(db, campaign, 'gate', status=IdeaStatus.CLAIMED)
    add_ideas(db, campaign, *(f'behind the gate {n}' for n in range(3)), after=(gate,))
    scheduler = make_scheduler(db, runtime, tmp_path)
    assert run_all(scheduler) == [1], 'the free slot: a fresh run, which finds no idea to claim'
    scheduler.stop(timeout=1)
    with db.session() as s:
        assert [w.kind for w in s.scalars(select(AgentSession))] == [IMPLEMENTER], 'no research sweep'
        assert s.get_one(Run, 1).idea_id is None


def test_reap(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store(tmp_path, 'pi', PI_ID, cwd=tmp_path / 'agent_sessions' / '2' / 'worktree')
    (idea,) = add_ideas(db, campaign, 'stale')
    repo = Repo(campaign.path)
    with db.session() as s:
        s.add(
            Run(
                campaign_id=campaign.id,
                idea_id=idea,
                attempt=1,
                base_candidate_id=1,
                harness='script',
                model='m',
                status=RunStatus.RUNNING,
            )
        )
        s.flush()  # the run first: the unit of work orders unrelated mappers by name
        s.add(
            AgentSession(
                kind='implementer',
                campaign_id=campaign.id,
                subject=TargetRef(RUN, 1),
                harness='script',
                model='m',
                runtime='subprocess',
                handle={},
                state=AgentSessionState.STARTING,  # mid-start: nothing to re-attach to
                log_dir='x',
                session_id='no-spec-to-read-it-with',
            )
        )
        s.get_one(Idea, idea).status = IdeaStatus.CLAIMED
        evaluator = s.get_one(Campaign, campaign.id).evaluator_id
        assert evaluator is not None
        s.add(Evaluation(candidate_id=1, evaluator_id=evaluator, started_at=utcnow()))
        s.commit()
        s.add(
            AgentSession(
                kind='research',
                campaign_id=campaign.id,
                harness='pi',
                model='m',
                runtime='subprocess',
                handle={},
                state=AgentSessionState.STARTING,
                log_dir='y',
                session_id=PI_ID,
            )
        )
        s.add(
            AgentSession(
                kind='watchdog',
                campaign_id=campaign.id,
                harness='pi',
                model='m',
                runtime='subprocess',
                handle={},
                state=AgentSessionState.RUNNING,
                log_dir='z',  # running, but its worktree is gone: nothing to re-attach to either
                session_id='a-session-whose-file-is-gone',
            )
        )
        s.commit()
    wt = tmp_path / 'runs' / '1' / 'worktree'
    sweep_wt = tmp_path / 'agent_sessions' / '2' / 'worktree'
    for path in (wt, sweep_wt):
        path.parent.mkdir(parents=True)
        repo.git.worktree('add', '--detach', str(path), 'HEAD')

    make_scheduler(db, runtime, tmp_path).reap()

    with db.session() as s:
        run = s.get_one(Run, 1)
        assert (run.status, run.end) == (RunStatus.FAILED, RunEnd.STALE)
        assert s.get_one(Idea, idea).status is IdeaStatus.OPEN, 'a crash is not an attempt: its only one remains'
        assert s.get_one(AgentSession, 1).state is AgentSessionState.KILLED
        sweep = s.get_one(AgentSession, 2)
        assert sweep.state is AgentSessionState.KILLED
        assert sweep.usage is not None and sweep.usage.total == 16478, (
            'a reaped agent spent tokens too: read from its session file as the row closes, so max_tokens sees them'
        )
        gone = s.get_one(AgentSession, 3)
        assert (gone.state, gone.usage) == (AgentSessionState.KILLED, None), (
            'a session file not found leaves usage NULL'
        )
        assert s.scalars(select(Evaluation)).all() == [], (
            'its evaluator died with the daemon: its measurement is asked again'
        )
    assert not wt.exists() and not sweep_wt.exists(), "the run's and the sweep's worktrees are both removed"
    assert len(runtime.killed) == 3, 'the stale implementer processes were asked to stop'


def test_reap_tolerates_a_runtime_that_cannot_kill(
    db: Database,
    runtime: ScriptRuntime,
    tmp_path: Path,
    mocker: MockerFixture,
    tracer: Tracer,
    spans: InMemorySpanExporter,
) -> None:
    with db.session() as s:
        s.add(
            AgentSession(
                kind='watchdog',
                harness='pi',
                model='m',
                runtime='subprocess',
                handle={},
                state=AgentSessionState.RUNNING,
                log_dir='x',
            )
        )
        s.commit()
    mocker.patch.object(runtime, 'kill', side_effect=RuntimeError('gone'))
    make_scheduler(db, runtime, tmp_path, tracer).reap()
    assert events(spans, 'agent_session.kill_failed') == [{'agent_session': 1, 'error': 'gone'}]
    assert span_named(spans, 'piloop.reap').events[0].name == 'agent_session.kill_failed', 'attached to the reap span'
    with db.session() as s:
        assert s.get_one(AgentSession, 1).state is AgentSessionState.KILLED


def test_reap_survives_a_deleted_campaign(
    db: Database,
    campaign: Campaign,
    runtime: ScriptRuntime,
    tmp_path: Path,
    tracer: Tracer,
    spans: InMemorySpanExporter,
) -> None:
    (idea,) = add_ideas(db, campaign, 'stale')
    with db.session() as s:
        s.get_one(Campaign, campaign.id).path = str(tmp_path / 'gone')
        s.add(
            Run(
                campaign_id=campaign.id,
                idea_id=idea,
                attempt=1,
                base_candidate_id=1,
                harness='script',
                model='m',
                status=RunStatus.RUNNING,
            )
        )
        s.commit()
    make_scheduler(db, runtime, tmp_path, tracer).reap()
    with db.session() as s:
        assert s.get_one(Run, 1).status is RunStatus.FAILED
    assert events(spans, 'worktrees.orphaned') == [
        {'agent_dir': str(tmp_path / 'runs' / '1'), 'campaign_path': str(tmp_path / 'gone')}
    ]


@pytest.mark.usefixtures('no_fresh_runs')  # a free slot's worker would start a process that the stop then kills
def test_serve_stops(db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path) -> None:
    scheduler = make_scheduler(db, runtime, tmp_path)
    t = threading.Thread(target=scheduler.serve)
    t.start()
    scheduler.stop(timeout=5)
    t.join(timeout=5)
    assert not t.is_alive()


class HeldEvent(threading.Event):
    """A stop event whose lock its loop holds, as inside ``wait``: setting it would wait for that lock forever."""

    def set(self) -> None:
        raise AssertionError('the lock of the event is held by the thread that the signal interrupted')


def test_a_signal_during_the_loops_wait_ends_it_at_once(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A SIGTERM handler runs on the main thread, also while the loop holds its stop event's lock inside ``wait``.

    ``Event.set`` in the handler then waited for that lock forever, and ``piloop stop`` gave up after 30 s.
    """
    scheduler = make_scheduler(db, runtime, tmp_path)
    monkeypatch.setattr(scheduler, '_stop', HeldEvent())
    previous = signal.signal(signal.SIGUSR2, lambda *_: scheduler.interrupt())
    started = time.monotonic()
    try:
        threading.Timer(0.5, os.kill, (os.getpid(), signal.SIGUSR2)).start()
        scheduler.serve()
    finally:
        signal.signal(signal.SIGUSR2, previous)
    assert time.monotonic() - started < 10, 'the wait ends at the signal'


def test_the_loop_ends_once_its_home_is_gone(db: Database, runtime: ScriptRuntime, tmp_path: Path) -> None:
    home = tmp_path / 'home'
    home.mkdir()
    controller = RunController(db, Tracer(None), home, factory(runtime), executor=Workers())
    scheduler = Scheduler(db, controller, Tracer(None), home, daemon=DaemonSettings(home_check=timedelta(seconds=0.05)))
    threading.Timer(0.5, shutil.rmtree, (home,)).start()
    try:
        scheduler.serve()
    finally:
        scheduler.stop(timeout=5)
    assert not home.exists(), 'it ran until the home was gone'


def test_a_signal_during_start_up_ends_the_loop_before_its_first_turn(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scheduler = make_scheduler(db, runtime, tmp_path)

    def no_turn(_turn: Turn) -> None:
        raise AssertionError('no turn after the signal')

    monkeypatch.setattr(scheduler.loop, 'run_all', no_turn)
    scheduler.interrupt()
    scheduler.serve()


def test_a_dead_thread_is_traced_and_its_row_closed(
    db: Database,
    campaign: Campaign,
    runtime: ScriptRuntime,
    tmp_path: Path,
    tracer: Tracer,
    spans: InMemorySpanExporter,
    mocker: MockerFixture,
) -> None:
    scheduler = make_scheduler(db, runtime, tmp_path, tracer)
    mocker.patch.object(scheduler._controller, 'execute', side_effect=RuntimeError('thread broke'))
    wake = mocker.spy(scheduler.loop, 'wake')
    add_ideas(db, campaign, 'x')
    run_all(scheduler)
    scheduler.wait(SETTLE)
    assert active(scheduler) == []
    wait_until(lambda: call(CAMPAIGNS) in wake.call_args_list, 'the end of its thread wakes the pass to fill its slot')
    died = span_named(spans, 'agent_session.thread_died')
    assert dict(died.attributes or {}) == {'agent_session': 1, 'error': 'thread broke'}, 'no open span: it gets its own'
    with db.session() as s:
        agent_session = s.get_one(AgentSession, 1)
    assert (agent_session.state, agent_session.error) == (AgentSessionState.KILLED, 'RuntimeError: thread broke')


def test_a_tick_reads_no_entry_points(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The installed packages do not change while a process runs: a tick reads each campaign's config without a scan."""
    scheduler = make_scheduler(db, runtime, tmp_path)
    run_all(scheduler)
    scans: list[str] = []
    found = plugins.entry_points

    def entry_points(*, group: str) -> EntryPoints:
        scans.append(group)
        return found(group=group)

    monkeypatch.setattr(plugins, 'entry_points', entry_points)
    run_all(scheduler)
    run_all(scheduler)
    assert scans == []


def test_invalid_campaign_config_is_skipped_not_fatal(
    db: Database,
    campaign: Campaign,
    runtime: ScriptRuntime,
    tmp_path: Path,
    tracer: Tracer,
    spans: InMemorySpanExporter,
) -> None:
    store_config(db, campaign.id, {})
    assert run_all(make_scheduler(db, runtime, tmp_path, tracer)) == []
    assert dict(span_named(spans, 'campaign.invalid_config').attributes or {})['campaign'] == 'demo'


def test_reap_leaves_a_human_decision_alone(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    (idea,) = add_ideas(db, campaign, 'parked meanwhile')
    with db.session() as s:
        s.add(
            Run(
                campaign_id=campaign.id,
                idea_id=idea,
                attempt=1,
                base_candidate_id=1,
                harness='script',
                model='m',
                status=RunStatus.RUNNING,
            )
        )
        s.get_one(Idea, idea).status = IdeaStatus.DROPPED
        s.commit()
    make_scheduler(db, runtime, tmp_path).reap()
    with db.session() as s:
        assert s.get_one(Idea, idea).status is IdeaStatus.DROPPED


IMPLEMENTER_TIMEOUT = 60
"""The role timeout of the stop tests' implementers, which a stop must not wait out."""


def test_stop_kills_in_flight_implementers_and_fails_their_runs(
    db: Database,
    campaign: Campaign,
    runtime: ScriptRuntime,
    tmp_path: Path,
    tracer: Tracer,
    spans: InMemorySpanExporter,
) -> None:
    write_toml(Path(campaign.path), HARNESS_SLEEPS, implementer_timeout=IMPLEMENTER_TIMEOUT)
    set_config(db, campaign)
    (idea,) = add_ideas(db, campaign, 'long')
    scheduler = make_scheduler(db, runtime, tmp_path, tracer)
    assert run_all(scheduler) == [1]
    handle = wait_for_agent_session_handle(db)
    pid_file = tmp_path / 'runs' / '1' / 'child.pid'
    wait_until(lambda: pid_file.is_file() and pid_file.read_text().strip())  # the agent writes it after it starts
    child_pid = int(pid_file.read_text())

    started = time.monotonic()
    scheduler.stop(timeout=10)
    # the contract: stop() kills what runs, it does not wait for the role timeout; how fast it kills depends on the host
    assert time.monotonic() - started < IMPLEMENTER_TIMEOUT, 'stop() does not wait for implementer_timeout'
    assert proc.started(int(str(handle['pid']))) is None and proc.started(child_pid) is None
    assert active(scheduler) == []
    with db.session() as s:
        run = s.get_one(Run, 1)
        assert (run.status, run.end) == (RunStatus.FAILED, RunEnd.DAEMON_STOPPED)
        assert s.get_one(Idea, idea).status is IdeaStatus.OPEN
        assert (s.get_one(AgentSession, 1).state, s.get_one(AgentSession, 1).error) == (
            AgentSessionState.KILLED,
            RunEnd.DAEMON_STOPPED,
        )
        [(_, finished)] = in_order(s, AgentSessionFinished, Event.target_id == 1)
        assert finished.state is AgentSessionState.KILLED, 'its event says what its row says'
        assert s.scalars(select(Candidate).where(Candidate.run_id == 1)).all() == [], 'no scoring after stop'
    assert not (tmp_path / 'runs' / '1' / 'worktree').exists()
    assert dict(span_named(spans, 'run.aborted').attributes or {}) == {'run': 1, 'end': RunEnd.DAEMON_STOPPED}
    traced = dict(span_named(spans, 'piloop.run').attributes or {})
    assert (traced['status'], traced['end']) == (RunStatus.FAILED, RunEnd.DAEMON_STOPPED), "the row's values"


def test_stop_keeps_the_work_of_the_runs_it_cuts_and_ends_their_claims(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    """A stop pins the commits and the uncommitted work of the runs it ends, as ``recovered`` candidates.

    The claim ends with the run: the idea is open again, and the next run starts from the head, as every run does.
    """
    harness = (
        '#!/bin/sh\necho step >> solve.txt\ngit commit -qam "step"\necho draft >> solve.txt\n'
        'sleep 30 &\necho $! > ../child.pid\nwait\n'
    )
    write_toml(Path(campaign.path), harness, implementer_timeout=60)
    set_config(db, campaign)
    (idea,) = add_ideas(db, campaign, 'long')
    scheduler = make_scheduler(db, runtime, tmp_path)
    assert run_all(scheduler) == [1]
    pid_file = tmp_path / 'runs' / '1' / 'child.pid'
    wait_until(pid_file.exists)
    scheduler.stop(timeout=10)

    repo = Repo(campaign.path)
    step, head = pinned(repo, 'refs/piloop/demo/r1/')
    assert repo.commit(step).summary == 'step'
    recovered = repo.commit(head)
    assert recovered.summary == UNCOMMITTED and recovered.tree['solve.txt'].data_stream.read().endswith(
        b'step\ndraft\n'
    ), 'the uncommitted line is its own recovered commit, as for any run'
    with db.session() as s:
        cut = s.scalars(select(Candidate).where(Candidate.run_id == 1).order_by(Candidate.id)).all()
        assert [(c.revision, c.origin, c.verdict) for c in cut] == [
            (step, Origin.RECOVERED, None),
            (head, Origin.RECOVERED, None),
        ]
        assert all(c.measurements == [] for c in cut), 'never measured'
        assert (s.get_one(Run, 1).end, s.get_one(Idea, idea).status) == (RunEnd.DAEMON_STOPPED, IdeaStatus.OPEN)
        assert [c.body for c in s.scalars(select(Comment).where(Comment.author == DAEMON_ACTOR))] == [
            'open: run#1 gave the idea back; it ended daemon_stopped'
        ]

    assert run_all(make_scheduler(db, runtime, tmp_path)) == [2]
    with db.session() as s:
        assert (s.get_one(Run, 2).idea_id, s.get_one(Run, 2).base_candidate_id) == (idea, 1), 'from the head'


def test_a_cut_commit_of_a_known_tree_is_that_candidate(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    """A cut commit of a known tree, here the base's, is that candidate, and the claim ends with the run."""
    harness = '#!/bin/sh\ngit commit -q --allow-empty -m "same tree"\nsleep 30 &\necho $! > ../child.pid\nwait\n'
    write_toml(Path(campaign.path), harness, implementer_timeout=60)
    set_config(db, campaign)
    (idea,) = add_ideas(db, campaign, 'long')
    scheduler = make_scheduler(db, runtime, tmp_path)
    assert run_all(scheduler) == [1]
    wait_until((tmp_path / 'runs' / '1' / 'child.pid').exists)
    scheduler.stop(timeout=10)
    with db.session() as s:
        assert s.scalars(select(Candidate).where(Candidate.run_id == 1)).all() == [], 'the base, not a new candidate'
        assert s.get_one(Candidate, 1).verdict is Outcome.KEEP
        assert s.get_one(Idea, idea).status is IdeaStatus.OPEN


@pytest.mark.usefixtures('daemon_api')
@pytest.mark.parametrize('escalated', [False, True])
def test_a_stop_after_the_implementer_ended_its_run_scores_it_on_the_next_start_and_never_requeues_its_idea(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path, escalated: bool
) -> None:
    """An implementer that ended its run has handed it off, even when a job of its own keeps the run open.

    A stop then closes the run as succeeded instead of returning the idea to ``open``, which would start a second run
    of the same work. The commits are pinned and the next daemon scores them and settles the idea, as for any run whose
    scoring a stop cut. An escalation that the implementer left open changes none of this.
    """
    note = (
        "INSERT INTO comments (target_kind, target_id, author, kind, body) VALUES ('idea', 1, 'implementer#1', 'note',"
        " 'the handoff is in workspace/HANDOFF.md')"
    )
    ask = escalation_sql('idea', 1, 'implementer#1')
    harness = (
        f'#!/bin/sh\necho step >> solve.txt\ngit commit -qam "step"\n{PILOOP_HINT} sql "{note}"\n'
        + (f'{PILOOP_HINT} sql "{ask}"\n' if escalated else '')
        + END_RUN
        + 'sleep 30 &\necho $! > ../child.pid\nwait\n'
    )
    write_toml(Path(campaign.path), harness, implementer_timeout=60)
    set_config(db, campaign)
    (idea,) = add_ideas(db, campaign, 'long')
    scheduler = make_scheduler(db, runtime, tmp_path)
    assert run_all(scheduler) == [1]
    pid_file = tmp_path / 'runs' / '1' / 'child.pid'
    wait_until(pid_file.exists)
    scheduler.stop(timeout=10)

    with db.session() as s:
        run = s.get_one(Run, 1)
        assert (run.end, run.error, run.scoring) == (RunEnd.FINISHED, None, True)
        assert s.get_one(Idea, idea).status is IdeaStatus.CLAIMED, 'not ready: a second run would redo the work'
        notes = s.scalars(select(Comment).where(Comment.author == DAEMON_ACTOR)).all()
        assert notes == [], 'its commits are scored, so no note sends the next run to them'
    [step] = pinned(Repo(campaign.path), 'refs/piloop/demo/r1/')
    assert Repo(campaign.path).commit(step).summary == 'step'

    restarted = make_scheduler(db, runtime, tmp_path)
    restarted.reap()
    restarted.wait(SETTLE)
    with db.session() as s:
        [candidate] = s.scalars(select(Candidate).where(Candidate.run_id == 1)).all()
        assert (candidate.summary, candidate.verdict) == ('step', Outcome.KEEP)
        assert s.get_one(Idea, idea).status is IdeaStatus.DONE
        assert s.get_one(Run, 1).scoring is False


def test_reap_kills_an_agent_that_no_executor_adopts(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    """A running agent that a previous daemon left with no job (no lease, no worktree) outlives it: the reap kills it."""
    agent = subprocess.Popen(['sleep', '60'], start_new_session=True)
    with db.session() as s:
        s.add(
            AgentSession(
                kind='implementer',
                campaign_id=campaign.id,
                harness='pi',
                model='m',
                runtime='subprocess',
                handle={
                    'pid': agent.pid,
                    'start_time': proc.started(agent.pid),
                    'log_dir': str(tmp_path),
                    'token': 't',
                },
                state=AgentSessionState.RUNNING,
                log_dir='x',
            )
        )
        s.commit()
    controller = RunController(db, Tracer(None), tmp_path, factory(runtime), executor=Workers())
    scheduler = Scheduler(db, controller, Tracer(None), tmp_path)
    scheduler.reap()
    scheduler.stop(timeout=5)
    assert agent.wait(timeout=5) == -signal.SIGKILL
    with db.session() as s:
        assert s.get_one(AgentSession, 1).state is AgentSessionState.KILLED


def test_a_hand_over_leaves_a_sweep_running_and_the_next_scheduler_re_attaches_to_it(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    """An in-place restart during a sweep keeps the sweep's agent and worktree, and the next daemon closes its row."""
    hold = tmp_path / 'hold'
    hold.touch()
    write_toml(Path(campaign.path), f'#!/bin/sh\necho $$ > ../agent.pid\nwhile [ -f {hold} ]; do sleep 0.05; done\n')
    set_config(db, campaign)
    agent_session_id = request_watchdog(db, campaign)
    first = make_scheduler(db, runtime, tmp_path)
    assert agent_session_id in run_all(first)
    sweep_dir = tmp_path / 'agent_sessions' / str(agent_session_id)
    wait_until((sweep_dir / 'agent.pid').exists)
    first.hand_over(timeout=5)
    with db.session() as s:
        assert s.get_one(AgentSession, agent_session_id).state is AgentSessionState.RUNNING
    assert (sweep_dir / 'worktree').is_dir()

    second = make_scheduler(db, ScriptRuntime(), tmp_path)
    second.reap()
    hold.unlink()
    second.wait(SETTLE)
    first.wait(SETTLE)
    with db.session() as s:
        sweep = s.get_one(AgentSession, agent_session_id)
        assert (sweep.state, sweep.error) == (AgentSessionState.EXITED, None)
    wait_until(lambda: not (sweep_dir / 'worktree').exists())  # once its result is in
    second.stop(timeout=5)


def test_a_wait_that_runs_out_names_what_is_still_in_flight(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = Gate()
    monkeypatch.setattr('piloop.plugins.vcs.git.commands.add_worktree', gate.wrap(git.add_worktree))
    scheduler = make_scheduler(db, runtime, tmp_path)
    add_ideas(db, campaign, 'x')
    assert run_all(scheduler) == [1]
    assert gate.entered.wait(10)
    with pytest.raises(TimeoutError, match=r'^still in flight after 0.1 s: agent_session#1$'):
        scheduler.wait(0.1)
    gate.release.set()
    scheduler.wait(SETTLE)
    scheduler.stop(timeout=5)


def test_stop_before_the_implementer_starts_never_starts_it(
    db: Database,
    campaign: Campaign,
    runtime: ScriptRuntime,
    tmp_path: Path,
    tracer: Tracer,
    spans: InMemorySpanExporter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = Gate()
    monkeypatch.setattr('piloop.plugins.vcs.git.commands.add_worktree', gate.wrap(git.add_worktree))
    scheduler = make_scheduler(db, runtime, tmp_path, tracer)
    (idea,) = add_ideas(db, campaign, 'x')
    run_all(scheduler)
    assert gate.entered.wait(10)
    started = time.monotonic()
    scheduler.stop(timeout=0.5)  # the worker makes the worktree, before it starts the agent
    assert time.monotonic() - started < 3
    gate.release.set()
    scheduler.wait(SETTLE)
    with db.session() as s:
        run = s.get_one(Run, 1)
        assert (run.status, run.end) == (RunStatus.FAILED, RunEnd.DAEMON_STOPPED)
        assert s.get_one(Idea, idea).status is IdeaStatus.OPEN
        agent_session = s.get_one(AgentSession, 1)
        assert (agent_session.state, agent_session.error, agent_session.handle) == (
            AgentSessionState.KILLED,
            RunEnd.DAEMON_STOPPED,
            {},
        ), 'closed by stop(); nothing was started'
        assert s.scalars(select(Lease.state)).all() == [LeaseState.CANCELLED], 'no worker reports it'

    wait_until(lambda: not (tmp_path / 'runs' / '1' / 'worktree').exists())  # the worker's, once its start returns
    assert events(spans, 'agent_session.kill_failed') == [], 'a row without a process is not asked to die'
    traced = dict(span_named(spans, 'piloop.run').attributes or {})
    assert (traced['status'], traced['end']) == (RunStatus.FAILED, RunEnd.DAEMON_STOPPED)
    assert dict(span_named(spans, 'piloop.agent').attributes or {})['state'] == 'killed'


def test_stop_waits_for_a_start_in_flight_then_kills_the_implementer(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_toml(Path(campaign.path), HARNESS_SLEEPS, implementer_timeout=60)
    set_config(db, campaign)
    gate = Gate()
    monkeypatch.setattr(runtime, 'start', gate.wrap(runtime.start))
    scheduler = make_scheduler(db, runtime, tmp_path)
    add_ideas(db, campaign, 'long')
    run_all(scheduler)
    assert gate.entered.wait(10)
    stopper = threading.Thread(target=scheduler.stop, args=(10,))
    stopper.start()
    stopper.join(0.3)
    assert stopper.is_alive(), 'stop() waits for the handle to be published'
    with db.session() as s:
        assert (s.get_one(AgentSession, 1).state, s.get_one(AgentSession, 1).handle) == (
            AgentSessionState.ENDING,
            {},
        ), 'the stop is on the row before its kill'
    gate.release.set()
    stopper.join(10)
    assert not stopper.is_alive()
    with db.session() as s:
        agent_session = s.get_one(AgentSession, 1)
        assert (agent_session.state, agent_session.error) == (AgentSessionState.KILLED, RunEnd.DAEMON_STOPPED)
        assert proc.started(int(str(agent_session.handle['pid']))) is None
        run = s.get_one(Run, 1)
        assert (run.status, run.end) == (RunStatus.FAILED, RunEnd.DAEMON_STOPPED)
    assert active(scheduler) == []


@pytest.mark.usefixtures('daemon_api')
def test_stop_kills_the_evaluator_and_scores_nothing_more(
    db: Database,
    campaign: Campaign,
    runtime: ScriptRuntime,
    tmp_path: Path,
    tracer: Tracer,
    spans: InMemorySpanExporter,
) -> None:
    pid_file = tmp_path / 'evaluator.pid'
    write_toml(Path(campaign.path), HARNESS_TWO_COMMITS + END_RUN)
    # by a rename: `>` creates the file before echo writes it, and the test would read it empty
    set_config(
        db, campaign, evaluate=['sh', '-c', f'echo $$ > {pid_file}.part; mv {pid_file}.part {pid_file}; exec sleep 30']
    )
    scheduler = make_scheduler(db, runtime, tmp_path, tracer)
    add_ideas(db, campaign, 'two commits')
    run_all(scheduler)
    wait_until(pid_file.exists)
    pid = int(pid_file.read_text())

    started = time.monotonic()
    scheduler.stop(timeout=10)
    assert time.monotonic() - started < 3, 'stop() does not wait for evaluate_timeout'
    assert proc.started(pid) is None
    assert active(scheduler) == []
    with db.session() as s:
        run = s.get_one(Run, 1)
        assert (run.status, run.error) == (RunStatus.SUCCEEDED, None), 'closed when its implementer ended'
        assert s.get_one(Idea, 1).status is IdeaStatus.CLAIMED, (
            'its run finished and only its scoring was cut: the next daemon scores it, the idea is not run again'
        )
        assert s.get_one(AgentSession, 1).state is AgentSessionState.EXITED, (
            'the implementer had finished; only scoring was cut'
        )
        made = s.scalars(select(Candidate).where(Candidate.run_id == 1).order_by(Candidate.id)).all()
        assert [(c.summary, c.verdict, c.measurements) for c in made] == [
            ('first', None, []),
            ('second', None, []),
        ], 'both trees are candidates; the measurement of the first was killed and nothing judged them'
    measured = [sp for sp in spans.get_finished_spans() if sp.name == 'piloop.measure']
    assert len(measured) == 1, 'the second commit is never measured'
    assert dict(measured[0].attributes or {})['status'] == 'cancelled'
    traced = dict(span_named(spans, 'piloop.run').attributes or {})
    assert (traced['status'], traced['end']) == (RunStatus.SUCCEEDED, RunEnd.FINISHED), 'only its scoring was cut'
    assert not (tmp_path / 'runs' / '1' / 'worktree').exists()
    assert list((tmp_path / 'runs' / '1' / 'eval-0').glob('lease-*/worktree')) == []


@pytest.mark.usefixtures('daemon_api')
def test_a_restart_during_an_evaluation_scores_the_run_on_start_and_never_runs_its_idea_again(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    """After a restart during an evaluation, the next daemon scores the run's commits and does not run its idea again.

    The next daemon scores and judges every commit of the run, and settles the idea once.
    """
    hold = tmp_path / 'hold'
    hold.touch()
    write_toml(Path(campaign.path), HARNESS_TWO_COMMITS + END_RUN)
    # the baseline, the run's bar under this new evaluator, is measured at once: the cut is in the run's own evaluation
    held = f'[ -f {hold} ] && [ $(wc -l < solve.txt) -gt 1 ]'
    wait_then_score = f'while {held}; do sleep 0.05; done'
    set_config(
        db, campaign, evaluate=['sh', '-c', f'{wait_then_score}; {reports(score="$(wc -l < solve.txt)", size="1")}']
    )
    first = make_scheduler(db, runtime, tmp_path)
    add_ideas(db, campaign, 'two commits')
    run_all(first)
    wait_until(lambda: list((tmp_path / 'runs' / '1' / 'eval-0').glob('lease-*/candidate-0/eval.stdout')))
    first.stop(timeout=10)
    with db.session() as s:
        assert s.get_one(Idea, 1).status is IdeaStatus.CLAIMED
        assert [c.verdict for c in s.scalars(select(Candidate).where(Candidate.run_id == 1))] == [None] * 2

    hold.unlink()
    second = make_scheduler(db, runtime, tmp_path)
    second.reap()
    second.wait(SETTLE)
    with db.session() as s:
        made = s.scalars(select(Candidate).where(Candidate.run_id == 1).order_by(Candidate.id)).all()
        assert [(c.summary, c.verdict, len(c.measurements)) for c in made] == [
            ('first', Outcome.KEEP, 1),
            ('second', Outcome.KEEP, 1),
        ], 'the cut commit and the one never reached are both measured and judged'
        assert s.get_one(Idea, 1).status is IdeaStatus.DONE, 'settled by its one finished run'
    run_all(second)
    second.wait(SETTLE)
    with db.session() as s:
        assert s.scalars(select(Run.idea_id).order_by(Run.id)).all() == [1, None], (
            'the idea was never claimed again: the fresh run of the free slot found no open idea'
        )
    second.stop(timeout=5)


@pytest.mark.parametrize(('before', 'after'), [(IdeaStatus.CLAIMED, IdeaStatus.OPEN), (IdeaStatus.DROPPED, None)])
def test_a_run_whose_scoring_cannot_resume_returns_its_idea_to_the_queue(
    db: Database,
    campaign: Campaign,
    runtime: ScriptRuntime,
    tmp_path: Path,
    tracer: Tracer,
    spans: InMemorySpanExporter,
    before: IdeaStatus,
    after: IdeaStatus | None,
) -> None:
    """``reap`` resumes every run that a stopped daemon was scoring, whatever its idea's status, and the run settles.

    A human may have parked the idea meanwhile: it stays parked, and the run gives back its place under ``max_runs``.
    """
    (idea,) = add_ideas(db, campaign, 'orphan')
    with db.session() as s:
        s.add(
            Run(
                campaign_id=campaign.id,
                idea_id=idea,
                attempt=1,
                base_candidate_id=1,
                harness='script',
                model='m',
                status=RunStatus.SUCCEEDED,
                end=RunEnd.FINISHED,
                scoring=True,
            )
        )
        s.get_one(Idea, idea).status = before
        s.get_one(Campaign, campaign.id).path = str(tmp_path / 'gone')
        s.commit()
    sched = make_scheduler(db, runtime, tmp_path, tracer)
    sched.reap()
    sched.wait(SETTLE)
    with db.session() as s:
        assert s.get_one(Idea, idea).status is (after or before), 'attempts remain; a parked idea stays parked'
        assert s.scalar(select(Run.scoring)) is False
    assert span_named(spans, 'piloop.resume').status.status_code is StatusCode.ERROR


def measured_by(db: Database, candidate_id: CandidateId) -> list[tuple[int, float | None, str | None]]:
    with db.session() as s:
        rows = s.get_one(Candidate, candidate_id).measurements
        return [(m.evaluator_id, m.metrics.get('score'), m.error) for m in rows]


def test_the_next_pass_measures_a_registered_baseline_with_its_links_and_unpinned_runs_wait_for_it(
    db: Database, campaign_path: Path, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    """Registering measures nothing. The daemon's next pass does, also the first pass of a daemon that restarted."""
    write_toml(campaign_path, HARNESS_APPEND, evaluate=['sh', '-c', reports(score='$(cat data/x.csv)', size='1')])
    with db.session() as s:
        campaign, baseline = campaigns.register(s, campaign_path, System.HUMAN)
        s.commit()
        assert baseline.measurements == []
    add_ideas(db, campaign, 'one more line')
    sched = make_scheduler(db, runtime, tmp_path)
    assert run_all(sched) == [], 'the run waits for the baseline'
    sched.wait(SETTLE)
    assert measured_by(db, baseline.id) == [(1, 1.0, None)], 'the linked data/x.csv is in its checkout'
    assert run_all(sched) == [1]


def test_a_baseline_that_fails_to_evaluate_pauses_its_campaign_and_one_measured_before_does_not(
    db: Database, campaign_path: Path, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    write_toml(campaign_path, HARNESS_APPEND, evaluate=['false'])
    with db.session() as s:
        campaign, baseline = campaigns.register(s, campaign_path, System.HUMAN)
        s.commit()
    sched = make_scheduler(db, runtime, tmp_path)

    def evaluated(evaluate: list[str] = EVALUATE) -> Campaign:
        """The campaign once the daemon measured its baseline with ``evaluate``, and acted on what that did."""
        write_toml(campaign_path, HARNESS_APPEND, evaluate=evaluate)
        with db.session() as s:
            sync(s, s.get_one(Campaign, campaign.id))
            s.commit()
        run_all(sched)
        sched.wait(SETTLE)
        run_all(sched)
        with db.session() as s:
            return s.get_one(Campaign, campaign.id)

    logs = tmp_path / 'projects' / str(campaign.id) / 'measure' / f'{baseline.id}-e1'
    held = evaluated(['false'])
    assert (held.status, held.paused, held.paused_reason) == (
        CampaignStatus.ACTIVE,
        Pause.LAUNCHES,
        f'the baseline failed to evaluate: evaluate exit 1: ; its logs are in {logs}; fix it, then'
        f' `piloop candidate remeasure {baseline.id}` and `piloop resume demo`',
    )
    assert evaluated().paused is Pause.LAUNCHES, 'the pause still measures: a fixed evaluate command measures it'
    with db.session() as s:  # the human resumes it
        ops.resume(s, campaign.id)
        s.commit()
    assert evaluated(['false', 'again']).paused is None, 'an older evaluator measured the baseline'
    assert [error is None for _, _, error in measured_by(db, baseline.id)] == [False, True, False]


@pytest.mark.usefixtures('daemon_api')
def test_a_new_evaluator_measures_the_head_runs_wait_for_it_and_older_keeps_only_on_request(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    """A new evaluator measures the head again and unpinned runs wait for it. An older keep waits for someone to ask.

    Without the head's score, new runs cannot be compared with the kept lineage. Every older keep measured again
    unasked held the evaluations for hours after each change of the evaluator.
    """
    write_toml(Path(campaign.path), HARNESS_APPEND + END_RUN)
    set_config(db, campaign)
    sched = make_scheduler(db, runtime, tmp_path)
    add_ideas(db, campaign, 'one more line')
    run_all(sched)
    sched.wait(SETTLE)
    with db.session() as s:
        old = s.get_one(Campaign, campaign.id).evaluator_id
        kept = s.scalars(select(Candidate).where(Candidate.run_id == 1)).one()
        assert kept.verdict is Outcome.KEEP
    doubled = ['sh', '-c', reports(score='$(( 2 * $(wc -l < solve.txt) ))', size='1')]
    write_toml(Path(campaign.path), HARNESS_APPEND, evaluate=doubled)
    with db.session() as s:
        proj = s.get_one(Campaign, campaign.id)
        sync(s, proj)
        s.commit()
        new = proj.evaluator_id
    assert new != old
    add_ideas(db, campaign, 'another line')

    assert run_all(sched) == [], 'no keep has a score under the new evaluator: the run waits instead of being parked'
    assert statuses(db) == [IdeaStatus.DONE, IdeaStatus.OPEN]
    sched.wait(SETTLE)
    assert measured_by(db, kept.id) == [(old, 2.0, None), (new, 4.0, None)], 'the head first'
    assert measured_by(db, CandidateId(1)) == [(old, 1.0, None)], 'one at a time'

    assert run_all(sched) == [2], 'the head has a score: the run starts from it'
    sched.wait(SETTLE)
    assert measured_by(db, CandidateId(1)) == [(old, 1.0, None)], (
        'the baseline is no base or bar of any run: nobody asked'
    )
    with db.session() as s:
        assert s.get_one(Run, 2).base_candidate_id == kept.id
        assert s.get_one(Candidate, kept.id).verdict is Outcome.KEEP, 'measuring again judges nothing again'
        request_remeasure(s, s.get_one(Candidate, 1), System.HUMAN, repeats=1)
        s.commit()
    run_all(sched)
    sched.wait(SETTLE)
    assert measured_by(db, CandidateId(1)) == [(old, 1.0, None), (new, 2.0, None)], 'measured when asked'
    with db.session() as s:
        assert s.get_one(Candidate, 1).verdict is Outcome.KEEP


@pytest.mark.usefixtures('daemon_api')
def test_after_a_new_evaluator_runs_start_from_the_head_once_the_head_alone_is_measured(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    """Implementer slots sat idle while old keeps, neither base nor bar of any idea, were measured again one at a
    time. A run starts from the head and is judged against it."""
    write_toml(Path(campaign.path), HARNESS_APPEND + END_RUN)
    set_config(db, campaign)
    sched = make_scheduler(db, runtime, tmp_path)
    add_ideas(db, campaign, 'first line')
    run_all(sched)
    sched.wait(SETTLE)
    add_ideas(db, campaign, 'second line')
    run_all(sched)
    sched.wait(SETTLE)
    with db.session() as s:
        older, newer = (s.scalars(select(Candidate).where(Candidate.run_id == r)).one().id for r in (1, 2))
    doubled = ['sh', '-c', reports(score='$(( 2 * $(wc -l < solve.txt) ))', size='1')]
    write_toml(Path(campaign.path), HARNESS_APPEND, evaluate=doubled)
    with db.session() as s:
        proj = s.get_one(Campaign, campaign.id)
        sync(s, proj)
        new = proj.evaluator_id
        assert new is not None
        s.add(Measurement(candidate_id=newer, evaluator_id=new, metrics={'score': 6.0, 'size': 1.0}))
        s.commit()
        assert due(s, proj) == [], 'the head has a score: the older keeps wait for an ask'
    (free,) = add_ideas(db, campaign, 'free')
    assert run_all(sched) == [3], 'the head has a score'
    with db.session() as s:
        assert (s.get_one(Run, 3).idea_id, s.get_one(Run, 3).base_candidate_id) == (free, newer)
    sched.wait(SETTLE)
    sched.stop(timeout=5)


@pytest.mark.usefixtures('daemon_api')
def test_after_a_new_evaluator_no_run_starts_from_the_best_until_every_keep_is_measured(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    """After a new evaluator, no run starts until every keep is measured again.

    A keep can land while the new evaluator measures an older one. If the queue opened when the older keep had a
    score, runs would start from it, because the newer keep has no score yet.
    """
    write_toml(Path(campaign.path), HARNESS_APPEND + END_RUN)
    set_config(db, campaign)
    sched = make_scheduler(db, runtime, tmp_path)
    add_ideas(db, campaign, 'first line')
    run_all(sched)
    sched.wait(SETTLE)
    (second,) = add_ideas(db, campaign, 'second line')
    run_all(sched)
    sched.wait(SETTLE)
    with db.session() as s:
        older, newer = (s.scalars(select(Candidate).where(Candidate.run_id == r)).one().id for r in (1, 2))
        assert [s.get_one(Candidate, c).verdict for c in (older, newer)] == [Outcome.KEEP, Outcome.KEEP]
    doubled = ['sh', '-c', reports(score='$(( 2 * $(wc -l < solve.txt) ))', size='1')]
    write_toml(Path(campaign.path), HARNESS_APPEND, max_implementers=2, evaluate=doubled)
    with db.session() as s:
        proj = s.get_one(Campaign, campaign.id)
        sync(s, proj)
        new = proj.evaluator_id
        assert new is not None
        for candidate, value in ((1, 2.0), (older, 4.0)):  # measured before the newer keep landed
            s.add(Measurement(candidate_id=candidate, evaluator_id=new, metrics={'score': value, 'size': 1.0}))
        s.commit()
    (chained,) = add_ideas(db, campaign, 'chained', after=(second,))
    (free,) = add_ideas(db, campaign, 'free')

    assert run_all(sched) == [], 'the newer keep has no score: no base is known to be the best'
    sched.wait(SETTLE)
    assert measured_by(db, newer)[-1] == (new, 6.0, None)
    assert sorted(run_all(sched)) == [3, 4]
    with db.session() as s:
        assert {s.get_one(Run, r).idea_id: s.get_one(Run, r).base_candidate_id for r in (3, 4)} == {
            chained: newer,
            free: newer,
        }
    sched.stop(timeout=5)
    sched.stop(timeout=5)


def test_an_imported_reference_is_measured_by_the_daemon_and_stays_a_reference(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    repo = Repo(campaign.path)
    repo.git.checkout('-qb', 'elsewhere')
    (Path(campaign.path) / 'solve.txt').write_text('a\nb\nc\n')
    repo.git.commit('-qam', 'three lines')
    repo.git.checkout('-q', 'main')
    with db.session() as s:
        proj = s.get_one(Campaign, campaign.id)
        reference = import_reference(
            s, proj, GitRepository(repo, PILOOP), 'elsewhere', summary=None, actor=System.ORCHESTRATOR
        ).id
        evaluator = proj.evaluator_id
        s.commit()
    write_toml(Path(campaign.path), '#!/bin/sh\ntrue\n')  # the fresh run of the free slot changes nothing
    set_config(db, campaign)
    sched = make_scheduler(db, runtime, tmp_path)
    run_all(sched)
    sched.wait(SETTLE)
    assert measured_by(db, reference) == [(evaluator, 3.0, None)]
    with db.session() as s:
        assert s.get_one(Candidate, reference).verdict is None, 'measured, never judged'
        trunk = head(s, s.get_one(Campaign, campaign.id))
        assert trunk is not None and trunk.id == 1, "it scores 3 to the baseline's 1 and is still not the head"
    sched.stop(timeout=5)


def test_a_keep_that_cannot_be_checked_out_is_measured_as_a_crash_once(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    with db.session() as s:
        s.add(
            Candidate(
                campaign_id=campaign.id,
                tree_sha='0' * 40,
                revision='0' * 40,
                verdict=Outcome.KEEP,
                verdict_by='human',
            )
        )
        s.commit()
    sched = make_scheduler(db, runtime, tmp_path)
    run_all(sched)
    sched.wait(SETTLE)
    [(_, score, error)] = measured_by(db, CandidateId(2))
    assert score is None and error is not None and error.endswith('names no commit in ' + campaign.path), error
    run_all(sched)
    sched.wait(SETTLE)
    assert len(measured_by(db, CandidateId(2))) == 1, 'a crash is an answer: not measured again'
    sched.stop(timeout=5)


def test_stop_before_a_sweep_starts_never_starts_it(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = Gate()
    monkeypatch.setattr('piloop.plugins.campaign_type.consumers.compose_brief', gate.wrap(compose_brief))
    occupy_the_run_slot(db, campaign)
    scheduler = make_scheduler(db, runtime, tmp_path)
    agent_session_id = request_watchdog(db, campaign)
    assert run_all(scheduler) == [agent_session_id]
    assert gate.entered.wait(10)
    started = time.monotonic()
    scheduler.stop(timeout=0.5)  # the sweep thread is composing its brief, before its start window
    assert time.monotonic() - started < 3
    gate.release.set()
    scheduler.wait(SETTLE)
    with db.session() as s:
        agent_session = s.get_one(AgentSession, agent_session_id)
        assert (agent_session.state, agent_session.error, agent_session.harness, agent_session.handle) == (
            AgentSessionState.KILLED,
            RunEnd.DAEMON_STOPPED,
            'script',
            {},
        )
        assert s.scalars(select(Event).where(Event.kind == 'agent_session.started')).all() == []
    assert not (tmp_path / 'agent_sessions' / str(agent_session_id)).exists(), 'nothing was started'


def test_stop_waits_for_a_sweep_start_in_flight_then_kills_it(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_toml(Path(campaign.path), HARNESS_SLEEPS)
    set_config(db, campaign)
    gate = Gate()
    monkeypatch.setattr(runtime, 'start', gate.wrap(runtime.start))
    occupy_the_run_slot(db, campaign)
    scheduler = make_scheduler(db, runtime, tmp_path)
    agent_session_id = request_watchdog(db, campaign)
    assert run_all(scheduler) == [agent_session_id]
    assert gate.entered.wait(10)
    stopper = threading.Thread(target=scheduler.stop, args=(10,))
    stopper.start()
    stopper.join(0.3)
    assert stopper.is_alive(), 'stop() waits for the handle to be published'
    with db.session() as s:
        agent_session = s.get_one(AgentSession, agent_session_id)
        assert (agent_session.state, agent_session.harness, agent_session.handle) == (
            AgentSessionState.ENDING,
            'script',
            {},
        ), 'the stop is on the row before its kill'
    gate.release.set()
    stopper.join(10)
    assert not stopper.is_alive()
    with db.session() as s:
        agent_session = s.get_one(AgentSession, agent_session_id)
        assert (agent_session.state, agent_session.error) == (AgentSessionState.KILLED, RunEnd.DAEMON_STOPPED)
        assert proc.started(int(str(agent_session.handle['pid']))) is None
    assert active(scheduler) == []


class PromptingRuntime(ScriptRuntime):
    """A subprocess runtime with a prompt channel that records what the daemon sends."""

    def __init__(self) -> None:
        super().__init__()
        self.prompts: list[tuple[str, str]] = []

    def prompt(self, handle: Handle, text: str) -> None:
        self.prompts.append((str(handle.data['agent']), text))


def running_agent_session(
    db: Database, campaign: Campaign, title: str, *, state: AgentSessionState = AgentSessionState.RUNNING
) -> int:
    """Add an idea with one run that started now and its agent session row in ``state``.

    The idea, the run and the implementer have the same id.
    """
    (idea,) = add_ideas(db, campaign, title)
    with db.session() as s:
        s.get_one(Idea, idea).status = IdeaStatus.CLAIMED
        run = Run(
            campaign_id=campaign.id,
            idea_id=idea,
            attempt=1,
            base_candidate_id=1,
            harness='script',
            model='m',
            status=RunStatus.RUNNING,
            started_at=utcnow(),
        )
        s.add(run)
        s.flush()
        s.add(
            AgentSession(
                kind=IMPLEMENTER,
                campaign_id=campaign.id,
                subject=TargetRef(RUN, run.id),
                harness='script',
                model='m',
                runtime='subprocess',
                handle={'agent': f'implementer-{run.id}', 'pid': 4242},
                state=state,
                log_dir='x',
            )
        )
        s.commit()
    return idea


def lease_running(db: Database) -> None:
    """Put each running agent session on a live lease of a worker elsewhere, as the daemon's are."""
    with db.session() as s:
        leased = select(Lease.agent_session_id).where(Lease.agent_session_id.is_not(None))
        rows = s.scalars(
            select(AgentSession).where(AgentSession.state == AgentSessionState.RUNNING, AgentSession.id.not_in(leased))
        ).all()
        for w in rows:
            now = utcnow()
            job = home_cas(db).put(json.dumps({'kind': 'agent', 'agent_session': w.id}).encode())
            handle: dict[str, object] = {'runtime': w.runtime, 'data': w.handle}
            s.add(
                Lease(
                    kind=AGENT,
                    agent_session_id=w.id,
                    state=LeaseState.RUNNING,
                    worker='elsewhere',
                    job_cid=job,
                    handle=handle,
                    renewed_at=now,
                    expires_at=now + timedelta(hours=1),
                )
            )
        s.commit()


def revoked(db: Database) -> list[object]:
    """The agents whose leases the daemon revoked: what it asked their workers to kill."""
    with db.session() as s:
        leases = s.scalars(select(Lease).where(Lease.revoked_at.is_not(None)).order_by(Lease.id)).all()
        handles = [lease.handle['data'] for lease in leases]
        return [data['agent'] for data in handles if isinstance(data, dict)]


def comment(
    db: Database, kind: str, target_kind: str, target_id: int, body: str, *, author: str = 'human', priority: int = 2
) -> CommentId:
    """Add a comment through the raw SQL path that agents use. ``created_at`` is CURRENT_TIMESTAMP, to the second.

    Each comment is addressed to the human at ``priority``. An escalation must have this, and other kinds may. The
    push to the implementer ignores it.
    """
    with closing(sql.connect(Path(db.engine.url.database or ''), actor=ACTORS.validate_python(author))) as con:
        return CommentId(
            sql.run(
                con,
                f"INSERT INTO comments (target_kind, target_id, author, kind, body, priority) VALUES ('{target_kind}',"
                f" {target_id}, '{author}', '{kind}', '{body}', {priority})",
            ).last_insert_rowid
        )


def settled(db: Database, kind: str) -> list[tuple[int, EventModel]]:
    with db.session() as s:
        return [(e.target_id, e.payload) for e in s.scalars(select(Event).where(Event.kind == kind))]


@dataclass
class Prompted:
    """``Executor.prompt`` that records each prompt and, when ``answers``, records it queued at once."""

    results: CampaignResults
    answers: bool = True
    prompts: list[tuple[int, Prompt]] = field(default_factory=list)

    def __call__(self, key: AgentKey, prompt: Prompt) -> None:
        self.prompts.append((key.agent_session, prompt))
        if self.answers:
            self.results.queued(key, prompt.id, None)


def prompted(db: Database, scheduler: Scheduler, mocker: MockerFixture, *, answers: bool = True) -> Prompted:
    """Replace the executor's ``prompt`` of ``scheduler`` with a ``Prompted``."""
    fake = Prompted(
        CampaignResults(
            db, Tracer(None), campaigns_of(scheduler)._home, disk(campaigns_of(scheduler)._home), lambda: False
        ),
        answers,
    )
    mocker.patch.object(scheduler._controller.executor, 'prompt', side_effect=fake)
    return fake


def test_messages_for_a_running_agent_go_to_its_executor_once_each_until_they_have_an_outcome_and_notes_never_do(
    db: Database, campaign: Campaign, tmp_path: Path, tracer: Tracer, spans: InMemorySpanExporter, mocker: MockerFixture
) -> None:
    runtime = PromptingRuntime()
    idea = running_agent_session(db, campaign, 'live')
    coming_up = running_agent_session(db, campaign, 'coming up', state=AgentSessionState.STARTING)
    with db.session() as s:
        before = utcnow() - timedelta(minutes=5)
        s.add(
            Comment(
                target=TargetRef(IDEA, idea), author='human', kind='message', body='in the brief', created_at=before
            )
        )
        s.commit()
    scheduler = make_scheduler(db, runtime, tmp_path, tracer)
    executor = prompted(db, scheduler, mocker)
    assert run_all(scheduler) == [] and executor.prompts == [], 'a message from before the run started is in its brief'

    steer = comment(db, 'message', 'idea', idea, 'keep solve.py under 120 lines\nthe rest is detail')
    on_run = comment(db, 'message', 'run', 1, 'x' * 250, author='orchestrator')
    comment(db, 'note', 'run', 1, 'on the board: for whoever reads the run', author='orchestrator')
    comment(db, 'decision', 'idea', idea, 'for the human, not the pane')
    comment(db, 'note', 'campaign', campaign.id, 'not for any implementer')
    waiting = comment(db, 'message', 'idea', coming_up, 'its implementer is starting: waits for its pane')
    comment(db, 'escalation', 'idea', idea, 'for the human, not the pane')
    direct = comment(db, 'message', 'agent_session', 1, 'piloop prompt: the text as written\nall of it')
    run_all(scheduler)
    assert executor.prompts == [
        (
            1,
            Prompt(
                steer,
                '[piloop] message on idea#1 by human: keep solve.py under 120 lines — full text: SELECT body FROM'
                f' comments WHERE id={steer}',
            ),
        ),
        (
            1,
            Prompt(
                on_run,
                f'[piloop] message on run#1 by orchestrator: {"x" * 199}… — full text: SELECT body FROM comments'
                f' WHERE id={on_run}',
            ),
        ),
        (1, Prompt(direct, 'piloop prompt: the text as written\nall of it')),
    ], 'notes, a decision and an escalation are on the board, and only a message is sent'
    assert settled(db, 'comment.queued') == [
        (1, PromptOutcome(kind='comment.queued', comment=c)) for c in (steer, on_run, direct)
    ]
    assert events(spans, 'comment.queued') == [{'comment': c, 'agent_session': 1} for c in (steer, on_run, direct)]
    run_all(scheduler)
    assert len(executor.prompts) == 3, 'settled by the events: nothing is sent twice'

    with db.session() as s:
        s.get_one(AgentSession, 2).state = AgentSessionState.RUNNING
        s.commit()
    run_all(scheduler)
    assert executor.prompts[-1][0] == 2 and executor.prompts[-1][1].id == waiting, (
        'a comment that landed while the pane was coming up is sent once the implementer runs'
    )

    restarted = make_scheduler(db, runtime, tmp_path)
    executor = prompted(db, restarted, mocker, answers=False)
    run_all(restarted)
    assert executor.prompts == [], 'a new daemon replays nothing that has an outcome: the record is in the database'
    later = comment(db, 'message', 'run', 1, 'and stop after the next commit')
    run_all(restarted)
    run_all(restarted)
    assert [p.id for _, p in executor.prompts] == [later], 'once per daemon process, also without an outcome yet'
    again = make_scheduler(db, runtime, tmp_path, tracer)
    executor = prompted(db, again, mocker)
    run_all(again)
    assert [p.id for _, p in executor.prompts] == [later], 'the next daemon sends what has no outcome again'
    refused = comment(db, 'message', 'run', 1, 'the executor fails to take it once')
    locked = OperationalError('SELECT', {}, Exception('locked'))
    taking = mocker.patch.object(again._controller.executor, 'prompt', side_effect=[locked, None])
    run_all(again)
    run_all(again)
    assert [c.args[1].id for c in taking.call_args_list] == [refused, refused], 'the next tick sends it again'


def test_an_implementers_own_messages_are_not_echoed_back(
    db: Database, campaign: Campaign, tmp_path: Path, mocker: MockerFixture
) -> None:
    idea = running_agent_session(db, campaign, 'live')
    scheduler = make_scheduler(db, PromptingRuntime(), tmp_path)
    executor = prompted(db, scheduler, mocker)
    comment(db, 'message', 'idea', idea, 'what I tried', author='implementer#1')
    comment(db, 'message', 'run', 1, 'signed the other documented way', author='run#1')
    run_all(scheduler)
    assert executor.prompts == [] and settled(db, 'comment.queued') == []
    comment(db, 'message', 'idea', idea, 'a sibling implementer is not the author', author='implementer#2')
    run_all(scheduler)
    assert [p.text.split(' — ')[0] for _, p in executor.prompts] == [
        '[piloop] message on idea#1 by implementer#2: a sibling implementer is not the author'
    ]


def test_a_message_that_cannot_be_delivered_is_settled_as_undelivered_and_never_fails_the_tick(
    db: Database,
    campaign: Campaign,
    runtime: ScriptRuntime,
    tmp_path: Path,
    tracer: Tracer,
    spans: InMemorySpanExporter,
) -> None:
    """A message that cannot reach the implementer is settled as undelivered, and the tick does not fail.

    Here the row runs without a lease, so ``workers`` says why at once. The tick does not try again.
    """
    idea = running_agent_session(db, campaign, 'headless')
    scheduler = make_scheduler(db, runtime, tmp_path, tracer)
    stuck = comment(db, 'message', 'idea', idea, 'a headless implementer has no input channel')
    assert run_all(scheduler) == []
    error = 'the agent has no live lease'
    assert settled(db, 'comment.queued') == []
    assert settled(db, 'comment.undelivered') == [
        (1, PromptOutcome(kind='comment.undelivered', comment=stuck, error=error))
    ]
    assert events(spans, 'comment.undelivered') == [{'comment': stuck, 'agent_session': 1, 'error': error}]
    run_all(scheduler)
    assert len(settled(db, 'comment.undelivered')) == 1 and len(events(spans, 'comment.undelivered')) == 1, (
        'settled: the next brief carries the comment, retrying would fail the same way'
    )


@pytest.mark.usefixtures('submitting')
def test_a_run_frees_its_slot_when_its_implementer_ends_and_its_idea_settles_once_scored(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    """A run frees its implementer slot when its implementer ends, and its idea settles only when the commits are scored."""
    go = tmp_path / 'go'
    gated = f'while [ ! -f {go} ]; do sleep 0.05; done; {reports(score="$(wc -l < solve.txt)", size="1")}'
    set_config(db, campaign, evaluate=['sh', '-c', gated])
    first, second = add_ideas(db, campaign, 'first', 'second')
    with db.session() as s:  # the new evaluator measured the baseline, so runs start from it
        p = s.get_one(Campaign, campaign.id)
        evaluator = current_evaluator(s, p, p.config)
        s.add(Measurement(candidate_id=1, evaluator_id=evaluator.id, metrics={'score': 1, 'size': 1}))
        s.commit()
    scheduler = make_scheduler(db, runtime, tmp_path)
    assert run_all(scheduler) == [1]
    # the run closes, then its commit becomes a candidate: wait for both, the evaluation is gated
    wait_until(lambda: candidate_rows(db, RunId(1)))
    with db.session() as s:
        run = s.get_one(Run, 1)
        assert (run.status, run.finished_at is not None) == (RunStatus.SUCCEEDED, True), 'closed: its implementer ended'
        assert s.get_one(Idea, first).status is IdeaStatus.CLAIMED, 'its commit is not judged yet'
        assert s.scalars(select(Candidate).where(Candidate.run_id == 1)).one().verdict is None
    assert run_all(scheduler) == [2], 'the one slot is free while run#1 is scored'
    go.touch()
    scheduler.wait(SETTLE)
    assert statuses(db) == [IdeaStatus.DONE, IdeaStatus.DONE]
    with db.session() as s:
        assert s.scalars(select(Candidate).where(Candidate.run_id == 1)).one().verdict is Outcome.KEEP
        assert s.get_one(Idea, second).status is IdeaStatus.DONE


def candidate_rows(db: Database, run_id: RunId) -> int:
    with db.session() as s:
        return len(s.scalars(select(Candidate).where(Candidate.run_id == run_id)).all())


def stop_by_hand(tmp_path: Path, campaign: Campaign) -> None:
    """Stop the campaign as a person's UPDATE does: not the daemon's stop, so it gets no closing sweep."""
    with closing(sql.connect(tmp_path / 'piloop.db', actor=System.HUMAN)) as con:
        sql.run(
            con, f"UPDATE campaigns SET status = 'stopped', stopped_reason = 'by the test' WHERE id = {campaign.id}"
        )


def stop_campaign(db: Database, campaign: Campaign, reason: str) -> None:
    with db.session() as s:
        p = s.get_one(Campaign, campaign.id)
        p.status, p.stopped_reason = CampaignStatus.STOPPED, reason
        s.commit()


def test_implementers_of_a_stopped_campaign_are_told_to_wrap_up_then_ended_after_the_grace(
    db: Database, campaign: Campaign, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker: MockerFixture
) -> None:
    """When a campaign stops, its implementers are told to wrap up, and they are ended after the grace period."""
    runtime = PromptingRuntime()
    running_agent_session(db, campaign, 'in flight')
    with db.session() as s:
        s.add(
            AgentSession(
                kind=RESEARCH,
                campaign_id=campaign.id,
                runtime='subprocess',
                handle={'agent': 'research-2', 'pid': 4243},
                state=AgentSessionState.RUNNING,
            )
        )
        s.commit()
    lease_running(db)
    scheduler = make_scheduler(db, runtime, tmp_path)
    fake = prompted(db, scheduler, mocker)
    run_all(scheduler)
    assert fake.prompts == [], 'an active campaign is left alone'

    stop_campaign(db, campaign, 'max_runs: 2 runs')
    run_all(scheduler)
    run_all(scheduler)
    assert [(agent, p.text) for agent, p in fake.prompts] == snapshot(
        [
            (
                1,
                '[piloop] The campaign stopped (max_runs: 2 runs). Save what you have now, with a summary that says where it stands. Leave a note on what you did, then end your run (`piloop run end`). This session ends in 10 minutes.',
            )
        ]
    ), 'a sweep is bounded by its timeout and the closing watchdog is one: only implementers are wound down'
    assert settled(db, 'agent_session.wrap_up') == [(1, WrapUp(reason='max_runs: 2 runs', comment=1))]
    assert runtime.prompts == [], 'a note that its executor delivers, never straight to the runtime'
    run_all(scheduler)
    assert (len(fake.prompts), revoked(db)) == (1, []), 'told once; ended only when the grace is over'

    timings(db, campaign, wrap_up_grace=0)
    run_all(scheduler)
    assert revoked(db) == ['implementer-1']


def test_an_implementer_that_the_stop_ends_mid_turn_closes_its_run_as_cut_by_the_campaign_stop(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An implementer in the middle of a turn takes a message only when the turn ends, so the wrap-up prompt
    may never reach it before the grace period ends. The run then closed as "implementer produced no commits" or with the
    kill's exit status, and nothing named the stop.

    The run closes with ``campaign stopped``: not an attempt at its idea, and not counted toward a run limit. What
    the implementer committed, and what it left uncommitted, is still scored.
    """
    harness = '#!/bin/sh\necho step >> solve.txt\ngit commit -qam "step"\necho draft >> solve.txt\nexec sleep 30\n'
    write_toml(Path(campaign.path), harness, implementer_timeout=60)
    set_config(db, campaign)
    (idea,) = add_ideas(db, campaign, 'long')
    scheduler = make_scheduler(db, runtime, tmp_path)
    assert run_all(scheduler) == [1]
    wait_for_agent_session_handle(db)
    deadline = time.monotonic() + 10
    while 'draft' not in (tmp_path / 'runs' / '1' / 'worktree' / 'solve.txt').read_text():
        assert time.monotonic() < deadline
        time.sleep(0.05)

    stop_campaign(db, campaign, 'plateau: 5 runs without a keep')
    run_all(scheduler)
    assert [kind for kind, _ in settled(db, 'agent_session.wrap_up')] == [1], 'told, but the turn goes on'
    timings(db, campaign, wrap_up_grace=0)
    run_all(scheduler)
    scheduler.wait(SETTLE)
    scheduler.stop(timeout=5)
    with db.session() as s:
        run = s.get_one(Run, 1)
        assert (run.status, run.end) == (RunStatus.FAILED, RunEnd.CAMPAIGN_STOPPED)
        [ended] = s.scalars(select(Event).where(Event.kind == 'agent_session.stop_ended')).all()
        assert (ended.target_id, ended.payload) == (
            1,
            AgentEnded(reason='plateau: 5 runs without a keep', end=RunEnd.CAMPAIGN_STOPPED),
        )
        made = s.scalars(select(Candidate).where(Candidate.run_id == 1).order_by(Candidate.id))
        assert [(c.origin, c.verdict) for c in made] == [(Origin.RUN, Outcome.KEEP), (Origin.RECOVERED, None)], (
            'its commit is scored, its draft recovered'
        )
        assert s.get_one(Idea, idea).status is IdeaStatus.OPEN, 'requeued from the recovered draft'
        assert runs(s, s.get_one(Campaign, campaign.id)) == 0, 'cut by the stop: no run limit counts it'


def test_a_stop_with_kill_ends_the_implementers_at_once_and_a_graceful_one_can_be_made_one(
    db: Database, campaign: Campaign, tmp_path: Path, mocker: MockerFixture
) -> None:
    """``piloop campaign stop --kill``: no wrap-up prompt and no grace. A sweep keeps its timeout."""
    runtime = PromptingRuntime()
    running_agent_session(db, campaign, 'in flight')
    lease_running(db)
    scheduler = make_scheduler(db, runtime, tmp_path)
    fake = prompted(db, scheduler, mocker)
    with db.session() as s:
        request_stop(s, s.get_one(Campaign, campaign.id), System.HUMAN, reason='machine needed', ending=Ending.WRAP_UP)
        s.commit()
    run_all(scheduler)  # stops the campaign; its implementers wind down from the next tick, as for a [stop] condition
    run_all(scheduler)
    run_all(scheduler)
    assert (len(fake.prompts), revoked(db)) == (1, []), 'graceful: told, and the grace runs'
    with db.session() as s:
        request_stop(s, s.get_one(Campaign, campaign.id), System.HUMAN, reason='now', ending=Ending.NOW)
        s.commit()
    run_all(scheduler)
    assert revoked(db) == ['implementer-1'], 'a stopped campaign can still be killed'
    run_all(scheduler)
    assert settled(db, 'agent_session.stop_ended') == [
        (1, AgentEnded(reason='stopped by human: machine needed', end=RunEnd.CAMPAIGN_STOPPED))
    ], 'one event, however many ticks the kill takes to land'
    scheduler.stop(timeout=5)


def test_a_cancelled_run_is_told_to_wrap_up_and_ended_after_the_grace(
    db: Database, campaign: Campaign, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker: MockerFixture
) -> None:
    runtime = PromptingRuntime()
    running_agent_session(db, campaign, 'redundant')
    running_agent_session(db, campaign, 'useful')
    with db.session() as s:
        sweep = AgentSession(
            kind=RESEARCH,
            campaign_id=campaign.id,
            runtime='subprocess',
            handle={'agent': 'research-3', 'pid': 4243},
            state=AgentSessionState.RUNNING,
            started_at=utcnow(),
        )
        s.add(sweep)
        s.flush()
        request_cancel(s, s.get_one(Run, 1), System.HUMAN, ending=Ending.WRAP_UP)
        request_cancel(s, sweep, System.ORCHESTRATOR, ending=Ending.WRAP_UP)
        s.commit()
    lease_running(db)
    scheduler = make_scheduler(db, runtime, tmp_path)
    fake = prompted(db, scheduler, mocker)
    run_all(scheduler)
    run_all(scheduler)
    assert [(agent, p.text) for agent, p in fake.prompts] == snapshot(
        [
            (
                1,
                '[piloop] Your run was cancelled (by human). Save what you have now, with a summary that says where it stands. Leave a note on what you did, then end your run (`piloop run end`). This session ends in 10 minutes.',
            ),
            (
                3,
                '[piloop] This sweep was cancelled (by orchestrator). Save what you have now, with a summary that says where it stands. Leave a note on what you did, then stop. This session ends in 10 minutes.',
            ),
        ]
    ), 'only the cancelled run and the cancelled sweep are told'
    run_all(scheduler)
    assert revoked(db) == []
    timings(db, campaign, wrap_up_grace=0)
    run_all(scheduler)
    assert revoked(db) == ['implementer-1', 'research-3']
    assert settled(db, 'agent_session.stop_ended') == [
        (1, AgentEnded(reason='cancelled by human', end=RunEnd.CANCELLED)),
        (3, AgentEnded(reason='cancelled by orchestrator', end=RunEnd.CANCELLED)),
    ]
    scheduler.stop(timeout=5)


def test_an_agent_of_no_campaign_winds_down_on_the_home_s_grace(
    db: Database, campaign: Campaign, tmp_path: Path, mocker: MockerFixture
) -> None:
    with db.session() as s:
        orphan = AgentSession(
            kind=RESEARCH,
            runtime='subprocess',
            handle={'agent': 'research-1', 'pid': 4243},
            state=AgentSessionState.RUNNING,
            started_at=utcnow(),
            reason='by hand',
        )
        s.add(orphan)
        s.flush()
        request_cancel(s, orphan, System.HUMAN, ending=Ending.WRAP_UP)
        s.commit()
    lease_running(db)
    controller = serving(RunController(db, Tracer(None), tmp_path, executor=Workers()))
    scheduler = Scheduler(db, controller, Tracer(None), tmp_path, agents=AgentSettings(wrap_up_grace=timedelta(0)))
    prompted(db, scheduler, mocker)
    run_all(scheduler)
    run_all(scheduler)
    assert revoked(db) == ['research-1'], 'told, then ended at once: the grace of the home is 0'
    scheduler.stop(timeout=5)


def test_a_cancelled_implementer_closes_its_run_as_cancelled_scores_its_commits_and_reopens_its_idea(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    """``piloop agent cancel``: the run became redundant, so its idea does not run again until it is set ready."""
    harness = '#!/bin/sh\necho step >> solve.txt\ngit commit -qam "step"\nexec sleep 30\n'
    write_toml(Path(campaign.path), harness, implementer_timeout=60)
    set_config(db, campaign)
    (idea,) = add_ideas(db, campaign, 'redundant')
    scheduler = make_scheduler(db, runtime, tmp_path)
    assert run_all(scheduler) == [1]
    wait_for_agent_session_handle(db)
    deadline = time.monotonic() + 10
    while not Repo(tmp_path / 'runs' / '1' / 'worktree').git.log('--oneline').count('step'):
        assert time.monotonic() < deadline
        time.sleep(0.05)
    with db.session() as s:
        request_cancel(s, s.get_one(AgentSession, 1), System.HUMAN, ending=Ending.NOW)
        s.commit()
    assert run_all(scheduler) == []
    scheduler.wait(SETTLE)
    scheduler.stop(timeout=5)
    with db.session() as s:
        run = s.get_one(Run, 1)
        assert (run.status, run.end) == (RunStatus.FAILED, RunEnd.CANCELLED)
        verdicts = [c.verdict for c in s.scalars(select(Candidate).where(Candidate.run_id == 1))]
        assert verdicts == [Outcome.KEEP], 'what it committed is scored'
        assert s.get_one(Idea, idea).status is IdeaStatus.OPEN
        [note] = s.scalars(select(Comment).where(Comment.target_kind == IDEA)).all()
        assert note.body == 'open: run#1 was cancelled by human; drop the idea to take it off the board'
        assert runs(s, s.get_one(Campaign, campaign.id)) == 0, 'not an attempt: no run limit counts it'


def test_a_run_end_asked_after_a_cancel_ended_the_implementer_is_refused_and_the_run_is_cancelled(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``piloop run end`` in flight when the daemon ends a cancelled implementer would settle the run as succeeded.

    The row is ``ending`` in a commit before the kill, and the end checks the row under the write lock, so the late end
    is refused, whenever it comes after the cancel, and the run closes as cancelled.
    """
    write_toml(Path(campaign.path), '#!/bin/sh\nexec sleep 30\n', implementer_timeout=60)
    set_config(db, campaign)
    add_ideas(db, campaign, 'redundant')
    scheduler = make_scheduler(db, runtime, tmp_path)
    assert run_all(scheduler) == [1]
    # the cancel frees run#1's slot, and a fresh run would take it as soon as run#1 closes: the count below is run#1's
    monkeypatch.setattr(OptimizationPlanner, 'queue', lambda _planner, _s, _campaign, _left: None)
    wait_for_agent_session_handle(db)
    with db.session() as s:
        request_cancel(s, s.get_one(Run, 1), System.HUMAN, ending=Ending.NOW)
        s.commit()
    run_all(scheduler)
    # the kill may land before the end: the implementer is ending or ended, or its run failed already
    late = (
        r'^(implementer#1 is (ending|killed|exited): the daemon ended it before its run ended|run#1 is failed already)$'
    )
    with db.session() as s, pytest.raises(ops.Refused, match=late):
        end_run(s, RunEnding.SUBMITTED, None, actor=Agent(role=IMPLEMENTER, session=1))
    scheduler.wait(SETTLE)
    scheduler.stop(timeout=5)
    with db.session() as s:
        run = s.get_one(Run, 1)
        assert (run.status, run.end) == (RunStatus.FAILED, RunEnd.CANCELLED)
        assert s.get_one(AgentSession, 1).state is AgentSessionState.KILLED


def test_an_implementer_cancelled_before_it_did_anything_is_not_a_failed_start(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An agent that the daemon ends on request within ``[agents] startup_seconds``, with no commit and no comment, did not
    fail to start: no ``anomaly.startup`` and no count toward the startup breaker. Its row is ``killed``."""
    write_toml(Path(campaign.path), '#!/bin/sh\nexec sleep 30\n', implementer_timeout=60)
    set_config(db, campaign)
    add_ideas(db, campaign, 'unwanted')
    scheduler = make_scheduler(db, runtime, tmp_path)
    assert run_all(scheduler) == [1]
    # the cancel frees run#1's slot, and a fresh run would take it as soon as run#1 closes: the count below is run#1's
    monkeypatch.setattr(OptimizationPlanner, 'queue', lambda _planner, _s, _campaign, _left: None)
    wait_for_agent_session_handle(db)
    with db.session() as s:
        request_cancel(s, s.get_one(Run, 1), System.HUMAN, ending=Ending.NOW)
        s.commit()
    assert run_all(scheduler) == []
    scheduler.wait(SETTLE)
    scheduler.stop(timeout=5)
    with db.session() as s:
        assert s.scalars(select(Event).where(has(StartupAnomaly))).all() == []
        assert s.get_one(AgentSession, 1).state is AgentSessionState.KILLED
        run = s.get_one(Run, 1)
        assert (run.status, run.end) == (RunStatus.FAILED, RunEnd.CANCELLED)
        assert s.scalar(select(func.count()).select_from(AgentSession)) == 1, 'not launched again'


def test_a_cancelled_request_never_launches_and_a_cancelled_sweep_is_ended(
    db: Database, campaign: Campaign, tmp_path: Path
) -> None:
    runtime = PromptingRuntime()
    (idea,) = add_ideas(db, campaign, 'queued')
    with db.session() as s:
        s.get_one(Idea, idea).status = IdeaStatus.CLAIMED
        run = Run(campaign_id=campaign.id, idea_id=idea, attempt=1, base_candidate_id=1, harness='script', model='m')
        s.add(run)
        s.flush()
        queued = AgentSession(
            kind=IMPLEMENTER, campaign_id=campaign.id, subject=TargetRef(RUN, run.id), harness='script', model='m'
        )
        sweep = AgentSession(
            kind=RESEARCH,
            campaign_id=campaign.id,
            runtime='subprocess',
            handle={'agent': 'research-2', 'pid': 4243},
            state=AgentSessionState.RUNNING,
        )
        requested = AgentSession(kind=RESEARCH, campaign_id=campaign.id, reason='low_water')
        s.add_all([queued, sweep, requested])
        s.flush()
        request_cancel(s, queued, System.HUMAN, ending=Ending.WRAP_UP)
        request_cancel(s, sweep, System.HUMAN, ending=Ending.NOW)
        request_cancel(s, requested, System.HUMAN, ending=Ending.NOW)
        s.commit()
    occupy_the_run_slot(db, campaign)  # the cancel frees run#1's slot, which no fresh run takes
    lease_running(db)
    scheduler = make_scheduler(db, runtime, tmp_path)
    assert run_all(scheduler) == [], 'the queued run is cancelled before it launches'
    assert revoked(db) == ['research-2']
    with db.session() as s:
        w = s.get_one(AgentSession, 1)
        assert (w.state, w.error, s.get_one(Run, 1).end) == (
            AgentSessionState.KILLED,
            'cancelled by human',
            RunEnd.CANCELLED,
        )
        assert (s.get_one(AgentSession, 3).state, s.get_one(AgentSession, 3).error) == (
            AgentSessionState.KILLED,
            'cancelled by human',
        )
        assert s.get_one(Idea, idea).status is IdeaStatus.OPEN, 'a cancel stops the run, not its idea'
    scheduler.stop(timeout=5)


def died_at_startup(db: Database, campaign: Campaign, kind: RoleName, minutes_ago: float = 0) -> AgentSessionId:
    """Add a killed agent row and the launcher's ``anomaly.startup`` event, ``minutes_ago`` minutes ago."""
    with db.session() as s:
        agent_session = AgentSession(
            kind=kind, campaign_id=campaign.id, harness='script', state=AgentSessionState.KILLED
        )
        s.add(agent_session)
        s.flush()
        payload = StartupAnomaly(
            role=kind,
            harness='script',
            subject=None,
            seconds=35.0,
            error=f'FileNotFoundError: {kind}-{agent_session.id}: no such harness',
        )
        s.add(
            Event(
                actor='daemon',
                target=TargetRef(TargetKind.AGENT_SESSION, agent_session.id),
                payload=payload,
                at=utcnow() - timedelta(minutes=minutes_ago),
            )
        )
        s.commit()
        return agent_session.id


def pause_escalations(db: Database) -> list[Comment]:
    with db.session() as s:
        return list(s.scalars(select(Comment).where(Comment.kind == 'escalation').order_by(Comment.id)))


def test_startup_deaths_pause_launches_and_escalate_until_they_age_out(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    """Three startup deaths within the window pause launches, with one escalation that says why.

    Without the pause, each death launches another agent on a host that is already overloaded. Launches resume when
    the deaths age out of the window.
    """
    died_at_startup(db, campaign, IMPLEMENTER, minutes_ago=11)
    for kind in (WATCHDOG, RESEARCH):
        died_at_startup(db, campaign, kind)
    wanted = request_watchdog(db, campaign)
    scheduler = make_scheduler(db, runtime, tmp_path)
    assert wanted in run_all(scheduler), 'two recent deaths: launches go on'
    scheduler.wait(SETTLE)

    died = [died_at_startup(db, campaign, WATCHDOG) for _ in range(2)]
    request_watchdog(db, campaign)
    assert run_all(scheduler) == []
    [escalation] = pause_escalations(db)
    assert (escalation.target_kind, escalation.author, escalation.priority) == (TargetKind.CAMPAIGN, 'daemon', 1)
    assert details(escalation) == IsStr(
        regex=r'Launches paused: 4 agents died at startup in the last 10 minutes\. Load average [\d.]+ / [\d.]+ / [\d.]+'
        rf' on \d+ CPUs when launches paused\..*\n- agent_session#\d+ \(research on script\): .*\n- agent_session#{died[0]} \(watchdog on script\):'
        rf' FileNotFoundError: watchdog-{died[0]}: no such harness\n'
        rf'- agent_session#{died[1]} .*',
        regex_flags=re.DOTALL,
    )
    assert run_all(scheduler) == [] and len(pause_escalations(db)) == 1, 'one escalation while the pause holds'

    with db.session() as s:
        s.get_one(Comment, escalation.id).acked_at = utcnow()
        s.commit()
    assert run_all(scheduler) == [] and len(pause_escalations(db)) == 1, 'acknowledged means seen: not posted again'

    latest = died_at_startup(db, campaign, RESEARCH)
    assert run_all(scheduler) == []
    [extended] = pause_escalations(db)
    assert extended.id == escalation.id and extended.acked_at is not None, 'the same pause: updated in place'
    assert details(extended).startswith(
        'Launches paused: 5 agents'
    ) and f'- agent_session#{latest} (research' in details(extended)

    with db.session() as s:
        for event in s.scalars(select(Event).where(has(StartupAnomaly))):
            event.at -= FewestLive().startup_window
        s.commit()
    assert run_all(scheduler) != [], 'the deaths aged out of the window: launches resume'
    scheduler.wait(SETTLE)

    for _ in range(FewestLive().startup_breaker):
        died_at_startup(db, campaign, WATCHDOG)
    request_watchdog(db, campaign)
    assert run_all(scheduler) == [] and len(pause_escalations(db)) == 2, 'a new pause: a new escalation'
    with db.session() as s:
        kinds = s.scalars(select(Event.kind).where(Event.kind.like('launch.%')).order_by(Event.id)).all()
    assert kinds == ['launch.paused', 'launch.resumed', 'launch.paused']


def test_the_triggers_that_read_no_time_ask_for_no_pass_of_their_own(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    """Only ``cron`` and the rest of ``low_water`` compare a time: the others wait for the campaign's next event."""
    scheduler = make_scheduler(db, runtime, tmp_path)
    with db.session() as s:
        p = s.get_one(Campaign, campaign.id)
        cfg = CampaignConfig.model_validate(p.config)
        on = campaigns_of(scheduler)._pass(s, p, cfg, RESEARCH, role_of(cfg, ResearchRole), utcnow(), left(s, p, cfg))
        for trigger in (
            StalledTrigger(),
            RunFailed(),
            DeadAtStartup(),
            StoppedTrigger(),
            SqlTrigger(when='SELECT 1'),
        ):
            assert trigger.due_in(on) is None, trigger


def test_an_implementer_is_told_once_to_wrap_up_before_its_timeout(
    db: Database, campaign: Campaign, tmp_path: Path, mocker: MockerFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a warning, the role timeout kills an implementer mid-probe, with its last step uncommitted.

    The wrap-up is a note on the agent, which its executor delivers like any other. The role timeout still ends it.
    """
    runtime = PromptingRuntime()
    running_agent_session(db, campaign, 'long')
    lease_running(db)
    scheduler = make_scheduler(db, runtime, tmp_path)
    fake = prompted(db, scheduler, mocker)
    with db.session() as s:
        timeout = s.get_one(Campaign, campaign.id).config.role(IMPLEMENTER).timeout
    lead = wrap_up_lead(timeout, ImplementerRole().wrap_up_lead)

    def started(ago: float) -> None:
        with db.session() as s:
            s.get_one(AgentSession, 1).started_at = utcnow() - timedelta(seconds=ago)
            s.commit()

    started(timeout - lead - 5)
    run_all(scheduler)
    run_all(scheduler)
    assert fake.prompts == [], 'not yet'
    started(timeout - lead)
    timings(db, campaign, wrap_up_grace=0)
    for _ in range(3):
        run_all(scheduler)
    [(agent, prompt)] = fake.prompts  # once
    assert agent == 1 and prompt.text == IsStr(
        regex=re.escape(
            f'[piloop] Your time is almost up (the role timeout of {timeout:g}s). Save what you have now, with a'
            ' summary that says where it stands. Leave a note on what you did, then end your run (`piloop run end`). This session'
            ' ends in'
        )
        + r' [\d.]+ minutes\.'  # the time left when the tick ran: a loaded host takes seconds of it
    )
    assert revoked(db) == [], 'the role timeout ends it, not the daemon'
    assert [kind for kind, _ in settled(db, 'agent_session.wrap_up')] == [1]
    scheduler.stop(timeout=5)


def test_an_extension_reaches_the_executor_once_for_each_total_and_an_implementer_told_to_wrap_up_is_told_again(
    db: Database, campaign: Campaign, tmp_path: Path, mocker: MockerFixture
) -> None:
    runtime = PromptingRuntime()
    running_agent_session(db, campaign, 'long')
    lease_running(db)
    scheduler = make_scheduler(db, runtime, tmp_path)
    fake = prompted(db, scheduler, mocker)
    extended = mocker.patch.object(scheduler._controller.executor, 'extend')
    with db.session() as s:
        timeout = s.get_one(Campaign, campaign.id).config.role(IMPLEMENTER).timeout
    lead = wrap_up_lead(timeout, ImplementerRole().wrap_up_lead)
    with db.session() as s:
        s.get_one(AgentSession, 1).started_at = utcnow() - timedelta(seconds=timeout - lead)
        s.commit()
    timings(db, campaign, wrap_up_grace=0)
    for _ in range(2):
        run_all(scheduler)
    [(_, first)] = fake.prompts
    assert first.text.startswith('[piloop] Your time is almost up (the role timeout of')

    def extend_by(hours: float) -> None:
        with db.session() as s:
            extend(s, s.get_one(AgentSession, 1), timedelta(hours=hours), System.ORCHESTRATOR)
            s.commit()

    extended.side_effect = [OperationalError('INSERT', {}, Exception('locked')), None]
    extend_by(1)
    extend_by(1)
    for _ in range(3):
        run_all(scheduler)
    assert extended.call_args_list == [call(AgentKey(AgentSessionId(1)), 7200)] * 2, (
        'the total that it has not handed over: again after the executor refused it, then never'
    )
    assert [p.text[:40] for _, p in fake.prompts[1:]] == ['[piloop] orchestrator gave you 1:00:00 m'] * 2
    with db.session() as s:
        s.get_one(AgentSession, 1).started_at = utcnow() - timedelta(seconds=timeout + 7200 - lead)
        s.commit()
    for _ in range(2):
        run_all(scheduler)
    assert fake.prompts[-1][1].text.startswith(
        f'[piloop] Your time is almost up (your timeout of {timeout + 7200:g}s, with its extensions)'
    ), 'told again at the new deadline'
    scheduler.stop(timeout=5)


def test_an_implementer_is_told_to_wrap_up_before_the_campaign_stops_when_that_comes_before_its_timeout(
    db: Database, campaign: Campaign, tmp_path: Path, mocker: MockerFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run that started minutes before a ``max_hours`` stop was cut by the stop, never told to wrap up."""
    set_config(db, campaign, plugins={'role': {'implementer': {'timeout': 3600}}, 'stop_rule': {'max_hours': 0.25}})
    runtime = PromptingRuntime()
    running_agent_session(db, campaign, 'late')
    lease_running(db)
    scheduler = make_scheduler(db, runtime, tmp_path)
    fake = prompted(db, scheduler, mocker)

    def started(minutes: float) -> None:
        with db.session() as s:
            at = utcnow() - timedelta(minutes=minutes)
            s.get_one(AgentSession, 1).started_at = s.get_one(Run, 1).started_at = at
            s.commit()

    started(10)  # the stop in 5 minutes: its timeout is 15 minutes, and the wrap-up comes 3.75 minutes before
    run_all(scheduler)
    run_all(scheduler)
    assert fake.prompts == [], 'not yet'
    started(12)
    timings(db, campaign, wrap_up_grace=0)
    for _ in range(2):  # the wrap-up is a note, which the next tick hands to the executor
        run_all(scheduler)
    [(agent, prompt)] = fake.prompts
    assert agent == 1 and prompt.text.startswith(
        "[piloop] Your time is almost up (the campaign's stop, 900s after you started). Save what you have now"
    )
    scheduler.stop(timeout=5)


def ended_by_its_worker(
    db: Database, agent_session_id: int, how: RunEnding = RunEnding.SUBMITTED, *, ago: timedelta = timedelta(0)
) -> None:
    """The worker of ``agent_session_id`` ends its run as ``how``, ``ago`` before now."""
    with db.session() as s:
        run = end_run(s, how, 'too slow', actor=Agent(role=IMPLEMENTER, session=agent_session_id))
        s.commit()
    with db.session() as s:
        [event] = s.scalars(select(Event).where(Event.target == TargetRef(RUN, run.id), has(RunEndRequested))).all()
        event.at = utcnow() - ago
        s.commit()


@pytest.mark.parametrize(('how', 'end'), [(RunEnding.SUBMITTED, RunEnd.FINISHED), (RunEnding.GAVE_UP, RunEnd.GAVE_UP)])
def test_an_implementer_is_ended_a_minute_after_it_ended_its_run(
    db: Database, campaign: Campaign, tmp_path: Path, how: RunEnding, end: RunEnd
) -> None:
    """A worker that ended its run is done with it. A job that it left running must not hold its slot.

    The daemon ends it ``end_grace`` after the end, with no prompt, so its last tool call returns first. The run
    closes as its end says (``run_outcome``).
    """
    runtime = PromptingRuntime()
    running_agent_session(db, campaign, 'build')
    lease_running(db)
    scheduler = make_scheduler(db, runtime, tmp_path)
    comment(db, 'note', 'idea', 1, 'done', author='implementer#1')
    run_all(scheduler)
    assert revoked(db) == [], 'a note ends nothing'
    ended_by_its_worker(db, 1, how)
    run_all(scheduler)
    assert revoked(db) == [], 'within the grace: its last tool call returns'
    with db.session() as s:
        [event] = s.scalars(select(Event).where(has(RunEndRequested))).all()
        event.at = utcnow() - timedelta(seconds=61)
        s.commit()
    run_all(scheduler)
    run_all(scheduler)
    assert (revoked(db), runtime.prompts) == (['implementer-1'], [])
    assert settled(db, 'agent_session.stop_ended') == [(1, AgentEnded(reason='ended its run', end=end))]
    scheduler.stop(timeout=5)


@pytest.mark.parametrize('closed', [AgentSessionState.EXITED, AgentSessionState.KILLED])
def test_a_wind_down_that_read_the_implementer_before_its_report_closed_the_row_leaves_it_closed(
    db: Database, campaign: Campaign, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, closed: AgentSessionState
) -> None:
    """The pass reads the implementer as running, then its report closes the row, then the pass ends it.

    Set ``ending`` again, the row would never close: no report is left to close it, and its run and saved work wait.
    """
    idea = running_agent_session(db, campaign, 'build')
    lease_running(db)
    scheduler = make_scheduler(db, PromptingRuntime(), tmp_path)
    ended_by_its_worker(db, idea, ago=timedelta(seconds=61))
    end = Agents._end

    def reported_first(agents: Agents, s: Session, agent_session: AgentSession, reason: str, how: RunEnd) -> None:
        with db.session() as other:
            row = other.get_one(AgentSession, agent_session.id)
            row.state, row.ended_at = closed, utcnow()
            other.commit()
        end(agents, s, agent_session, reason, how)

    monkeypatch.setattr(Agents, '_end', reported_first)
    run_all(scheduler)
    with db.session() as s:
        assert s.get_one(AgentSession, idea).state is closed
    assert settled(db, 'agent_session.stop_ended') == []
    scheduler.stop(timeout=5)


@pytest.mark.usefixtures('daemon_api')
def test_an_implementer_that_left_a_job_running_after_it_ended_its_run_succeeds_and_the_job_is_killed(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A headless agent whose background job outlives its turn does not exit: it held the slot to the role timeout."""
    note = (
        "INSERT INTO comments (target_kind, target_id, author, kind, body) VALUES ('idea', 1, 'implementer#1', 'note',"
        " 'done')"
    )
    harness = (
        f'#!/bin/sh\necho step >> solve.txt\ngit commit -qam "step"\n{PILOOP_HINT} sql "{note}"\n{END_RUN}'
        'sleep 300 &\necho $! > ../child.pid\nwait\n'
    )
    write_toml(Path(campaign.path), harness, implementer_timeout=300)
    set_config(db, campaign)
    (idea,) = add_ideas(db, campaign, 'long')
    with db.session() as s:
        stored = s.get_one(Campaign, campaign.id)
        stored.config = reconfigured(stored.config, plugins={'role': {'implementer': {'end_grace': 0}}})
        s.commit()
    scheduler = make_scheduler(db, runtime, tmp_path)
    assert run_all(scheduler) == [1]
    pid_file = tmp_path / 'runs' / '1' / 'child.pid'
    wait_until(lambda: pid_file.exists() and pid_file.read_text().strip())
    child = int(pid_file.read_text())
    start = proc.started(child)
    assert start is not None
    run_all(scheduler)
    scheduler.wait(20)
    with db.session() as s:
        run = s.get_one(Run, 1)
        assert (run.status, run.end) == (RunStatus.SUCCEEDED, RunEnd.FINISHED)
        assert s.get_one(Idea, idea).status is IdeaStatus.DONE
    assert not proc.alive(child, start), 'the job it left is killed with it'
    scheduler.stop(timeout=5)


def test_a_stopped_campaign_is_measured_again_only_on_request(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path, mocker: MockerFixture
) -> None:
    """Measuring a suspicious result again is part of the review after a campaign stops. The head and the bars that
    runs need are moot then: a new evaluator measures nothing unasked."""
    doubled = ['sh', '-c', reports(score='$(( 2 * $(wc -l < solve.txt) ))', size='1')]
    write_toml(Path(campaign.path), HARNESS_APPEND, evaluate=doubled)
    with db.session() as s:
        sync(s, s.get_one(Campaign, campaign.id))
        s.commit()
    stop_campaign(db, campaign, 'plateau: 5 runs without a keep')
    scheduler = make_scheduler(db, runtime, tmp_path)
    measured = mocker.patch.object(scheduler._controller, 'remeasure')
    run_all(scheduler)
    scheduler.wait(SETTLE)
    measured.assert_not_called()
    with db.session() as s:
        request_remeasure(s, s.get_one(Candidate, 1), System.HUMAN)
        s.commit()
    run_all(scheduler)
    scheduler.wait(SETTLE)
    measured.assert_called_once_with(1)


def test_one_daemon_measures_again_each_time_someone_asks(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path, mocker: MockerFixture
) -> None:
    """A campaign's re-measure slot frees when its re-measure ends, on the next pass, with no drain in between."""
    scheduler = make_scheduler(db, runtime, tmp_path)

    def answered(candidate_id: CandidateId) -> None:
        with db.session() as s:
            for asked, _ in pending(s, RemeasureRequested):
                answer(s, asked)
            s.commit()

    measured = mocker.patch.object(scheduler._controller, 'remeasure', side_effect=answered)
    for times in (1, 2):
        with db.session() as s:
            request_remeasure(s, s.get_one(Candidate, 1), System.HUMAN)
            s.commit()
        wait_until(lambda n=times: (run_all(scheduler), measured.call_count)[1] == n)
    scheduler.wait(SETTLE)
    assert measured.call_count == 2


def test_the_daemon_measures_a_candidate_again_when_someone_asks(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path, mocker: MockerFixture
) -> None:
    """A requested re-measure goes through the queue of re-measures, after the head and the bars that runs need."""
    scheduler = make_scheduler(db, runtime, tmp_path)
    measured = mocker.patch.object(scheduler._controller, 'remeasure')
    with db.session() as s:
        request_remeasure(s, s.get_one(Candidate, 1), System.ORCHESTRATOR)
        s.commit()
    run_all(scheduler)
    scheduler.wait(SETTLE)
    measured.assert_called_once_with(1)
