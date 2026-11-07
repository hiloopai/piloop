"""Each limit of the stop rule ends the campaign exactly as the configuration reference says. A stopped campaign requests nothing."""

import ast
import re
import sys
from datetime import timedelta
from importlib.metadata import EntryPoint
from pathlib import Path

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from piloop_best_cost import BestCost
from piloop_stop_after_bests import AfterBests
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from piloop.actor import Agent, System
from piloop.campaign import ops
from piloop.campaign.ops import Refused, pause
from piloop.campaign.stop import (
    CampaignClosed,
    CampaignReopened,
    StopRequested,
    acted,
    check as check_campaign,
    due,
    gather,
    hours,
    hours_due,
    request_stop,
    requested,
    seconds,
    timeout as agent_timeout,
)
from piloop.config import CampaignConfig, ConfigError, load_campaign_config
from piloop.db.events import emit, has, in_order
from piloop.db.models import AgentSession, AgentSessionState, Campaign, Comment, Event, Pause, utcnow
from piloop.db.requests import RequestAnswered, pending
from piloop.db.session import Database
from piloop.db.triggers.changelog import CampaignPausedChanged, CampaignStatusChanged
from piloop.ids import CampaignStatus, Ending, TargetKind, TargetRef
from piloop.otel import Tracer
from piloop.plugins import host
from piloop.plugins.campaign_type.kit.backlog.ids import IdeaStatus
from piloop.plugins.campaign_type.kit.backlog.tables import Idea
from piloop.plugins.campaign_type.kit.candidates.ids import CandidateId, Origin
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.evaluation.tables import Measurement
from piloop.plugins.campaign_type.kit.pool.ops import exclude
from piloop.plugins.campaign_type.kit.pool.pool import Pool
from piloop.plugins.campaign_type.kit.pool.scored import record_scored
from piloop.plugins.campaign_type.kit.runs.controller import RunFinished
from piloop.plugins.campaign_type.kit.runs.ids import IMPLEMENTER, RUN, RunEnd, RunId, RunStatus
from piloop.plugins.campaign_type.kit.runs.stop import runs, runs_without_best
from piloop.plugins.campaign_type.kit.runs.tables import Run
from piloop.plugins.escalation.contract import ESCALATION
from piloop.plugins.fact_source.contract import FactReadError
from piloop.plugins.fact_source.facts import (
    ATTEMPTS,
    CAMPAIGN,
    EVALUATIONS,
    FUNNEL,
    HOURS,
    METRIC_SUMS,
    NEW_BESTS,
    OPTIMIZATION,
    RUNS,
    TOKENS,
    UNSETTLED,
    Facts,
    FactValue,
    Funnel,
    per_best,
    per_evaluation,
    per_run,
    reading,
    since_best,
    worker_spend,
)
from piloop.plugins.fact_source.kit.readings import FactRead, Readings
from piloop.plugins.host import data_errors
from piloop.plugins.memory.local import LocalMemory
from piloop.plugins.memory.local.tables import Document
from piloop.plugins.metric import Direction
from piloop.plugins.stop_rule import limits as limits_module
from piloop.plugins.stop_rule.contract import GO, STOP_RULES, Escalate, Go, Hold, Shape, Stop, StopCase, StopRule
from piloop.plugins.stop_rule.limits import Limits
from piloop.reports.overview import progress
from tests.conftest import (
    HARNESS_APPEND,
    HARNESS_NOOP,
    HARNESS_WORSE,
    LEAD,
    SETTLE,
    ScriptRuntime,
    details,
    end_of,
    example,
    run_all,
    span_named,
    spent,
    write_toml,
)
from tests.daemon.test_roles import PLUGIN, installed
from tests.daemon.test_scheduler import add_ideas, make_scheduler, set_config, stop_campaign

LOWER_PRIMARY = [{'name': 'score', 'direction': 'higher'}, {'name': 'size', 'direction': 'lower', 'role': 'primary'}]


def configure(
    db: Database,
    campaign: Campaign,
    max_implementers: int = 1,
    harness: str = HARNESS_NOOP,
    roles: dict[str, object] | None = None,
    **stop: float,
) -> CampaignConfig:
    write_toml(Path(campaign.path), harness, max_implementers=max_implementers)
    with db.session() as s:
        cfg = load_campaign_config(Path(campaign.path), plugins={'role': roles or {}, 'stop_rule': stop})
        s.get_one(Campaign, campaign.id).config = cfg
        s.commit()
    return cfg


def add_run(
    db: Database,
    campaign: Campaign,
    *,
    status: RunStatus = RunStatus.SUCCEEDED,
    started_at: timedelta = timedelta(0),
    finished_at: timedelta = timedelta(0),
    keep_score: float | None = None,
    end: RunEnd | None = None,
    error: str | None = None,
    measured: bool = True,
) -> RunId:
    """Add a run, finished by default, with a candidate scored when the run finished: 0, or ``keep_score``.

    A score above the best's makes the candidate the new best (``new_bests``). With ``measured=False`` the run has no
    candidate. Add one with ``add_candidate``. A run in flight has none. ``started_at`` and ``finished_at`` are how
    long ago.
    """
    with db.session() as s:
        n = s.scalar(select(func.count(Idea.id))) or 0
        idea = Idea(
            track='score',
            campaign_id=campaign.id,
            title=f'idea {n}',
            hypothesis='t',
            created_by='t',
            status=IdeaStatus.DONE,
        )
        s.add(idea)
        s.flush()
        in_flight = status in (RunStatus.QUEUED, RunStatus.RUNNING)
        run = Run(
            campaign_id=idea.campaign_id,
            idea_id=idea.id,
            attempt=1,
            base_candidate_id=1,
            harness='h',
            model='m',
            status=status,
            end=end or end_of(status),
            error=error,
            started_at=utcnow() - started_at,
            finished_at=None if in_flight else utcnow() - finished_at,
        )
        s.add(run)
        s.commit()
    if keep_score is not None or (measured and not in_flight):
        candidate_id = add_candidate(db, campaign, run.id, Origin.RUN)
        with db.session() as s:
            row = s.get_one(Campaign, campaign.id)
            s.add(
                Measurement(
                    candidate_id=candidate_id, evaluator_id=row.evaluator_id, metrics={'score': keep_score or 0}
                )
            )
            s.flush()
            record_scored(s, Pool.of(s, row, row.config.all_tracks[0]), candidate_id)
            [scored] = [e for e in s.new if isinstance(e, Event)]
            scored.at = s.get_one(Run, run.id).finished_at
            s.commit()
    return run.id


def add_candidate(db: Database, campaign: Campaign, run_id: RunId, origin: Origin) -> CandidateId:
    """Add an unmeasured candidate of run ``run_id``, of ``origin``, a child of the baseline."""
    with db.session() as s:
        candidate = Candidate(
            campaign_id=campaign.id,
            run_id=run_id,
            parent_id=1,
            tree_sha=f'tree{run_id}',
            revision=f'commit{run_id}',
            origin=origin,
        )
        s.add(candidate)
        s.commit()
        return candidate.id


def reason(db: Database, campaign: Campaign) -> str | None:
    """Return why the stop rule stops the campaign now, or None when it goes on. These campaigns have no ``[spend]``."""
    with db.session() as s:
        row = s.get_one(Campaign, campaign.id)
        answer = check_campaign(s, row, CampaignConfig.model_validate(row.config))
    assert not isinstance(answer, Hold), 'nothing here holds launches'
    return answer.reason if isinstance(answer, Stop) else None


def progress_line(db: Database, campaign: Campaign, cfg: CampaignConfig) -> str:
    """Return the first part of the progress line: the runs against ``max_runs``."""
    with db.session() as s:
        return progress(s, s.get_one(Campaign, campaign.id), cfg, utcnow()).split(' · ')[0]


def campaign_state(db: Database, campaign: Campaign) -> tuple[CampaignStatus, str | None]:
    with db.session() as s:
        row = s.get_one(Campaign, campaign.id)
        return row.status, row.stopped_reason


def test_no_stop_table_means_open_ended(db: Database, campaign: Campaign) -> None:
    configure(db, campaign)
    add_run(db, campaign, keep_score=100.0, started_at=timedelta(days=30))
    for _ in range(3):
        add_run(db, campaign, status=RunStatus.FAILED)
    assert reason(db, campaign) is None


