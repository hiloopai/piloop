"""The control operations: each validates, changes the state and leaves its event, in the caller's transaction."""

import re
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest
from git import Repo
from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from piloop.actor import Agent, System
from piloop.campaign import ops
from piloop.campaign.stop import CAMPAIGN_STATES
from piloop.config import load_campaign_config
from piloop.db import machine
from piloop.db.events import in_order
from piloop.db.models import ENDED, AgentSession, AgentSessionState, Campaign, Comment, Event, EventModel, Pause, utcnow
from piloop.db.session import Database
from piloop.db.triggers.changelog import (
    CampaignParentChanged,
    CampaignPausedChanged,
    ExclusionChanged,
    IdeaCreated,
    IdeaLinked,
    IdeaTrackChanged,
)
from piloop.ids import CampaignId, CampaignStatus, CommentKind, TargetKind, TargetRef
from piloop.plugins.campaign_type.kit.backlog import ops as backlog_ops
from piloop.plugins.campaign_type.kit.backlog.ideas import claimable
from piloop.plugins.campaign_type.kit.backlog.ids import IDEA, IdeaId, IdeaStatus, LinkKind
from piloop.plugins.campaign_type.kit.backlog.tables import Idea, IdeaLink
from piloop.plugins.campaign_type.kit.candidates.ids import CANDIDATE, CandidateId, Origin
from piloop.plugins.campaign_type.kit.candidates.references import import_reference
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.evaluation import ops as evaluation_ops, wire as evaluation_api
from piloop.plugins.campaign_type.kit.evaluation.ids import MEASUREMENT, TrackName
from piloop.plugins.campaign_type.kit.evaluation.measure import MeasurementRecorded
from piloop.plugins.campaign_type.kit.evaluation.tables import Measurement
from piloop.plugins.campaign_type.kit.pool import ops as pool_ops
from piloop.plugins.campaign_type.kit.pool.pool import Pool, best_of
from piloop.plugins.campaign_type.kit.runs import ops as runs_ops
from piloop.plugins.campaign_type.kit.runs.ids import RUN, RunEnding, RunId, RunStatus
from piloop.plugins.campaign_type.kit.runs.ops import RunEndRequested
from piloop.plugins.campaign_type.kit.runs.tables import Run
from piloop.plugins.campaign_type.optimization.ops import follow_up
from piloop.plugins.vcs.git import PILOOP, GitRepository
from tests.conftest import bare_config, end_of, reconfigured

ACTOR = System.ORCHESTRATOR


@pytest.fixture
def me(db: Database, campaign: Campaign, tmp_path: Path) -> Iterator[Database]:
    """The campaign's database, opened as the orchestrator: the change-log triggers sign its writes with that actor."""
    database = Database(f'sqlite:///{tmp_path / "piloop.db"}', actor=ACTOR)
    yield database
    database.close()


def kinds(db: Database, target_kind: TargetKind, target_id: int) -> list[tuple[str, str, EventModel]]:
    with db.session() as s:
        rows = s.scalars(
            select(Event).where(Event.target_kind == target_kind, Event.target_id == target_id).order_by(Event.id)
        )
        return [(e.kind, e.actor, e.payload) for e in rows]


CREATED = ('idea.created', 't', IdeaCreated())
"""The event of an idea that ``add_idea`` inserts."""


def add_idea(db: Database, campaign_id: CampaignId, title: str, status: IdeaStatus = IdeaStatus.OPEN) -> IdeaId:
    with db.session() as s:
        idea = Idea(
            campaign_id=campaign_id, track='score', title=title, hypothesis=title, status=status, created_by='t'
        )
        s.add(idea)
        s.commit()
        return idea.id


def run_idea(db: Database, status: IdeaStatus, run: RunStatus, *, scoring: bool = False) -> IdeaId:
    """An idea in ``status`` with one run in ``run``: a status set by hand while the run is open, or scored."""
    idea_id = add_idea(db, CampaignId(1), f'{status} with a {run} run {scoring}', status)
    with db.session() as s:
        s.add(
            Run(
                campaign_id=CampaignId(1),
                idea_id=idea_id,
                attempt=1,
                base_candidate_id=1,
                harness='h',
                model='m',
                status=run,
                end=end_of(run),
                scoring=scoring,
            )
        )
        s.commit()
    return idea_id


def run_candidate(db: Database, campaign: Campaign, *, origin: Origin = Origin.RUN) -> CandidateId:
    """A candidate of ``origin`` of a run of a new idea from the baseline, with one measurement (score 0)."""
    idea_id = add_idea(db, campaign.id, f'tried {origin}')
    with db.session() as s:
        run = Run(campaign_id=campaign.id, idea_id=idea_id, attempt=1, base_candidate_id=1, harness='script', model='m')
        s.add(run)
        s.flush()
        c = Candidate(
            campaign_id=campaign.id,
            run_id=run.id,
            parent_id=1,
            tree_sha=f'tree {idea_id}',
            revision=f'commit {idea_id}',
            origin=origin,
        )
        s.add(c)
        s.flush()
        s.add(Measurement(candidate_id=c.id, evaluator_id=1, metrics={'score': 0.0, 'size': 0.0}))
        s.commit()
        return c.id


def test_a_pause_holds_launches_or_everything_with_its_reason_and_a_resume_clears_it(me: Database) -> None:
    with me.session() as s:
        campaign = ops.pause(s, CampaignId(1), System.HUMAN, evals=False, reason=' wait for evaluator v2 ')
        s.commit()
        assert ops.paused(campaign) == ops.Paused(Pause.LAUNCHES, System.HUMAN, 'wait for evaluator v2')
    with (
        me.session() as s,
        pytest.raises(ops.Refused, match=r'paused already \(launches\) by human: wait for evaluator v2'),
    ):
        ops.pause(s, CampaignId(1), System.HUMAN, evals=False, reason='again')
    with me.session() as s:
        ops.pause(s, CampaignId(1), System.HUMAN, evals=True, reason='and the evaluations')
        s.commit()
    with me.session() as s:
        assert ops.resume(s, CampaignId(1)) == ops.Paused(Pause.ALL, System.HUMAN, 'and the evaluations')
        s.commit()
        assert ops.paused(s.get_one(Campaign, 1)) is None
    assert [e for e in kinds(me, TargetKind.CAMPAIGN, 1) if e[0] == 'campaign.paused'] == [
        (
            'campaign.paused',
            ACTOR.signature,
            CampaignPausedChanged(old=None, new=Pause.LAUNCHES, paused_reason='wait for evaluator v2'),
        ),
        (
            'campaign.paused',
            ACTOR.signature,
            CampaignPausedChanged(old=Pause.LAUNCHES, new=Pause.ALL, paused_reason='and the evaluations'),
        ),
        ('campaign.paused', ACTOR.signature, CampaignPausedChanged(old=Pause.ALL, new=None, paused_reason=None)),
    ], 'the trigger logs each change, signed by the connection, and nothing else does'
    with me.session() as s:
        assert s.scalars(select(Comment)).all() == [], 'a pause is not an escalation'
        with pytest.raises(ops.Refused, match='campaign demo is not paused'):
            ops.resume(s, CampaignId(1))
        with pytest.raises(ops.Refused, match='a reason is required'):
            ops.pause(s, CampaignId(1), System.HUMAN, evals=False, reason='  ')
        with pytest.raises(ops.Refused, match='no campaign#9'):
            ops.pause(s, CampaignId(9), System.HUMAN, evals=False, reason='why')


def test_a_hold_of_the_daemon_only_widens_a_pause_and_a_resume_says_whose_pause_it_lifted(me: Database) -> None:
    """The daemon's holds have one writer, ``hold``, and the pause records its owner.

    A hold never narrows a pause, nor replaces one that holds as much: a person's reason stays. One that holds more
    takes the pause over, and a resume lifts it as any other, saying that it was the daemon's.
    """
    with me.session() as s:
        row = s.get_one(Campaign, 1)
        ops.hold(row, Pause.LAUNCHES, 'the baseline failed')
        assert ops.paused(row) == ops.Paused(Pause.LAUNCHES, System.DAEMON, 'the baseline failed')
        ops.hold(row, Pause.LAUNCHES, 'it failed again')
        assert ops.paused(row) == ops.Paused(Pause.LAUNCHES, System.DAEMON, 'the baseline failed'), 'once'
        s.commit()
        assert ops.resume(s, CampaignId(1)) == ops.Paused(Pause.LAUNCHES, System.DAEMON, 'the baseline failed')
        ops.pause(s, CampaignId(1), System.HUMAN, evals=False, reason='a review')
        ops.hold(row, Pause.LAUNCHES, 'the baseline failed')
        assert ops.paused(row) == ops.Paused(Pause.LAUNCHES, System.HUMAN, 'a review'), "the person's stays"
        ops.hold(row, Pause.ALL, 'evaluations fail for the machine')
        assert ops.paused(row) == ops.Paused(Pause.ALL, System.DAEMON, 'evaluations fail for the machine'), 'widened'
        ops.hold(row, Pause.LAUNCHES, 'the baseline failed')
        assert ops.paused(row) == ops.Paused(Pause.ALL, System.DAEMON, 'evaluations fail for the machine'), (
            'a hold never narrows a pause'
        )
        s.commit()


def test_a_retire_lifts_the_pause_and_a_retired_campaign_never_moves_again(me: Database) -> None:
    """A retired campaign holds nothing (``CAMPAIGN_CHECKS``), and ``retired`` is an end of ``CAMPAIGN_STATES``."""
    with me.session() as s:
        row = s.get_one(Campaign, 1)
        row.status, row.stopped_reason = CampaignStatus.STOPPED, 'by the test'
        ops.pause(s, CampaignId(1), System.HUMAN, evals=True, reason='a review')
        s.commit()
        ops.retire(s, CampaignId(1), System.HUMAN, reason='done')
        s.commit()
        assert (row.status, ops.paused(row)) == (CampaignStatus.RETIRED, None)
        with pytest.raises(machine.Refused, match=r'is retired: it cannot become active'):
            machine.transition(
                s, CAMPAIGN_STATES, row.id, CampaignStatus.ACTIVE, None, System.HUMAN, stopped_reason=None
            )