def test_target_on_the_best_in_the_metric_direction(db: Database, campaign: Campaign) -> None:
    configure(db, campaign, target=3.0)
    assert reason(db, campaign) is None, 'the baseline scores 1, and is the best'
    add_run(db, campaign, keep_score=2.9)
    assert reason(db, campaign) is None
    add_run(db, campaign, keep_score=3.0)
    assert reason(db, campaign) == 'target: score 3 ≥ 3'
    with db.session() as s:
        exclude(s, CandidateId(3), 'gamed the evaluation', actor=System.HUMAN)
        s.commit()
    assert reason(db, campaign) is None, 'an excluded candidate is never the best'
    with db.session() as s:
        c = s.get_one(Candidate, 3)
        c.excluded_reason, c.excluded_by, c.origin = None, None, Origin.SKIPPED
        s.commit()
    assert reason(db, campaign) is None, 'a skipped candidate is not in the pool'
    with db.session() as s:
        c = s.get_one(Candidate, 3)
        c.origin = Origin.RUN
        for m in s.scalars(select(Measurement).where(Measurement.candidate_id == 3)):
            m.metrics = {'score': 3.4}
        s.commit()
    assert reason(db, campaign) == 'target: score 3.4 ≥ 3', 'the best is a query of the measurements'


def test_target_lower_is_better(db: Database, campaign: Campaign) -> None:
    with db.session() as s:
        cfg = load_campaign_config(Path(campaign.path), metrics=LOWER_PRIMARY, plugins={'stop_rule': {'target': 5}})
        s.get_one(Campaign, campaign.id).config = cfg
        s.commit()
    assert reason(db, campaign) == 'target: size 5 ≤ 5', "the baseline's solve.txt is five bytes"


def test_a_campaign_that_max_runs_stopped_shows_all_its_runs_in_the_progress_line(
    db: Database, campaign: Campaign
) -> None:
    """The progress line counts the runs as ``max_runs`` does, and a stop never ends a run that ``max_runs`` counts."""
    cfg = configure(db, campaign, max_runs=2)
    add_run(db, campaign)
    in_flight = add_run(db, campaign, status=RunStatus.RUNNING)
    assert (reason(db, campaign), progress_line(db, campaign, cfg)) == (None, 'run 2/2'), (
        'the run in flight counts, but it can still end without a scored candidate, so the campaign goes on'
    )
    with db.session() as s:
        run = s.get_one(Run, in_flight)
        run.status, run.end = RunStatus.FAILED, RunEnd.GAVE_UP
        run.finished_at = utcnow()
        s.commit()
    assert (reason(db, campaign), progress_line(db, campaign, cfg)) == (None, 'run 1/2'), 'a run without a commit'
    add_run(db, campaign)
    assert (reason(db, campaign), progress_line(db, campaign, cfg)) == ('max_runs: 2 runs', 'run 2/2')


def test_max_runs_waits_for_a_finished_run_whose_commits_are_scored(db: Database, campaign: Campaign) -> None:
    """While the commits of a finished run are scored (``runs.scoring``), the run counts but cannot stop the campaign.

    Its idea's status does not matter: a human may park or reject it meanwhile.
    """
    configure(db, campaign, max_runs=1)
    run_id = add_run(db, campaign, measured=False)

    def scoring(on: bool, idea: IdeaStatus) -> tuple[str | None, int]:
        with db.session() as s:
            run = s.get_one(Run, run_id)
            assert run.idea is not None
            run.scoring, run.idea.status = on, idea
            s.commit()
            return reason(db, campaign), runs(s, s.get_one(Campaign, campaign.id))

    assert scoring(True, IdeaStatus.CLAIMED) == (None, 1), 'its candidates do not exist yet, but it keeps its place'
    assert scoring(True, IdeaStatus.DROPPED) == (None, 1), 'parked meanwhile: it still keeps its place'
    add_candidate(db, campaign, run_id, Origin.SKIPPED)
    assert scoring(False, IdeaStatus.DONE) == (None, 0), 'only skip commits: it does not count, and another run starts'
    assert scoring(False, IdeaStatus.CLAIMED) == (None, 0), 'scored: the status of its idea does not matter'