def test_a_raw_pause_needs_a_reason_and_a_known_scope(me: Database, tmp_path: Path) -> None:
    with closing(sqlite3.connect(tmp_path / 'piloop.db')) as con:
        with pytest.raises(sqlite3.IntegrityError, match='campaigns.paused and campaigns.paused_reason go together'):
            con.execute("UPDATE campaigns SET paused = 'launches' WHERE id = 1")
        with pytest.raises(sqlite3.IntegrityError, match='CHECK constraint failed'):
            con.execute(
                "UPDATE campaigns SET paused = 'forever', paused_reason = 'r', paused_by = 'human' WHERE id = 1"
            )
        with pytest.raises(sqlite3.IntegrityError, match='campaigns.a pause has its owner'):
            con.execute("UPDATE campaigns SET paused = 'launches', paused_reason = 'r' WHERE id = 1")
    with me.session() as s:
        s.get_one(Campaign, 1).paused_reason = 'no pause'
        with pytest.raises(IntegrityError, match='go together'):
            s.commit()


@pytest.mark.usefixtures('auditor')
def test_an_agent_whose_role_files_nothing_adds_no_idea_and_the_others_add_open_ideas(me: Database) -> None:
    with me.session() as s:
        with pytest.raises(ops.Refused, match='implementer#6 files no ideas'):
            backlog_ops.add_idea(s, CampaignId(1), 'mine', actor=Agent(role='implementer', session=6))
        for actor in (ACTOR, System.HUMAN, Agent(role='auditor', session=4)):
            idea = backlog_ops.add_idea(s, CampaignId(1), f'by {actor}', actor=actor)
            assert (idea.status, idea.created_by) == (IdeaStatus.OPEN, actor.signature)


def test_an_exclusion_is_the_actors_with_the_reason_in_its_event_and_takes_the_candidate_out_of_the_race(
    me: Database, campaign: Campaign
) -> None:
    c = run_candidate(me, campaign)
    with me.session() as s:
        evaluation_ops.add_measurement(s, c, evaluation_api.Report(metrics={'score': 5.0, 'size': 1.0}), actor=ACTOR)
        s.commit()
        best = best_of(s, s.get_one(Campaign, 1))
        assert best is not None and best.id == c, 'its mean, 2.5, beats the baseline'
        assert pool_ops.exclude(s, c, ' gamed the evaluation ', actor=ACTOR).id == c
        s.commit()
        candidate = s.get_one(Candidate, c)
        assert (candidate.excluded_reason, candidate.excluded_by) == ('gamed the evaluation', ACTOR.signature)
        best = best_of(s, s.get_one(Campaign, 1))
        assert best is not None and best.id == 1, 'excluded: the baseline is the best again'
        pool = Pool.of(s, s.get_one(Campaign, 1), s.get_one(Campaign, 1).config.all_tracks[0])
        assert pool.standing(c).told() == (
            'score 2.5 over 2 evals: excluded (gamed the evaluation); vs parent +1.5, vs best candidate#1 +1.5'
        ), 'its scores stay in the pool'
    payload = ExclusionChanged(old=None, new='gamed the evaluation', excluded_by=ACTOR.signature)
    assert [(k, a, e) for k, a, e in kinds(me, CANDIDATE, c) if k == 'candidate.excluded_reason'] == [
        ('candidate.excluded_reason', ACTOR.signature, payload)
    ]
    with me.session() as s:
        for target, reason, error in [
            (CandidateId(99), 'why', 'no candidate#99'),
            (c, ' ', 'a reason is required'),
            (CandidateId(1), 'why', 'candidate#1 is the baseline, which may always be best'),
        ]:
            with pytest.raises(ops.Refused, match=re.escape(error)):
                pool_ops.exclude(s, target, reason, actor=ACTOR)


def test_a_measurement_by_hand_is_recorded_and_says_where_its_candidate_stands(
    me: Database, campaign: Campaign
) -> None:
    """Its mean includes the measurement at once, so the pool and its best follow: nothing is judged again."""
    c = run_candidate(me, campaign)
    report = evaluation_api.Report(metrics={'score': 3.0, 'size': 9.0}, seconds=12.5)
    with me.session() as s:
        added = evaluation_ops.add_measurement(s, c, report, actor=ACTOR)
        s.commit()
        measurement = added.measurement
        assert added.standing == (
            'score 1.5 over 2 evals: the best of the score track now, rank 1 of 2; vs parent +0.5'
        ), 'the mean of 0 and 3 beats the baseline'
        assert (measurement.evaluator_id, measurement.metrics, measurement.seconds) == (1, report.metrics, 12.5)
        best = best_of(s, s.get_one(Campaign, 1))
        assert best is not None and best.id == c
    assert kinds(me, MEASUREMENT, measurement.id) == [
        (
            'measurement.recorded',
            ACTOR.signature,
            MeasurementRecorded(candidate=c, metrics={'score': 3.0, 'size': 9.0}, error=None, run=None),
        )
    ], 'shaped like the daemon event'
    assert kinds(me, CANDIDATE, c) == [], 'no request: the best is a query'
    with me.session() as s:
        failed = evaluation_ops.add_measurement(
            s, c, evaluation_api.Report(metrics={}, error='timeout'), actor=ACTOR
        ).standing
        assert failed.startswith('score 1.5 over 2 evals: '), 'a failed evaluation leaves the mean'
        for origin in (Origin.RECOVERED, Origin.SKIPPED):
            s.get_one(Candidate, c).origin = origin
            with pytest.raises(
                ops.Refused, match=f'candidate#{c} is {origin}: it never enters the pool; submit its tree to score it'
            ):
                evaluation_ops.add_measurement(s, c, report, actor=ACTOR)


def test_a_follow_up_title_is_free_in_its_own_campaign(me: Database, campaign: Campaign) -> None:
    with me.session() as s:
        other = Campaign(name='other', path='/other', config=bare_config('other'))
        s.add(other)
        s.flush()
        s.add(Idea(campaign_id=other.id, track='score', title='try', hypothesis='h', created_by='t'))
        s.flush()
        [seed] = follow_up(s, s.get_one(Candidate, 1), ['try'], 'why', ACTOR, track=TrackName('score'))
        assert seed.title == 'try', "another campaign's idea titles are not this one's"


def test_a_measurement_by_hand_refuses_a_report_that_does_not_fit_the_campaign(
    me: Database, campaign: Campaign
) -> None:
    with me.session() as s:
        with pytest.raises(ops.Refused, match='the report has no size: give every metric, or the error'):
            evaluation_ops.add_measurement(
                s, CandidateId(1), evaluation_api.Report(metrics={'score': 1.0}), actor=ACTOR
            )
        with pytest.raises(ops.Refused, match='piloop.toml has no metric accuracy: its metrics are score, size'):
            evaluation_ops.add_measurement(
                s,
                CandidateId(1),
                evaluation_api.Report(metrics={'score': 1.0, 'size': 5.0, 'accuracy': 1.0}),
                actor=ACTOR,
            )
        with pytest.raises(ops.Refused, match='no candidate#99'):
            evaluation_ops.add_measurement(s, CandidateId(99), evaluation_api.Report(metrics={}), actor=ACTOR)
        s.get_one(Campaign, 1).evaluator_id = None
        with pytest.raises(ops.Refused, match='campaign demo has no evaluator yet'):
            evaluation_ops.add_measurement(
                s, CandidateId(1), evaluation_api.Report(metrics={'score': 1.0, 'size': 5.0}), actor=ACTOR
            )


@pytest.mark.parametrize(
    'text',
    [
        '{"metrics": {"score": "high"}}',
        '{"metrics": {"score": 1e999}}',
        '{"metrics": {}, "seconds": -1}',
        '{"metrics": {}, "extra": 1}',
        '{"seconds": 1}',
    ],
)
def test_a_report_is_validated_at_the_boundary(text: str) -> None:
    with pytest.raises(ValidationError):
        evaluation_api.Report.model_validate_json(text)


def test_measuring_held_says_when_the_daemon_measures_a_campaign_again(me: Database, campaign: Campaign) -> None:
    """What a remeasure answers: ``held`` is why the daemon measures it later, and a stopped campaign is measured."""
    with me.session() as s:
        p = s.get_one(Campaign, 1)
        p.status, p.stopped_reason = CampaignStatus.STOPPED, 'max_runs: 3 runs'
        assert evaluation_ops.measuring_held(s, p, p.config) == '', 'a stopped campaign is still measured on request'
        p.status, p.stopped_reason = CampaignStatus.ACTIVE, None
        ops.pause(s, CampaignId(1), System.HUMAN, evals=True, reason='why')
        assert evaluation_ops.measuring_held(s, p, p.config) == (
            ' once campaign demo is resumed (its evaluations are paused)'
        )
        ops.resume(s, CampaignId(1))
        source = {'provider': 'command', 'command': ['true'], 'metrics': ['spend']}
        sources = {'use': ['campaign', 'optimization', 'spend'], 'spend': source}
        p.config = reconfigured(p.config, plugins={'stop_rule': {'hold_at': {'spend': 10.0}}, 'fact_source': sources})
        assert evaluation_ops.measuring_held(s, p, p.config) == (
            ' once the stop rule no longer holds it (spend not known)'
        )
        p.status, p.stopped_reason = CampaignStatus.STOPPED, 'by the test'
        assert evaluation_ops.measuring_held(s, p, p.config) == '', 'the stop rule holds an active campaign only'


def worker(db: Database, status: RunStatus = RunStatus.RUNNING) -> tuple[RunId, Agent]:
    """A fresh run of campaign#1 in ``status``, with no idea, and the implementer that works on it."""
    with db.session() as s:
        run = Run(
            campaign_id=CampaignId(1),
            base_candidate_id=CandidateId(1),
            harness='h',
            model='m',
            status=status,
            end=end_of(status),
        )
        s.add(run)
        s.flush()
        state = AgentSessionState.RUNNING if status is RunStatus.RUNNING else AgentSessionState.REQUESTED
        session = AgentSession(
            kind='implementer',
            campaign_id=1,
            subject=TargetRef(RUN, run.id),
            state=state,
            ended_at=utcnow() if state in ENDED else None,
        )
        s.add(session)
        s.commit()
        return run.id, Agent(role='implementer', session=session.id)