def test_max_runs_caps_the_runs_queued_in_one_tick(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    configure(db, campaign, max_implementers=4, max_runs=1)
    add_ideas(db, campaign, 'a', 'b', 'c', 'd')
    sched = make_scheduler(db, runtime, tmp_path)
    assert run_all(sched) == [1], 'four free slots, but max_runs leaves room for one run'
    sched.wait(SETTLE)
    with db.session() as s:
        assert s.scalars(select(Run.id)).all() == [1]
    sched.stop(timeout=5)


def test_max_runs_leaves_room_for_the_remaining_runs_only(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    configure(db, campaign, max_implementers=4, max_runs=3)
    add_run(db, campaign)
    add_run(db, campaign)
    add_ideas(db, campaign, 'a', 'b')
    sched = make_scheduler(db, runtime, tmp_path)
    assert run_all(sched) == [1], 'two runs already: max_runs 3 leaves room for one more'
    sched.wait(SETTLE)
    with db.session() as s:
        assert s.scalar(select(func.count(Run.id))) == 3
    sched.stop(timeout=5)


def test_max_hours_since_the_first_run_started(db: Database, campaign: Campaign) -> None:
    configure(db, campaign, max_hours=12)
    assert reason(db, campaign) is None, 'the clock starts with the first run'
    add_run(db, campaign, started_at=timedelta(hours=11, minutes=59))
    assert reason(db, campaign) is None
    add_run(db, campaign, started_at=timedelta(hours=12, minutes=30))
    assert reason(db, campaign) == 'max_hours: 12.5h since the first run'


def test_the_pass_comes_back_when_the_hours_that_the_rule_leaves_run_out(db: Database, campaign: Campaign) -> None:
    """``hours`` is the one fact that time alone uses up: the pass returns when the rule's ``left`` of it runs out."""
    configure(db, campaign, max_hours=12)
    now = utcnow()

    def due(row: Campaign) -> timedelta | None:
        at = hours_due(row, check_campaign(s, row, row.config), now)
        return None if at is None else at - now

    with db.session() as s:
        row = s.get_one(Campaign, campaign.id)
        assert due(row) == timedelta(hours=12), 'no run yet: the whole limit is left'
    add_run(db, campaign, started_at=timedelta(hours=11))
    with db.session() as s:
        row = s.get_one(Campaign, campaign.id)
        left = due(row)
        assert left is not None and abs(left - timedelta(hours=1)) < timedelta(minutes=1)
        assert hours_due(row, Go(), now) is None, 'an answer that leaves no hours: no clock'
        assert hours_due(row, Hold('spend not known'), now) is None, 'a held campaign uses up no hours'
        row.paused, row.paused_reason, row.paused_by = Pause.ALL, 'night', 'human'
        assert due(row) is None, 'paused time does not count'


def test_the_time_a_campaign_was_paused_does_not_count_toward_max_hours(db: Database, campaign: Campaign) -> None:
    configure(db, campaign, max_hours=12)
    add_run(db, campaign, started_at=timedelta(hours=12, minutes=30))
    assert reason(db, campaign) == 'max_hours: 12.5h since the first run'
    now = utcnow()

    def paused(hours_ago: float, new: Pause | None) -> None:
        with db.session() as s:
            payload = CampaignPausedChanged(old=None, new=new, paused_reason=None)
            s.add(
                Event(
                    actor='t',
                    target=TargetRef(TargetKind.CAMPAIGN, campaign.id),
                    payload=payload,
                    at=now - timedelta(hours=hours_ago),
                )
            )
            s.commit()

    paused(14, Pause.LAUNCHES)  # before the first run: only its last half hour counts
    paused(12, None)
    paused(9, Pause.ALL)
    paused(8.9, Pause.LAUNCHES)  # a change of what it holds goes on with the same pause
    paused(8.75, None)

    def elapsed() -> float | None:
        with db.session() as s:
            value = hours(s, s.get_one(Campaign, campaign.id), now)
            return None if value is None else round(value, 2)

    assert (elapsed(), reason(db, campaign)) == (11.75, None), '12.5h less half an hour and a quarter paused'
    paused(1, Pause.LAUNCHES)  # still paused: up to now
    assert elapsed() == 10.75


def test_max_tokens_sums_the_usage_of_every_ended_agent_of_the_campaign(db: Database, campaign: Campaign) -> None:
    configure(db, campaign, max_tokens=100_000)
    assert reason(db, campaign) is None, 'nothing spent yet'
    with db.session() as s:
        other = Campaign(name='other', path='/other', config=campaign.config)
        s.add(other)
        s.flush()
        ended = dict(state=AgentSessionState.EXITED, ended_at=utcnow())
        s.add_all(
            [
                AgentSession(kind=IMPLEMENTER, campaign_id=campaign.id, usage=spent(60_000, 9), **ended),
                AgentSession(kind=LEAD, campaign_id=campaign.id, usage=spent(30_000, 2), **ended),
                AgentSession(kind=LEAD, campaign_id=campaign.id, usage=None, **ended),
                AgentSession(kind=IMPLEMENTER, campaign_id=other.id, usage=spent(50_000, 1), **ended),
            ]
        )
        s.commit()
    assert reason(db, campaign) is None, "90k: every role counts, another campaign's agents do not, unknown usage is 0"
    with db.session() as s:
        s.add(AgentSession(kind=LEAD, campaign_id=campaign.id, usage=spent(10_000, 1), **ended))
        s.commit()
    assert reason(db, campaign) == 'max_tokens: 100000 tokens spent'


def test_plateau_counts_finished_runs_since_the_last_new_best(db: Database, campaign: Campaign) -> None:
    configure(db, campaign, plateau=2)
    add_run(db, campaign, status=RunStatus.FAILED)
    add_run(db, campaign, status=RunStatus.RUNNING)
    assert reason(db, campaign) is None, 'a run in flight has not failed to find a new best yet'
    add_run(db, campaign, status=RunStatus.TIMED_OUT)
    assert reason(db, campaign) == 'plateau: 2 runs without a new best'
    add_run(db, campaign, keep_score=5.0)
    assert reason(db, campaign) is None, 'a new best resets the count'
    add_run(db, campaign, status=RunStatus.SUCCEEDED)
    assert reason(db, campaign) is None
    add_run(db, campaign, status=RunStatus.SUCCEEDED)
    assert reason(db, campaign) == 'plateau: 2 runs without a new best'
    with db.session() as s:
        s.add(Candidate(campaign_id=campaign.id, tree_sha='imported', revision='c', origin=Origin.REFERENCE))
        s.commit()
    assert reason(db, campaign) == 'plateau: 2 runs without a new best', 'an imported reference resets nothing'


def test_only_runs_that_produced_a_measured_candidate_count_toward_max_runs(db: Database, campaign: Campaign) -> None:
    """A run with no commit, or with only ``Piloop-Eval: skip`` commits, does not count toward ``max_runs``.

    A run in flight counts toward ``max_runs``, so the queue never starts more runs than ``max_runs`` allows. A run
    whose candidate waits for its measurement also counts.
    """
    configure(db, campaign, max_runs=3)
    add_run(db, campaign, status=RunStatus.FAILED, end=RunEnd.GAVE_UP, measured=False)
    add_candidate(db, campaign, add_run(db, campaign, measured=False), Origin.SKIPPED)
    with db.session() as s:
        row = s.get_one(Campaign, campaign.id)
        assert (runs(s, row), runs_without_best(s, row)) == (0, 2), 'plateau counts them'
    add_run(db, campaign)
    assert reason(db, campaign) is None, 'one measured run'
    add_candidate(db, campaign, add_run(db, campaign, measured=False), Origin.RUN)
    assert reason(db, campaign) is None, 'two'
    add_run(db, campaign, status=RunStatus.RUNNING)
    with db.session() as s:
        assert runs(s, s.get_one(Campaign, campaign.id)) == 3, 'a run in flight counts toward max_runs'


def test_plateau_counts_every_finished_attempt_also_one_that_never_reached_the_evaluator(
    db: Database, campaign: Campaign
) -> None:
    """A run without a new best counts toward ``plateau``, whether it scored or not: a campaign whose runs never submit
    plateaus too. ``max_runs`` counts only the runs that reached the evaluator.
    """
    configure(db, campaign, plateau=4)
    add_run(db, campaign, keep_score=5.0)
    add_run(db, campaign, status=RunStatus.FAILED, end=RunEnd.GAVE_UP, measured=False)
    add_candidate(db, campaign, add_run(db, campaign, measured=False), Origin.SKIPPED)
    add_run(db, campaign, status=RunStatus.SUCCEEDED, measured=False)
    assert reason(db, campaign) is None, 'three runs without a new best'
    add_run(db, campaign, status=RunStatus.FAILED, error='agent exit 1', measured=False)
    assert reason(db, campaign) == 'plateau: 4 runs without a new best', 'none of them scored'


def test_a_run_whose_worker_gave_up_is_an_attempt_for_plateau_and_holds_no_place_under_max_runs(
    db: Database, campaign: Campaign
) -> None:
    """Giving up tried the idea: ``plateau`` counts it, as any attempt. With nothing measured, ``max_runs`` does not."""
    configure(db, campaign, plateau=2)
    add_run(db, campaign, keep_score=5.0)
    add_run(db, campaign, status=RunStatus.FAILED, end=RunEnd.GAVE_UP, error='the window does not fit', measured=False)
    assert reason(db, campaign) is None, 'one attempt without a new best'
    add_run(db, campaign, status=RunStatus.FAILED, end=RunEnd.GAVE_UP, error='no speedup', measured=False)
    assert reason(db, campaign) == 'plateau: 2 runs without a new best'
    with db.session() as s:
        assert runs(s, s.get_one(Campaign, campaign.id)) == 1, 'only the measured run holds a place under max_runs'


def test_a_reopen_starts_a_fresh_plateau_window_and_an_update_that_sets_the_campaign_active_does_not(
    db: Database, campaign: Campaign
) -> None:
    """``plateau`` counts the runs that finished after the later of the last new best and the latest reopen.

    A reopen (``CampaignReopened``) is the one start that every limit counts from: ``max_runs``, ``max_hours`` and
    ``max_tokens`` too. An UPDATE that sets the campaign active is no reopen, so it stops again at once. An evaluator
    change alone resets nothing: the campaign still has made no progress.
    """
    configure(db, campaign, plateau=2)
    add_run(db, campaign, keep_score=5.0, finished_at=timedelta(minutes=11))
    add_run(db, campaign, finished_at=timedelta(minutes=10))
    add_run(db, campaign, finished_at=timedelta(minutes=9))
    assert reason(db, campaign) == 'plateau: 2 runs without a new best'
    stop_campaign(db, campaign, 'plateau: 2 runs without a new best')
    with db.session() as s:
        row = s.get_one(Campaign, campaign.id)
        row.status, row.stopped_reason = CampaignStatus.ACTIVE, None
        s.commit()
    assert reason(db, campaign) == 'plateau: 2 runs without a new best', 'an UPDATE is no reopen'
    stop_campaign(db, campaign, 'plateau: 2 runs without a new best')
    with db.session() as s:
        emit(s, CampaignClosed(), TargetRef(TargetKind.CAMPAIGN, campaign.id), System.DAEMON)
        s.commit()
        ops.reopen(s, campaign.id, System.HUMAN, reason='one more idea')
        s.commit()
    assert reason(db, campaign) is None, 'the runs before the reopen are not counted'
    add_run(db, campaign)
    assert reason(db, campaign) is None
    add_run(db, campaign)
    assert reason(db, campaign) == 'plateau: 2 runs without a new best'


def test_runs_that_the_daemon_stopped_count_toward_no_budget(db: Database, campaign: Campaign) -> None:
    configure(db, campaign, plateau=1, max_runs=2)
    for end in (RunEnd.DAEMON_STOPPED, RunEnd.STALE, RunEnd.CAMPAIGN_STOPPED):
        add_run(db, campaign, status=RunStatus.FAILED, end=end)
    assert reason(db, campaign) is None, 'a stop, a crash or a campaign stop is not an attempt'
    add_run(db, campaign, status=RunStatus.FAILED, error='evaluate timed out')
    assert reason(db, campaign) == 'plateau: 1 runs without a new best'
    add_run(db, campaign)
    assert reason(db, campaign) == 'max_runs: 2 runs'


def test_plateau_counts_in_completion_order(db: Database, campaign: Campaign) -> None:
    configure(db, campaign, plateau=1)
    add_run(db, campaign)
    add_run(db, campaign, keep_score=5.0, finished_at=timedelta(hours=1))
    assert reason(db, campaign) == 'plateau: 1 runs without a new best', (
        'the earlier-created run finished after the new best, so it counts'
    )


def test_plateau_ignores_runs_that_finished_before_a_later_new_best(db: Database, campaign: Campaign) -> None:
    configure(db, campaign, plateau=1)
    add_run(db, campaign, keep_score=5.0)
    add_run(db, campaign, finished_at=timedelta(hours=1))
    assert reason(db, campaign) is None, 'the later-created run finished before the new best, so it does not count'


def test_stopping_drains_the_requests_and_queued_runs_pending_at_that_moment(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    configure(db, campaign, plateau=1)
    add_run(db, campaign)
    (idea,) = add_ideas(db, campaign, 'queued before the stop')
    with db.session() as s:
        s.get_one(Idea, idea).status = IdeaStatus.CLAIMED
        run = Run(campaign_id=campaign.id, idea_id=idea, attempt=1, base_candidate_id=1, harness='script', model='m')
        s.add(run)
        s.flush()
        s.add(
            AgentSession(
                kind=IMPLEMENTER, campaign_id=campaign.id, subject=TargetRef(RUN, run.id), harness='script', model='m'
            )
        )
        s.add(AgentSession(kind=LEAD, campaign_id=campaign.id, reason='requested before the stop'))
        s.commit()
    sched = make_scheduler(db, runtime, tmp_path)

    assert run_all(sched) == [], 'nothing launches'
    with db.session() as s:
        assert [(w.kind, w.state, w.error, w.ended_at is not None) for w in s.scalars(select(AgentSession))] == [
            (IMPLEMENTER, AgentSessionState.KILLED, RunEnd.CAMPAIGN_STOPPED, True),
            (LEAD, AgentSessionState.KILLED, RunEnd.CAMPAIGN_STOPPED, True),
        ]
        run = s.get_one(Run, 2)
        assert (run.status, run.end, run.finished_at is not None) == (RunStatus.FAILED, RunEnd.CAMPAIGN_STOPPED, True)
        assert s.get_one(Idea, idea).status is IdeaStatus.OPEN
        [(event, finished)] = in_order(s, RunFinished)
        assert (event.target_id, finished) == (2, RunFinished(end=RunEnd.CAMPAIGN_STOPPED, error=None))
        s.add(AgentSession(kind=LEAD, campaign_id=campaign.id, reason='by hand, after the stop'))
        s.commit()
    assert run_all(sched) == [3], 'a request made after the stop is explicit and launches'
    sched.wait(SETTLE)
    sched.stop(timeout=5)
    with db.session() as s:
        assert s.get_one(AgentSession, 3).state is AgentSessionState.EXITED


def test_a_requested_stop_ends_the_campaign_as_a_stop_condition_does(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    """An orchestrator that set ``campaigns.status`` by hand got no close and no drain.
    ``piloop campaign stop`` records the request, and the daemon stops the campaign on the [stop] path."""
    configure(db, campaign)
    add_run(db, campaign)
    (idea,) = add_ideas(db, campaign, 'queued before the stop')
    with db.session() as s:
        s.get_one(Idea, idea).status = IdeaStatus.CLAIMED
        run = Run(campaign_id=campaign.id, idea_id=idea, attempt=1, base_candidate_id=1, harness='script', model='m')
        s.add(run)
        s.flush()
        s.add(
            AgentSession(
                kind=IMPLEMENTER, campaign_id=campaign.id, subject=TargetRef(RUN, run.id), harness='script', model='m'
            )
        )
        request_stop(s, s.get_one(Campaign, campaign.id), System.HUMAN, reason='machine needed', ending=Ending.WRAP_UP)
        s.commit()
    sched = make_scheduler(db, runtime, tmp_path)
    assert run_all(sched) == [], 'nothing launches'
    assert campaign_state(db, campaign) == (CampaignStatus.STOPPED, 'stopped by human: machine needed')
    with db.session() as s:
        assert (s.get_one(Run, 2).end, s.get_one(Idea, idea).status) == (RunEnd.CAMPAIGN_STOPPED, IdeaStatus.OPEN)
        row = s.get_one(Campaign, campaign.id)
        row.status, row.stopped_reason = CampaignStatus.ACTIVE, None
        s.commit()
    assert run_all(sched) == [2], 'reactivated: the old request stops nothing, and the queued idea runs'
    sched.wait(SETTLE)
    sched.stop(timeout=5)
    assert campaign_state(db, campaign)[0] is CampaignStatus.ACTIVE
    with db.session() as s:
        request_stop(
            s, s.get_one(Campaign, campaign.id), Agent(role='implementer', session=3), reason='', ending=Ending.NOW
        )
        s.commit()
        assert requested(s, s.get_one(Campaign, campaign.id)) == (Stop('stopped by implementer#3'))
        assert [(e.actor, stop) for e, stop in pending(s, StopRequested)] == [
            ('implementer#3', StopRequested(reason='', ending=Ending.NOW))
        ], 'the one pending ask: the daemon answered the first when it stopped the campaign'
        (first, _), _ = in_order(s, StopRequested)
        assert [answered.ask for _, answered in in_order(s, RequestAnswered)] == [first.id]


def stop_while_held(db: Database, campaign: Campaign, hold: str) -> int:
    """Queue a run, hold the campaign's launches (an unknown spend, or a pause), and request a stop. Return the idea.

    The spend command reads ``bill`` next to the campaign, which does not exist yet.
    """
    configure(db, campaign)
    if hold == 'spend':
        bill = str(Path(campaign.path).parent / 'bill')
        rule = {'hold_at': {'spend': 10}}
        set_config(
            db,
            campaign,
            plugins={
                'stop_rule': rule,
                'fact_source': with_spend(command=['sh', '-c', f'cat {bill} >> "$PILOOP_RESULT_FILE"']),
            },
        )
    (idea,) = add_ideas(db, campaign, 'queued before the stop')
    with db.session() as s:
        s.get_one(Idea, idea).status = IdeaStatus.CLAIMED
        s.add(Run(campaign_id=campaign.id, idea_id=idea, attempt=1, base_candidate_id=1, harness='script', model='m'))
        if hold == 'pause':
            pause(s, campaign.id, System.HUMAN, evals=False, reason='too expensive')
        request_stop(s, s.get_one(Campaign, campaign.id), System.HUMAN, reason='over budget', ending=Ending.WRAP_UP)
        s.commit()
    return idea


def due_now(db: Database, campaign: Campaign) -> None:
    """Make the next spend reading of ``campaign`` due: its readings are an hour old."""
    with db.session() as s:
        readings = select(Event.id).where(has(FactRead), Event.target == TargetRef(TargetKind.CAMPAIGN, campaign.id))
        s.execute(update(Event).where(Event.id.in_(readings)).values(at=utcnow() - timedelta(hours=1)))
        s.commit()


def sweeps(db: Database, campaign: Campaign) -> list[AgentSessionState]:
    with db.session() as s:
        rows = s.scalars(select(AgentSession).where(AgentSession.campaign_id == campaign.id, AgentSession.kind == LEAD))
        return [w.state for w in rows]


def closes(db: Database, campaign: Campaign) -> int:
    """How many times the daemon closed ``campaign`` (``CampaignClosed``)."""
    with db.session() as s:
        return len(in_order(s, CampaignClosed, Event.target == TargetRef(TargetKind.CAMPAIGN, campaign.id)))


@pytest.mark.parametrize('hold', ['spend', 'pause'])
def test_a_requested_stop_ends_a_held_campaign_and_closes_it(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path, hold: str
) -> None:
    """Someone stops a campaign because it spends, so a stop works while the spend holds launches, or a pause does.

    The queued run is dropped, and the daemon closes the campaign: no review starts.
    """
    idea = stop_while_held(db, campaign, hold)
    sched = make_scheduler(db, runtime, tmp_path)
    assert run_all(sched) == [], 'nothing launches'
    assert campaign_state(db, campaign) == (CampaignStatus.STOPPED, 'stopped by human: over budget')
    assert (sweeps(db, campaign), closes(db, campaign)) == ([], 1)
    with db.session() as s:
        assert (s.get_one(Run, 1).end, s.get_one(Idea, idea).status) == (RunEnd.CAMPAIGN_STOPPED, IdeaStatus.OPEN)
        reads = len(in_order(s, FactRead))
    due_now(db, campaign)
    run_all(sched)
    with db.session() as s:
        assert len(in_order(s, FactRead)) == reads, 'the spend of a stopped campaign is not read'
    sched.stop(timeout=5)


def test_the_close_waits_until_the_stopped_campaigns_runs_are_scored_and_comes_once_per_stop(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    """The wrap-up's last commits are scored first, so the close sees the final best.

    The stop is on the feed, so a daemon that restarts in between still closes the campaign, once.
    """
    configure(db, campaign)
    running = add_run(db, campaign, status=RunStatus.RUNNING, measured=False)
    with db.session() as s:
        request_stop(s, s.get_one(Campaign, campaign.id), System.HUMAN, reason='done', ending=Ending.WRAP_UP)
        s.commit()
    sched = make_scheduler(db, runtime, tmp_path)
    assert run_all(sched) == []
    assert campaign_state(db, campaign) == (CampaignStatus.STOPPED, 'stopped by human: done')
    assert closes(db, campaign) == 0, 'its run is still open'
    with db.session() as s:
        run = s.get_one(Run, running)
        run.status, run.end, run.finished_at, run.scoring = RunStatus.SUCCEEDED, RunEnd.FINISHED, utcnow(), True
        s.commit()
    run_all(sched)
    assert closes(db, campaign) == 0, 'its commits are still scored'
    sched.stop(timeout=5)
    with db.session() as s:
        s.get_one(Run, running).scoring = False
        s.commit()
    restarted = make_scheduler(db, runtime, tmp_path)
    run_all(restarted)
    assert closes(db, campaign) == 1, 'settled: the daemon closes it'
    run_all(restarted)
    assert closes(db, campaign) == 1, 'once'
    with db.session() as s:
        row = s.get_one(Campaign, campaign.id)
        row.status, row.stopped_reason = CampaignStatus.ACTIVE, None
        request_stop(s, s.get_one(Campaign, campaign.id), System.HUMAN, reason='again', ending=Ending.WRAP_UP)
        s.commit()
    run_all(restarted)
    run_all(restarted)
    assert (closes(db, campaign), sweeps(db, campaign)) == (2, []), (
        'stopped again: closed again, and nothing reviews it'
    )
    restarted.stop(timeout=5)


def test_a_stopped_campaign_gets_no_trigger_requests(
    db: Database,
    campaign: Campaign,
    runtime: ScriptRuntime,
    tmp_path: Path,
    tracer: Tracer,
    spans: InMemorySpanExporter,
) -> None:
    configure(db, campaign, harness=HARNESS_WORSE, max_runs=1)
    add_ideas(db, campaign, 'a', 'b')
    sched = make_scheduler(db, runtime, tmp_path, tracer)
    assert run_all(sched) == [1], 'below max_runs: the queue trigger creates run 1, which commits a measured discard'
    sched.wait(SETTLE)
    assert campaign_state(db, campaign) == (CampaignStatus.ACTIVE, None)

    assert run_all(sched) == [], 'nothing launches on the tick that stops the campaign'
    assert campaign_state(db, campaign) == (CampaignStatus.STOPPED, 'max_runs: 1 runs')
    run_all(sched)
    with db.session() as s:
        assert [
            (w.kind, w.reason)
            for w in s.scalars(select(AgentSession).where(AgentSession.id > 1).order_by(AgentSession.id))
        ] == [], 'no implementer or sweeper request follows'
        assert s.scalars(select(Run)).all() and s.get_one(Idea, 2).status is IdeaStatus.OPEN, 'idea b never ran'
        [(event, change)] = in_order(s, CampaignStatusChanged)
        assert (event.actor, event.target_id, change) == (
            'daemon',
            campaign.id,
            CampaignStatusChanged(
                old=CampaignStatus.ACTIVE, new=CampaignStatus.STOPPED, stopped_reason='max_runs: 1 runs'
            ),
        ), 'the change log trigger records the stop, reason included; the daemon emits nothing by hand'
    assert dict(span_named(spans, 'campaign.stopped').attributes or {}) == {
        'campaign': 'demo',
        'reason': 'max_runs: 1 runs',
    }

    with db.session() as s:
        s.add(AgentSession(kind=LEAD, campaign_id=campaign.id, reason='by hand'))
        s.commit()
    assert run_all(sched) == [2], 'a hand-inserted request is explicit and launches'
    sched.wait(SETTLE)
    sched.stop(timeout=5)
    with db.session() as s:
        assert s.get_one(AgentSession, 2).state is AgentSessionState.EXITED


def test_a_stop_syncs_the_memory_documents_when_it_closes_the_campaign(
    db: Database,
    campaign: Campaign,
    runtime: ScriptRuntime,
    tmp_path: Path,
    tracer: Tracer,
    spans: InMemorySpanExporter,
) -> None:
    write_toml(Path(campaign.path), HARNESS_NOOP)
    with db.session() as s:
        cfg = load_campaign_config(
            Path(campaign.path), plugins={'stop_rule': {'max_runs': 1}, 'memory': {'documents': ['docs/*.md']}}
        )
        s.get_one(Campaign, campaign.id).config = cfg
        s.commit()
    add_run(db, campaign)
    Path(campaign.path, 'docs').mkdir()
    Path(campaign.path, 'docs', 'report.md').write_text('# Closing report\n')
    sched = make_scheduler(db, runtime, tmp_path, tracer)
    assert run_all(sched) == []
    sched.stop(timeout=5)
    with db.session() as s:
        [document] = s.scalars(select(Document)).all()
        assert (document.path, document.title, document.created_by) == ('docs/report.md', 'Closing report', 'daemon')
    assert dict(span_named(spans, 'campaign.closing').attributes or {}) == {
        'campaign': 'demo',
        'provider': 'memory local',
        'result': 'documents: 1 added, 0 unchanged',
    }


def test_a_stop_whose_document_sync_fails_still_stops_and_closes_the_campaign(
    db: Database,
    campaign: Campaign,
    runtime: ScriptRuntime,
    tmp_path: Path,
    tracer: Tracer,
    spans: InMemorySpanExporter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Greptile on hiloopai/piloop#7: a sync that raised rolled the stop back and ended the daemon."""
    write_toml(Path(campaign.path), HARNESS_NOOP)
    with db.session() as s:
        cfg = load_campaign_config(
            Path(campaign.path), plugins={'stop_rule': {'max_runs': 1}, 'memory': {'documents': ['docs/*.md']}}
        )
        s.get_one(Campaign, campaign.id).config = cfg
        s.commit()
    add_run(db, campaign)

    def broken(*_args: object) -> None:
        raise PermissionError(13, 'Permission denied', 'docs')

    monkeypatch.setattr(LocalMemory, 'sync', broken)
    sched = make_scheduler(db, runtime, tmp_path, tracer)
    assert run_all(sched) == []
    sched.stop(timeout=5)
    assert closes(db, campaign) == 1
    with db.session() as s:
        assert s.get_one(Campaign, campaign.id).status is CampaignStatus.STOPPED
    result = dict(span_named(spans, 'campaign.closing').attributes or {})['result']
    assert isinstance(result, str) and result.startswith('documents: not synced: PermissionError: [Errno 13]')


def test_the_contract_of_a_stop_rule_is_data_in_data_out() -> None:
    """A rule can move out of process or to a Wasm component: nothing live crosses its contract."""
    assert data_errors(StopRule) == []


def imports_outside_the_standard_library(module: object) -> set[str]:
    tree = ast.parse(Path(str(getattr(module, '__file__', ''))).read_text())
    imported = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module is not None}
    return {m for m in imported if m.split('.')[0] not in sys.stdlib_module_names}


def test_the_built_in_stop_rule_imports_only_the_standard_library_pydantic_its_contract_and_the_metric_types() -> None:
    """``limits`` is a pure provider: it reads nothing of piloop's state. pydantic declares its parameters.
    test_capabilities checks the example."""
    contracts = {
        'piloop.plugins.stop_rule.contract',
        'piloop.plugins.fact_source.facts',
        'piloop.plugins.metric',
        'pydantic',
    }
    assert imports_outside_the_standard_library(limits_module) == contracts


def test_limits_holds_while_a_fact_is_unknown_or_at_its_hold_at_and_a_hold_comes_before_a_stop() -> None:
    """``hold_at`` holds, on any fact that a source reads. A hold releases itself: the next check that sees the value
    below it goes on.

    A held campaign does not stop: nothing may launch while the value is unknown or at its limit.
    """
    spend = reading('spend')
    rule = Limits(max_runs=3, hold_at={'spend': 300})
    assert rule.limits() == {RUNS: 3, spend: 300}
    assert rule.reads() == [RUNS, HOURS, spend, UNSETTLED], 'what it shows, its limits, and what decides each'
    assert spend not in Limits(max_runs=3).reads(), 'no hold on the spend: no spend to read'

    def check(value: float | None, *, runs: int = 1) -> object:
        return rule.check(StopCase('score', Direction.HIGHER, facts(runs=runs, unsettled=0, spend=value)))

    assert check(299.99) == Go(Facts({'runs': 2})), 'a hold leaves no budget: it never stops the campaign'
    assert check(None) == Hold('spend not known'), 'not read yet, or the latest reading failed'
    at_limit = Escalate(
        'spend reached its hold_at of 300: nothing launches until you raise it and run piloop init',
        then=Hold('spend at its limit'),
    )
    assert check(300.0) == at_limit, 'it tells a human itself, once for each limit'
    assert check(300.0, runs=3) == at_limit, 'held, so max_runs waits'
    assert check(12.0, runs=3) == Stop('max_runs: 3 runs')
    gpu = Limits(hold_at={'gpu_hours': 8})
    assert gpu.check(StopCase('score', Direction.HIGHER, facts(gpu_hours=9))) == Escalate(
        'gpu_hours reached its hold_at of 8: nothing launches until you raise it and run piloop init',
        then=Hold('gpu_hours at its limit'),
    ), 'no fact is special'


def test_limits_leaves_each_limit_less_its_value_and_refuses_a_target_on_several_tracks() -> None:
    """``Go.left`` is what piloop starts within: the runs, the hours. A target is reached, not used up."""
    rule = Limits(max_runs=10, max_hours=12, max_tokens=1000, plateau=5, target=0.9)
    known = facts(runs=3, unsettled=1, hours=None, tokens=250, without_best=2, best=0.5)
    assert rule.check(StopCase('score', Direction.HIGHER, known)) == Go(
        Facts({'runs': 7, 'hours': 12.0, 'tokens': 750, 'without_best': 3})
    ), 'no run started yet: every hour is left'
    assert Limits().check(StopCase('score', Direction.HIGHER, facts(runs=3, hours=1.0))) == GO, 'open-ended: no bound'
    assert rule.check_config(Shape(tracks=1)) is None
    assert rule.check_config(Shape(tracks=2)) == 'limits the best, which needs a campaign of one track'
    assert Limits(max_runs=10).check_config(Shape(tracks=2)) is None, 'no target: any number of tracks'


def test_the_example_stop_rule_ends_after_its_new_bests_or_at_a_limit_of_limits() -> None:
    """``examples/plugins/stop-after-bests``: ``limits`` with one more limit, so it reads the facts that it limits and
    that ``limits`` shows, and every limit of ``limits`` still applies."""
    rule = AfterBests(bests=3, max_hours=12, max_tokens=1000, hold_at={'spend': 300})
    assert AfterBests(bests=3).reads() == [RUNS, HOURS, NEW_BESTS]
    assert rule.limits() == {HOURS: 12, TOKENS: 1000, reading('spend'): 300, NEW_BESTS: 3}

    def check(known: Facts) -> object:
        return rule.check(StopCase('score', Direction.HIGHER, known))

    assert check(facts(new_bests=2, hours=1, tokens=10, spend=12)) == Go(
        Facts({'hours': 11.0, 'tokens': 990, 'new_bests': 1})
    )
    assert check(facts(new_bests=3, hours=1, tokens=10, spend=12)) == Stop('after_bests: 3 new bests')
    assert check(facts(new_bests=0, hours=12.5, tokens=10, spend=12)) == Stop('max_hours: 12.5h since the first run')
    assert check(facts(new_bests=0, hours=1, tokens=1000, spend=12)) == Stop('max_tokens: 1000 tokens spent')
    assert isinstance(answer := check(facts(new_bests=3, hours=1, tokens=10, spend=300)), Escalate)
    assert answer.then == Hold('spend at its limit')
    assert check(facts(new_bests=0, hours=1, tokens=10)) == Hold('spend not known')
    shown = rule.describe(StopCase('score', Direction.HIGHER, facts(new_bests=2, hours=1, tokens=10, spend=12)))
    assert '2/3 new bests' in shown, 'its own limit, in the progress line of status and of the briefs'


def test_a_rule_that_reads_a_fact_needs_a_source_of_it_and_a_source_alone_only_posts_notices(
    campaign: Campaign, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rule that reads a fact without a source of it would hold forever."""
    installed(monkeypatch, [EntryPoint('goes_on', f'{PLUGIN}:GoesOn', STOP_RULES.group)])
    try:
        sources = with_spend(notices={'spend': [10]})
        load_campaign_config(
            Path(campaign.path), plugins={'stop_rule': {'provider': 'goes_on'}, 'fact_source': sources}
        )
        refused = "the stop rule 'limits' reads spend, which no fact source of this campaign supplies"
        with pytest.raises(ConfigError, match=re.escape(refused)):
            load_campaign_config(Path(campaign.path), plugins={'stop_rule': {'hold_at': {'spend': 10}}})
    finally:
        host.close()


def test_a_stop_rule_from_a_plugin_replaces_the_built_in_one_through_piloop_toml(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The example plugin ``examples/plugins/stop-after-bests``, installed, is selected by ``[plugins.stop_rule]``.

    Without a limit, ``limits`` never ends the campaign; ``after_bests`` ends it once a run's candidate first becomes the best.
    """
    example(tmp_path / 'site', 'stop-after-bests')
    monkeypatch.syspath_prepend(tmp_path / 'site')
    host.close()
    try:
        write_toml(Path(campaign.path), HARNESS_APPEND)
        for table, error in [
            ({'provider': 'never'}, "plugins.stop_rule: provider = 'never': unknown stop rule 'never'"),
            ({'provider': 'after_bests'}, 'bests: Field required'),
            (
                {'provider': 'limits', 'bests': 1},
                'plugins.stop_rule.bests = 1: Extra inputs are not permitted',
            ),
        ]:
            with pytest.raises(ConfigError, match=re.escape(error)):
                load_campaign_config(Path(campaign.path), plugins={'stop_rule': table})
        with db.session() as s:
            cfg = load_campaign_config(
                Path(campaign.path), plugins={'stop_rule': {'provider': 'after_bests', 'bests': 1}}
            )
            s.get_one(Campaign, campaign.id).config = cfg
            s.commit()
        add_ideas(db, campaign, 'a', 'b')
        sched = make_scheduler(db, runtime, tmp_path)
        assert run_all(sched) == [1]
        sched.wait(SETTLE)
        run_all(sched)
        assert campaign_state(db, campaign) == (CampaignStatus.STOPPED, 'after_bests: 1 new bests')
        sched.stop(timeout=5)
        assert STOP_RULES.sources()['after_bests'].release == 'piloop-stop-after-bests 0.1.0'
    finally:
        host.close()


def test_an_agent_may_run_its_role_timeout_cut_at_the_time_that_the_campaign_stops(
    db: Database, campaign: Campaign
) -> None:
    """A run that started minutes before a max_hours stop got its whole role timeout and outlived the stop."""
    configure(db, campaign, max_runs=4)
    with db.session() as s:
        row = s.get_one(Campaign, campaign.id)
        no_time = CampaignConfig.model_validate(row.config)
        assert agent_timeout(s, row, no_time, 1800, utcnow()) == 1800, 'the rule limits no hours'
    configure(db, campaign, max_hours=3)
    with db.session() as s:
        row = s.get_one(Campaign, campaign.id)
        cfg = CampaignConfig.model_validate(row.config)
        assert agent_timeout(s, row, cfg, 1800, utcnow()) == 1800, 'no run yet: three hours left'
    add_run(db, campaign, started_at=timedelta(hours=2, minutes=55))
    with db.session() as s:
        row = s.get_one(Campaign, campaign.id)
        now = utcnow()
        assert 290 < seconds(agent_timeout(s, row, cfg, 1800, now)) <= 300, 'five minutes left'
        started = now - timedelta(minutes=2)
        assert 410 < seconds(agent_timeout(s, row, cfg, 1800, started)) <= 420, 'an agent that started 2 minutes ago'
        row.status, row.stopped_reason = CampaignStatus.STOPPED, 'by the test'
        assert agent_timeout(s, row, cfg, 1800, now) == 1800, (
            'a stopped campaign cuts nothing: an agent that a person requests'
        )
        row.status, row.stopped_reason = CampaignStatus.ACTIVE, None
    add_run(db, campaign, started_at=timedelta(hours=3, minutes=5))
    with db.session() as s:
        row = s.get_one(Campaign, campaign.id)
        stop = Stop('max_hours: 3.1h since the first run')
        assert agent_timeout(s, row, cfg, 1800, utcnow()) == stop, (
            'past the stop: an agent that has not started never does'
        )
        assert seconds(stop) == 0, 'and one that runs has no time left'
        hold = {'max_hours': 3, 'hold_at': {'spend': 300}}
        held = load_campaign_config(Path(campaign.path), plugins={'stop_rule': hold, 'fact_source': with_spend()})
        assert agent_timeout(s, row, held, 1800, utcnow()) == 1800, 'a hold leaves no budget: nothing launches then'


def test_a_stop_is_due_by_one_rule_so_a_paused_campaign_past_its_limit_cuts_no_agent_short(
    db: Database, campaign: Campaign
) -> None:
    """The pass stops a campaign on ``due``, and an agent's timeout is cut on it: the two never disagree.

    A paused campaign does not stop at a limit of its rule. So its agents keep their whole timeout: an implementer is
    not told of a campaign's stop 0 s away, and an orchestrator that a paused campaign wakes is not put back
    every pass (the paused spin). A stop that someone requested is due also while the campaign is paused.
    """
    configure(db, campaign, max_hours=1)
    add_run(db, campaign, started_at=timedelta(hours=2))
    with db.session() as s:
        row = s.get_one(Campaign, campaign.id)
        cfg = CampaignConfig.model_validate(row.config)
        rule = Stop('max_hours: 2.0h since the first run')
        assert (due(s, row, acted(check_campaign(s, row, cfg))), agent_timeout(s, row, cfg, 1800, utcnow())) == (
            rule,
            rule,
        )
        ops.pause(s, campaign.id, System.HUMAN, evals=False, reason='a review')
        assert due(s, row, acted(check_campaign(s, row, cfg))) is None, 'a paused campaign does not stop at a limit'
        assert agent_timeout(s, row, cfg, 1800, utcnow()) == 1800, 'so it cuts no agent short'
        request_stop(s, row, System.HUMAN, reason='enough', ending=Ending.WRAP_UP)
        asked = Stop('stopped by human: enough')
        assert (due(s, row, acted(check_campaign(s, row, cfg))), agent_timeout(s, row, cfg, 1800, utcnow())) == (
            asked,
            asked,
        )


def facts(**values: FactValue) -> Facts:
    """The facts of a case, by name."""
    return Facts(dict(values))


def test_the_ratios_are_defined_once_and_take_the_facts_a_rule_names() -> None:
    """piloop knows no cost: a rule names the fact of what the campaign spent and the evaluator's cost metric."""
    known = facts(spend=400, new_bests=2, evaluations=10, metric_sums={'usd': 100, 'score': 7}, attempts=6)
    ratios = (
        per_evaluation(known, 'usd'),
        worker_spend(known, 'spend', 'usd'),
        per_run(known, 'spend', 'usd'),
        per_best(known, 'spend'),
    )
    assert ratios == (10, 300, 50, 200)
    assert (per_evaluation(known, 'cost'), worker_spend(known, 'spend', 'cost')) == (None, 400), 'no row printed it'
    assert per_best(known, 'gpu_hours') is None, 'a fact that the case does not have'
    unknown = facts(new_bests=0, evaluations=0, attempts=0)
    ratios = (
        per_evaluation(unknown, 'usd'),
        worker_spend(unknown, 'spend', 'usd'),
        per_run(unknown, 'spend', 'usd'),
        per_best(unknown, 'spend'),
    )
    assert ratios == (None,) * 4
    assert per_run(facts(spend=10, metric_sums={'usd': 4}, attempts=0), 'spend', 'usd') is None, 'no attempt yet'


COST = {'name': 'eval_cost_usd', 'direction': 'lower'}


def with_spend(**keys: object) -> dict[str, object]:
    """``[plugins.fact_source]`` with piloop's own sources and ``spend``, a ``command`` source, with ``keys`` over it."""
    source = {'provider': 'command', 'command': ['true'], 'metrics': ['spend']} | keys
    return {'use': ['campaign', 'optimization', 'spend'], 'spend': source}


def test_the_campaign_facts_sum_every_evaluation_count_the_attempts_and_a_reading_grows_since_the_latest_best(
    db: Database, campaign: Campaign
) -> None:
    metrics = [{'name': 'score', 'direction': 'higher', 'role': 'primary'}, {'name': 'size', 'direction': 'lower'}]
    set_config(db, campaign, metrics=[*metrics, COST], plugins={'fact_source': with_spend()})
    measured = add_run(db, campaign)
    add_run(db, campaign, status=RunStatus.FAILED, end=RunEnd.CANCELLED, measured=False)
    add_run(db, campaign, status=RunStatus.RUNNING)
    with db.session() as s:
        evaluator = s.get_one(Campaign, campaign.id).evaluator_id or 0
        candidate = s.scalars(select(Candidate).where(Candidate.run_id == measured)).one()
        s.add(Measurement(candidate_id=candidate.id, evaluator_id=evaluator, metrics={'eval_cost_usd': 2}))
        failed = {'eval_cost_usd': 1.5}
        s.add(Measurement(candidate_id=candidate.id, evaluator_id=evaluator, metrics=failed, error='evaluate exit 1'))
        s.commit()
    spend, since = reading('spend'), since_best('spend')
    every = [*CAMPAIGN, *OPTIMIZATION, spend, since]

    def known() -> Facts:
        with db.session() as s:
            row = s.get_one(Campaign, campaign.id)
            return gather(s, row, row.config, every, utcnow())

    def read(values: dict[str, float] | FactReadError, ago: timedelta = timedelta(0)) -> None:
        with db.session() as s:
            row = s.get_one(Campaign, campaign.id)
            [_, _, source] = row.config.fact_sources()
            assert isinstance(source, Readings)
            source.record(s, row, values)
            s.flush()
            s.execute(update(Event).where(Event.id == s.scalar(func.max(Event.id))).values(at=utcnow() - ago))
            s.commit()

    assert known().get(spend) is None, 'no reading yet'
    read({'spend': 100}, ago=timedelta(minutes=10))
    before = known()
    assert (before.get(EVALUATIONS), before.get(METRIC_SUMS), before.get(ATTEMPTS)) == (
        4,
        {'score': 1, 'size': 5, COST['name']: 3.5},
        2,
    ), 'every row, a failed one and the baseline among them; a row adds only what it printed; the open attempt too'
    assert before.get(FUNNEL) == Funnel(ideas=3, attempts=1, reached=1, measured=1, bests=0, unmeasured={}), (
        'the funnel waits for the open run, and a cancelled run is no attempt'
    )
    assert [before.get(f) for f in (spend, NEW_BESTS, since)] == [100, 0, 100], 'no new best yet: all of the spend'
    add_run(db, campaign, keep_score=2, finished_at=timedelta(minutes=5))
    read({'spend': 150})
    after = known()
    assert [after.get(f) for f in (NEW_BESTS, ATTEMPTS, since)] == [1, 3, 50], 'the spend read before the new best'
    assert (per_evaluation(after, COST['name']), per_best(after, 'spend')) == (3.5 / 5, 150)
    read(FactReadError('billing down'))
    assert (known().get(spend), known().get(since)) == (None, None), 'a failed reading: the value is not known'
    add_run(db, campaign, measured=False)
    assert known().get(FUNNEL) == Funnel(
        ideas=5, attempts=3, reached=2, measured=2, bests=1, unmeasured={'finished': 1}
    ), 'an attempt that left no candidate never reached the evaluator'
    with db.session() as s:
        row = s.get_one(Campaign, campaign.id)
        assert gather(s, row, row.config, [NEW_BESTS], utcnow()) == facts(new_bests=1), 'only the facts a rule reads'


def test_a_rule_that_escalates_lets_the_campaign_go_on_and_tells_a_human_once_per_reason(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installed(monkeypatch, [EntryPoint('escalates', f'{PLUGIN}:Escalates', STOP_RULES.group)])
    try:
        set_config(
            db, campaign, plugins={'stop_rule': {'provider': 'escalates', 'reason': '$900 since the last new best'}}
        )
        add_ideas(db, campaign, 'a', 'b')
        sched = make_scheduler(db, runtime, tmp_path)
        assert run_all(sched) == [1], 'the campaign goes on'
        sched.wait(SETTLE)
        run_all(sched)
        set_config(
            db, campaign, plugins={'stop_rule': {'provider': 'escalates', 'reason': '$1,800 since the new best'}}
        )
        run_all(sched)
        sched.wait(SETTLE)
        sched.stop(timeout=5)
        with db.session() as s:
            said = s.scalars(select(Comment).where(Comment.kind == ESCALATION).order_by(Comment.id))
            assert [(c.target, c.author, c.priority, details(c)) for c in said] == [
                (TargetRef(TargetKind.CAMPAIGN, campaign.id), 'daemon', 0, f'Stop rule escalates: {reason}')
                for reason in ('$900 since the last new best', '$1,800 since the new best')
            ], 'one escalation for each distinct reason: the comment is the record'
    finally:
        host.close()


def test_the_example_best_cost_rule_escalates_once_per_new_best_then_stops_at_its_harder_line() -> None:
    """``examples/plugins/best-cost``: the spend since the latest new best against the spend per new best, over
    ``limits``."""
    rule = BestCost(escalate_at=2, stop_at=4, max_hours=12)
    assert rule.reads() == [RUNS, HOURS, reading('spend'), NEW_BESTS, since_best('spend')]

    def check(since: float, *, bests: int = 2, spend: float = 400, hours: float = 1) -> object:
        known = facts(hours=hours, spend=spend, new_bests=bests, spend_since_best=since)
        return rule.check(StopCase('score', Direction.HIGHER, known))

    assert check(399) == Go(Facts({'hours': 11.0})), 'below 2 × $200 per new best'
    escalation = 'best_cost: since new best 2, spend passed 2 × its value per new best; it stops at 4 ×'
    assert check(400) == check(700) == Escalate(escalation), 'one reason for the new best, whatever the amount'
    assert check(800) == Stop('best_cost: spend 800 since the latest new best, 4 × the 200 per new best')
    assert check(5000, bests=0) == Go(Facts({'hours': 11.0})), 'no new best yet: no spend per new best'
    other = BestCost(spend='gpu_hours')
    assert since_best('gpu_hours') in other.reads(), 'the rule reads the fact that it names'
    assert check(800, hours=13) == Stop('max_hours: 13.0h since the first run'), 'the limits of limits come first'


def test_a_rule_that_reads_the_spend_since_a_new_best_needs_a_source_of_it(
    campaign: Campaign, monkeypatch: pytest.MonkeyPatch
) -> None:
    installed(monkeypatch, [EntryPoint('best_cost', 'piloop_best_cost:BestCost', STOP_RULES.group)])
    try:
        refused = "the stop rule 'best_cost' reads spend, spend_since_best, which no fact source of this campaign"
        with pytest.raises(ConfigError, match=re.escape(refused)):
            load_campaign_config(Path(campaign.path), plugins={'stop_rule': {'provider': 'best_cost'}})
        load_campaign_config(
            Path(campaign.path), plugins={'stop_rule': {'provider': 'best_cost'}, 'fact_source': with_spend()}
        )
    finally:
        host.close()


def test_a_reopened_campaign_gets_its_runs_hours_and_tokens_again(db: Database, campaign: Campaign) -> None:
    configure(db, campaign, max_runs=1, max_hours=1, max_tokens=100)
    add_run(db, campaign, started_at=timedelta(hours=2), finished_at=timedelta(hours=1))
    with db.session() as s:
        ended = dict(state=AgentSessionState.EXITED, ended_at=utcnow())
        s.add(AgentSession(kind=IMPLEMENTER, campaign_id=campaign.id, usage=spent(500), **ended))
        stopped_and_closed(s, campaign)
        s.commit()
    assert reason(db, campaign) == 'max_runs: 1 runs'
    with db.session() as s:
        with pytest.raises(Refused, match='^a reason is required'):
            ops.reopen(s, campaign.id, System.HUMAN, reason=' ')
        ops.reopen(s, campaign.id, System.HUMAN, reason='one more round')
        s.commit()
    assert reason(db, campaign) is None, 'the run, the hours and the tokens before the reopen do not count'
    with db.session() as s:
        row = s.get_one(Campaign, campaign.id)
        assert (row.status, row.stopped_reason) == (CampaignStatus.ACTIVE, None)
        [(_, reopened)] = in_order(s, CampaignReopened, Event.target == TargetRef(TargetKind.CAMPAIGN, campaign.id))
        assert reopened == CampaignReopened(reason='one more round')
        with pytest.raises(Refused, match='^demo is active: only a stopped campaign reopens; stop it first'):
            ops.reopen(s, campaign.id, System.HUMAN, reason='twice')
    add_run(db, campaign)
    assert reason(db, campaign) == 'max_runs: 1 runs', 'a run since the reopen counts'


def test_a_reopen_refuses_a_campaign_that_its_rule_would_stop_at_once_or_that_is_retired(
    db: Database, campaign: Campaign
) -> None:
    write_toml(Path(campaign.path), HARNESS_NOOP)
    with db.session() as s:
        row = s.get_one(Campaign, campaign.id)
        row.config = load_campaign_config(Path(campaign.path), plugins={'stop_rule': {'target': 0.5}})
        stopped_and_closed(s, campaign)
        s.commit()
    with db.session() as s:
        with pytest.raises(
            Refused, match=r'^demo would stop again at once \(target: .*\): change its stop rule first$'
        ):
            ops.reopen(s, campaign.id, System.HUMAN, reason='go on')
        s.rollback()
        assert s.get_one(Campaign, campaign.id).status is CampaignStatus.STOPPED
        row = s.get_one(Campaign, campaign.id)
        row.status, row.stopped_reason = CampaignStatus.RETIRED, 'by the test'
        s.commit()
    retired = rf'^campaign #{campaign.id} \(demo\) is retired, for good: fork it instead'
    with db.session() as s, pytest.raises(Refused, match=retired):
        ops.reopen(s, campaign.id, System.HUMAN, reason='go on')


def stopped_and_closed(s: Session, campaign: Campaign) -> None:
    """Stop ``campaign`` as the daemon does, and close it."""
    row = s.get_one(Campaign, campaign.id)
    row.status, row.stopped_reason = CampaignStatus.STOPPED, 'by the test'
    emit(s, CampaignClosed(), TargetRef(TargetKind.CAMPAIGN, campaign.id), System.DAEMON)