def test_a_worker_claims_one_open_idea_for_its_run_and_every_other_claim_is_refused(
    me: Database, campaign: Campaign
) -> None:
    held, free, first, dropped = (add_idea(me, CampaignId(1), t) for t in ('held', 'free', 'first', 'dropped'))
    claimed, done = (
        add_idea(me, CampaignId(1), t, status) for t, status in (('c', IdeaStatus.CLAIMED), ('d', IdeaStatus.DONE))
    )
    with me.session() as s:
        backlog_ops.link(s, held, LinkKind.AFTER, first, reason='after it', actor=ACTOR)
        s.get_one(Idea, dropped).status = IdeaStatus.DROPPED
        elsewhere = Campaign(name='elsewhere', path='/elsewhere', config=s.get_one(Campaign, 1).config)
        s.add(elsewhere)
        s.flush()
        strays = [
            Idea(campaign_id=campaign_id, title=title, hypothesis='h', track=track, created_by='human')
            for title, campaign_id, track in (('elsewhere', elsewhere.id, 'score'), ('gone track', 1, 'gone'))
        ]
        s.add_all(strays)
        s.commit()
        other, gone = (i.id for i in strays)
    run_id, implementer = worker(me)
    _, ended = worker(me, RunStatus.FAILED)
    for idea, actor, error in [
        (free, ACTOR, 'orchestrator has no run: only a worker has one'),
        (free, ended, r'run#\d+ is failed already'),
        (held, implementer, f'idea#{held} is held by idea#{first} \\(open\\)'),
        (dropped, implementer, f'idea#{dropped} is dropped: claim an open idea'),
        (claimed, implementer, f'idea#{claimed} is claimed: claim an open idea'),
        (done, implementer, f'idea#{done} is done: claim an open idea'),
        (IdeaId(99), implementer, 'no idea#99'),
        (other, implementer, 'is in another campaign than run#'),
        (gone, implementer, "is on track 'gone', which piloop.toml does not have: score"),
    ]:
        with me.session() as s, pytest.raises(ops.Refused, match=error):
            backlog_ops.claim(s, idea, actor=actor)
    with me.session() as s:
        taken = backlog_ops.claim(s, free, actor=implementer)
        s.commit()
        run = s.get_one(Run, run_id)
        assert (taken.status, run.idea_id, run.attempt) == (IdeaStatus.CLAIMED, free, 1)
    with me.session() as s, pytest.raises(ops.Refused, match=f'run#{run_id} claimed idea#{free} already'):
        backlog_ops.claim(s, first, actor=implementer)


def test_a_dropped_prerequisite_frees_a_claim_as_the_board_lists_it(me: Database, campaign: Campaign) -> None:
    gate, after = add_idea(me, CampaignId(1), 'gate'), add_idea(me, CampaignId(1), 'after the gate')
    with me.session() as s:
        backlog_ops.link(s, after, LinkKind.AFTER, gate, reason='after it', actor=ACTOR)
        s.get_one(Idea, gate).status = IdeaStatus.DROPPED
        s.commit()
        assert set(s.scalars(select(Idea.id).where(claimable()))) == {after}, 'the board lists it'
    _, implementer = worker(me)
    with me.session() as s:
        assert backlog_ops.claim(s, after, actor=implementer).status is IdeaStatus.CLAIMED, 'and the claim takes it'


def test_two_workers_that_claim_one_idea_at_once_get_it_once(me: Database, campaign: Campaign) -> None:
    """The claim holds the write lock from its first read, so the second claim reads the first one's write."""
    idea = add_idea(me, CampaignId(1), 'wanted')
    (first, a), (second, b) = worker(me), worker(me)
    refused: list[str] = []
    with me.session() as s:
        backlog_ops.claim(s, idea, actor=a)

        def other() -> None:
            with me.session() as t:
                try:
                    backlog_ops.claim(t, idea, actor=b)
                    t.commit()
                except ops.Refused as e:
                    refused.append(str(e))

        racing = threading.Thread(target=other)
        racing.start()
        racing.join(0.5)
        assert racing.is_alive(), 'the second claim waits for the write lock of the first'
        s.commit()
    racing.join(10)
    assert refused == [f'idea#{idea} is claimed: claim an open idea']
    with me.session() as s:
        assert [(r.id, r.idea_id) for r in s.scalars(select(Run).where(Run.id.in_((first, second))))] == [
            (first, idea),
            (second, None),
        ]


def test_a_worker_proposes_its_own_idea_and_claims_it_but_files_none_for_others(
    me: Database, campaign: Campaign
) -> None:
    run_id, implementer = worker(me)
    with me.session() as s, pytest.raises(ops.Refused, match='files no ideas for others'):
        backlog_ops.add_idea(s, CampaignId(1), 'for others', actor=implementer)
    with me.session() as s, pytest.raises(ops.Refused, match='orchestrator has no run'):
        backlog_ops.propose(s, 'mine', actor=ACTOR)
    with me.session() as s:
        idea = backlog_ops.propose(s, 'mine', actor=implementer, hypothesis='halve the window')
        s.commit()
        assert (idea.status, idea.created_by, idea.hypothesis) == (
            IdeaStatus.CLAIMED,
            implementer.signature,
            'halve the window',
        )
        assert s.get_one(Run, run_id).idea_id == idea.id


def test_a_worker_ends_its_own_running_run_once_with_or_without_a_reason_to_give_up(
    me: Database, campaign: Campaign
) -> None:
    run_id, implementer = worker(me)
    _, queued = worker(me, RunStatus.QUEUED)
    ending_id, ending = worker(me)
    with me.session() as s:
        s.get_one(AgentSession, ending.session).state = AgentSessionState.ENDING
        s.commit()
    for how, reason, actor, error in [
        (RunEnding.SUBMITTED, None, ACTOR, 'orchestrator has no run'),
        (RunEnding.SUBMITTED, None, queued, r'run#\d+ is queued: a run ends while it runs'),
        (RunEnding.SUBMITTED, None, ending, 'is ending: the daemon ended it before its run ended'),
    ]:
        with me.session() as s, pytest.raises(ops.Refused, match=error):
            runs_ops.end_run(s, how, reason, actor=actor)
    with me.session() as s:
        assert runs_ops.end_run(s, RunEnding.GAVE_UP, ' too slow ', actor=implementer).id == run_id
        s.commit()
        assert runs_ops.end_requested(s, s.get_one(Run, run_id)) == RunEndRequested(
            how=RunEnding.GAVE_UP, reason='too slow'
        )
    with me.session() as s, pytest.raises(ops.Refused, match=f'run#{run_id} was ended already: gave_up'):
        runs_ops.end_run(s, RunEnding.SUBMITTED, None, actor=implementer)
    assert [k for k, _, _ in kinds(me, RUN, run_id)] == ['run.end_requested']
    silent_id, silent = worker(me)
    with me.session() as s:
        runs_ops.end_run(s, RunEnding.GAVE_UP, ' ', actor=silent)
        s.commit()
        assert runs_ops.end_requested(s, s.get_one(Run, silent_id)) == RunEndRequested(how=RunEnding.GAVE_UP), (
            'a reason is optional'
        )
    assert [k for k, _, _ in kinds(me, RUN, ending_id)] == []


def test_an_idea_status_takes_a_reason_in_a_note_and_refuses_what_is_not_set_by_hand(
    me: Database, campaign: Campaign
) -> None:
    idea = add_idea(me, CampaignId(1), 'an idea')
    done = add_idea(me, CampaignId(1), 'done', IdeaStatus.DONE)
    running = add_idea(me, CampaignId(1), 'running', IdeaStatus.CLAIMED)
    with me.session() as s:
        assert backlog_ops.set_idea_status(
            s, idea, IdeaStatus.DROPPED, reason='after the v2 evaluator', actor=ACTOR
        ) is (IdeaStatus.OPEN)
        backlog_ops.set_idea_status(s, done, IdeaStatus.OPEN, reason='try it on the new best', actor=ACTOR)
        backlog_ops.set_idea_status(s, running, IdeaStatus.DROPPED, reason='redundant', actor=ACTOR)
        s.commit()
        notes = s.scalars(select(Comment).order_by(Comment.id)).all()
        assert [(n.target_id, n.author, n.kind, n.body) for n in notes] == [
            (idea, ACTOR.signature, CommentKind.NOTE, 'open → dropped: after the v2 evaluator'),
            (done, ACTOR.signature, CommentKind.NOTE, 'done → open: try it on the new best'),
            (running, ACTOR.signature, CommentKind.NOTE, 'claimed → dropped: redundant'),
        ]
    assert [e[:2] for e in kinds(me, IDEA, idea)] == [CREATED[:2], ('idea.status', ACTOR.signature)], (
        'the trigger, once'
    )
    busy = add_idea(me, CampaignId(1), 'busy', IdeaStatus.CLAIMED)
    dropped = run_idea(me, IdeaStatus.DROPPED, RunStatus.RUNNING)
    scoring = run_idea(me, IdeaStatus.DROPPED, RunStatus.SUCCEEDED, scoring=True)
    with me.session() as s:
        for target, status, error in [
            (dropped, IdeaStatus.OPEN, f'idea#{dropped} has an open run: run#\\d+ is running; drop it'),
            (dropped, IdeaStatus.DONE, 'done is not set by hand: give open, dropped'),
            (scoring, IdeaStatus.OPEN, f'idea#{scoring} has an open run: run#\\d+ is being scored'),
            (idea, IdeaStatus.DROPPED, f'idea#{idea} is dropped already'),
            (idea, IdeaStatus.CLAIMED, 'claimed is not set by hand: give open, dropped'),
            (busy, IdeaStatus.OPEN, f'idea#{busy} is claimed: its run is open; drop it'),
            (IdeaId(99), IdeaStatus.OPEN, 'no idea#99'),
        ]:
            with pytest.raises(ops.Refused, match=error):
                backlog_ops.set_idea_status(s, target, status, reason='why', actor=ACTOR)
        with pytest.raises(ops.Refused, match='a reason is required'):
            backlog_ops.set_idea_status(s, idea, IdeaStatus.OPEN, reason=' ', actor=ACTOR)


def test_links_are_validated_kept_acyclic_and_leave_events(me: Database, campaign: Campaign, db: Database) -> None:
    a, b, c = (add_idea(me, CampaignId(1), t) for t in 'abc')
    running = add_idea(me, CampaignId(1), 'running', IdeaStatus.CLAIMED)
    with me.session() as s:
        backlog_ops.link(s, a, LinkKind.AFTER, b, reason='b lands the parser', actor=ACTOR)
        backlog_ops.link(s, b, LinkKind.AFTER, c, reason='c first', actor=ACTOR)
        s.commit()
        links = s.scalars(select(IdeaLink).order_by(IdeaLink.idea_id)).all()
        assert [(lk.idea_id, lk.kind, lk.target_id) for lk in links] == [(a, LinkKind.AFTER, b), (b, LinkKind.AFTER, c)]
    assert kinds(me, IDEA, a) == [
        CREATED,
        ('idea.linked', ACTOR.signature, IdeaLinked(kind='idea.linked', target=b, link_kind=LinkKind.AFTER)),
    ], 'a trigger'
    with me.session() as s:
        assert [c.body for c in s.scalars(select(Comment).where(Comment.target_id == a))] == [
            f'after idea#{b}: b lands the parser'
        ]
    with db.session() as s:
        s.add(Campaign(name='other', path='/other', config=s.get_one(Campaign, 1).config))
        s.commit()
    elsewhere = add_idea(me, CampaignId(2), 'elsewhere')
    with me.session() as s:
        for idea, kind, target, error in [
            (c, LinkKind.AFTER, a, f'a cycle: idea#{a} already links after idea#{c}'),
            (c, LinkKind.AFTER, c, f'idea#{c} cannot link to itself'),
            (a, LinkKind.AFTER, b, f'idea#{a} links after idea#{b} already'),
            (a, LinkKind.AFTER, elsewhere, f'idea#{elsewhere} is in another campaign'),
            (running, LinkKind.AFTER, a, f'idea#{running} is claimed: its run is open: it started without idea#{a}'),
            (a, LinkKind.AFTER, IdeaId(99), 'no idea#99'),
        ]:
            with pytest.raises(ops.Refused, match=error):
                backlog_ops.link(s, idea, kind, target, reason='why', actor=ACTOR)
        with pytest.raises(ops.Refused, match='a reason is required'):
            backlog_ops.link(s, b, LinkKind.AFTER, a, reason='', actor=ACTOR)

        backlog_ops.unlink(s, a, LinkKind.AFTER, b, reason='b was dropped', actor=ACTOR)
        s.commit()
        assert s.get_one(Idea, a).links == []
        with pytest.raises(ops.Refused, match=f'idea#{a} has no after link to idea#{b}'):
            backlog_ops.unlink(s, a, LinkKind.AFTER, b, reason='again', actor=ACTOR)
        with pytest.raises(ops.Refused, match='a reason is required'):
            backlog_ops.unlink(s, b, LinkKind.AFTER, c, reason='', actor=ACTOR)
    assert kinds(me, IDEA, a)[-1] == (
        'idea.unlinked',
        ACTOR.signature,
        IdeaLinked(kind='idea.unlinked', target=b, link_kind=LinkKind.AFTER),
    )


def test_a_raw_sql_link_is_logged_like_the_commands(me: Database, campaign: Campaign) -> None:
    a, b = add_idea(me, CampaignId(1), 'a'), add_idea(me, CampaignId(1), 'b')
    with me.session() as s:
        s.execute(text(f"INSERT INTO idea_links (idea_id, target_id, kind) VALUES ({a}, {b}, 'after')"))
        s.execute(text(f'DELETE FROM idea_links WHERE idea_id = {a}'))
        s.commit()
    assert kinds(me, IDEA, a) == [
        CREATED,
        ('idea.linked', ACTOR.signature, IdeaLinked(kind='idea.linked', target=b, link_kind=LinkKind.AFTER)),
        ('idea.unlinked', ACTOR.signature, IdeaLinked(kind='idea.unlinked', target=b, link_kind=LinkKind.AFTER)),
    ]


def test_a_track_must_be_in_the_config_and_its_move_says_why(me: Database, campaign: Campaign) -> None:
    with me.session() as s:
        two = load_campaign_config(
            Path(campaign.path),
            metrics=[
                {'name': 'score', 'direction': 'higher', 'role': 'primary', 'margin': 1},
                {'name': 'size', 'direction': 'lower', 'role': 'primary', 'margin': 1},
            ],
        )
        s.get_one(Campaign, 1).config = two
        s.commit()
    idea = add_idea(me, CampaignId(1), 'smaller')
    running = add_idea(me, CampaignId(1), 'running', IdeaStatus.CLAIMED)
    with me.session() as s:
        assert backlog_ops.set_track(s, idea, TrackName('size'), reason='it shrinks the output', actor=ACTOR) == 'score'
        s.commit()
        [note] = s.scalars(select(Comment)).all()
        assert note.body == 'track score → size: it shrinks the output'
        for target, track, error in [
            (idea, TrackName('size'), f'idea#{idea} is on track size already'),
            (idea, TrackName('speed'), "demo has tracks score, size: there is no track 'speed'"),
            (
                running,
                TrackName('size'),
                f'idea#{running} is claimed: its run is open: its commits are scored on track score',
            ),
            (IdeaId(99), TrackName('size'), 'no idea#99'),
        ]:
            with pytest.raises(ops.Refused, match=error):
                backlog_ops.set_track(s, target, track, reason='why', actor=ACTOR)
        with pytest.raises(ops.Refused, match='a reason is required'):
            backlog_ops.set_track(s, idea, TrackName('score'), reason='', actor=ACTOR)
        with pytest.raises(ops.Refused, match='demo has tracks score, size: name one with --track'):
            backlog_ops.track_for(s.get_one(Campaign, 1), None)
    assert [(k, a, p) for k, a, p in kinds(me, IDEA, idea) if k == 'idea.track'] == [
        ('idea.track', ACTOR.signature, IdeaTrackChanged(old=TrackName('score'), new=TrackName('size')))
    ]


def parents(s: Session) -> dict[str, str | None]:
    """Each campaign's name, with the name of its parent."""
    names = {p.id: p.name for p in s.scalars(select(Campaign))}
    return {p.name: names.get(p.parent_id) if p.parent_id else None for p in s.scalars(select(Campaign))}


def test_a_campaign_has_one_parent_never_itself_or_a_descendant_and_each_change_is_an_event(me: Database) -> None:
    with me.session() as s:
        a = s.get_one(Campaign, 1)
        b, c, d = (Campaign(name=name, path=f'/{name}', config=a.config) for name in 'bcd')
        s.add_all([a, b, c, d])
        s.flush()
        assert (a.type, a.parent_id) == ('optimization', None), 'an optimization campaign, a root'
        assert ops.set_parent(s, b.id, a.id) is b
        ops.set_parent(s, c.id, b.id)
        ops.set_parent(s, d.id, a.id)
        assert parents(s) == {'demo': None, 'b': 'demo', 'c': 'b', 'd': 'demo'}
        for child, parent in ((a, c), (a, a)):
            with pytest.raises(ops.Refused, match=f'^{child.name} cannot be under {parent.name}: .*its descendants'):
                ops.set_parent(s, child.id, parent.id)
        assert a.parent_id is None and s.get(Campaign, c.id) is c, 'a refused change leaves the session usable'
        with pytest.raises(ops.Refused, match='^c is under b already'):
            ops.set_parent(s, c.id, b.id)
        with pytest.raises(ops.Refused, match='^no campaign#9'):
            ops.set_parent(s, c.id, CampaignId(9))
        ops.set_parent(s, c.id, None)
        with pytest.raises(ops.Refused, match='^c is a root already'):
            ops.set_parent(s, c.id, None)
        s.commit()
        assert parents(s) == {'demo': None, 'b': 'demo', 'c': None, 'd': 'demo'}
        changes = in_order(s, CampaignParentChanged)
        assert [(e.target_id, e.actor, change) for e, change in changes] == [
            (b.id, ACTOR.signature, CampaignParentChanged(old=None, new=a.id)),
            (c.id, ACTOR.signature, CampaignParentChanged(old=None, new=b.id)),
            (d.id, ACTOR.signature, CampaignParentChanged(old=None, new=a.id)),
            (c.id, ACTOR.signature, CampaignParentChanged(old=b.id, new=None)),
        ], 'the trigger logs each change, and a refused one leaves no event'
        with pytest.raises(IntegrityError, match='FOREIGN KEY'):
            s.execute(text('DELETE FROM campaigns WHERE id = :id'), {'id': a.id})


def test_the_database_refuses_a_cycle_of_parents(me: Database, tmp_path: Path) -> None:
    with closing(sqlite3.connect(tmp_path / 'piloop.db')) as con:
        con.execute('PRAGMA foreign_keys = ON')
        con.execute("INSERT INTO campaigns (name, path, config) VALUES ('b', '/b', '{}')")
        con.execute('UPDATE campaigns SET parent_id = 1 WHERE id = 2')
        for statement in (
            'UPDATE campaigns SET parent_id = 2 WHERE id = 1',
            'UPDATE campaigns SET parent_id = id WHERE id = 1',
            "INSERT INTO campaigns (id, name, path, config, parent_id) VALUES (9, 'c', '/c', '{}', 9)",
        ):
            with pytest.raises(sqlite3.IntegrityError, match='^campaigns.parent_id: a campaign cannot be under itself'):
                con.execute(statement)
        with pytest.raises(sqlite3.IntegrityError, match='FOREIGN KEY'):
            con.execute('UPDATE campaigns SET parent_id = 7 WHERE id = 1')
        assert con.execute('SELECT id, type, parent_id FROM campaigns').fetchall() == [
            (1, 'optimization', None),
            (2, 'optimization', 1),
        ]


def test_a_reference_measured_by_hand_enters_the_pool_and_may_be_the_best(me: Database, campaign: Campaign) -> None:
    """A composition made by hand is imported, then measured: no run has to commit it, and nothing keeps it."""
    repo = Repo(campaign.path)
    repo.git.checkout('-q', '--detach')
    (Path(campaign.path) / 'composed.txt').write_text('c\n')
    repo.git.add('-A')
    repo.git.commit('-qm', 'composed')
    composed_sha = repo.head.commit.hexsha
    repo.git.checkout('-q', 'main')
    with me.session() as s:
        proj = s.get_one(Campaign, 1)
        composed = import_reference(s, proj, GitRepository(repo, PILOOP), composed_sha, summary=None, actor=ACTOR).id
        s.commit()
        pool = Pool.of(s, proj, proj.config.all_tracks[0])
        assert pool.standing(composed).told() == 'not scored', 'imported, not measured yet'
        added = evaluation_ops.add_measurement(
            s, composed, evaluation_api.Report(metrics={'score': 2.0, 'size': 1.0}), actor=ACTOR
        )
        s.commit()
        assert added.standing == 'score 2 over 1 eval: the best of the score track now, rank 1 of 2'
        best = best_of(s, s.get_one(Campaign, 1))
        assert best is not None and (best.id, best.origin) == (composed, Origin.REFERENCE)
