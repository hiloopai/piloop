"""The API of the evaluation queue and of candidates."""

from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from piloop.actor import Agent, System
from piloop.api.client import Refused
from piloop.api.contract import (
    Checkout,
    Problem,
)
from piloop.db.models import (
    AgentSession,
    AgentSessionState,
    Campaign,
    Pause,
    utcnow,
)
from piloop.db.session import Database
from piloop.ids import RoleName
from piloop.net import tokens
from piloop.plugins.campaign_type.kit.candidates.ids import CandidateId, Outcome
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.evaluation.ids import RungName
from piloop.plugins.campaign_type.kit.evaluation.tables import Evaluation, Measurement
from piloop.plugins.campaign_type.kit.evaluation.wire import (
    CancelledBy,
    RungResult,
    RunningSince,
    Submission,
    SubmissionQuery,
    SubmitBody,
)
from piloop.plugins.campaign_type.kit.runs.tables import working_on
from piloop.plugins.campaign_type.kit.verdicts.verdict import write
from piloop.plugins.campaign_type.kit.verdicts.wire import RemeasureBody
from piloop.settings import Settings
from tests.conftest import EveryClient, serving_api
from tests.daemon.test_run_controller import set_config
from tests.daemon.test_submissions import commit, running, worktree


def test_the_evaluation_queue_answers_each_state_and_a_cancel_refuses_a_candidate_that_waits_for_nothing(
    db: Database, campaign: Campaign, cfg: Settings
) -> None:
    then = utcnow() - timedelta(hours=1)
    with db.session() as s:
        s.add(Evaluation(candidate_id=1, evaluator_id=campaign.evaluator_id, created_at=then, started_at=then))
        s.commit()
    with serving_api(cfg), EveryClient.on(cfg.socket_file, System.ORCHESTRATOR) as client:
        [running] = client.evaluations().evaluations
        assert (running.candidate, running.campaign, running.summary) == (1, 'demo', 'baseline')
        assert running.state == RunningSince(since=then)
        assert client.cancel_evaluation(CandidateId(1)).candidate.id == 1
        [stopping] = client.evaluations().evaluations
        assert stopping.state == CancelledBy(by='orchestrator'), 'it runs until the daemon stops its evaluator'
        with pytest.raises(
            Refused, match='^candidate#1 has no evaluation that waits or runs: see `piloop eval queue`$'
        ) as twice:
            client.cancel_evaluation(CandidateId(1))
        assert twice.value.error == Problem.REFUSED
        with pytest.raises(Refused, match='^no candidate#9$') as unknown:
            client.cancel_evaluation(CandidateId(9))
        assert unknown.value.error == Problem.UNKNOWN_CANDIDATE
    with db.session() as s:
        assert s.get_one(Campaign, campaign.id).paused is None, 'a baseline that measured before holds nothing'


def test_a_candidate_reads_with_its_metrics_beside_its_parents_and_each_rung_of_its_climb(
    db: Database, campaign: Campaign, cfg: Settings
) -> None:
    stop = 'score 2 vs 3 (-1; margin 0.5)'
    with db.session() as s:
        c = Candidate(campaign_id=campaign.id, parent_id=1, tree_sha='t2', revision='r2', summary='screened')
        s.add(c)
        s.flush()
        on = {'candidate_id': c.id, 'evaluator_id': campaign.evaluator_id}
        s.add_all(
            [
                Measurement(**on, rung=RungName('smoke'), metrics={'score': 1.0}, seconds=0.5),
                Measurement(**on, rung=RungName('screen'), metrics={'score': 3.0, 'size': 4.0}, seconds=1.0),
                Measurement(**on, rung=RungName('screen'), metrics={}, seconds=9.0, error='boom'),
                Measurement(
                    **on, rung=RungName('screen'), metrics={'score': 2.0, 'size': 6.0}, seconds=3.0, stopped=stop
                ),
            ]
        )
        s.commit()
        candidate_id = c.id
    with serving_api(cfg), EveryClient.on(cfg.socket_file, System.HUMAN) as client:
        shown = client.candidate(candidate_id)
        assert (shown.candidate.id, shown.candidate.label, shown.revision, shown.parent, shown.run) == (
            candidate_id,
            'pending',
            'r2',
            1,
            None,
        )
        assert (shown.evaluator, shown.metrics, shown.parent_metrics) == (
            campaign.evaluator_id,
            {},
            {'score': 1.0, 'size': 5.0},
        )
        assert shown.rungs == [
            RungResult(rung=RungName('smoke'), metrics={'score': 1.0}, seconds=0.5, measurements=1, stopped=None),
            RungResult(
                rung=RungName('screen'), metrics={'score': 2.5, 'size': 5.0}, seconds=2.0, measurements=2, stopped=stop
            ),
        ], 'the means of the successful measurements of each rung, and why the latest one stopped there'
        assert client.candidate(CandidateId(1)).rungs == []
        with pytest.raises(Refused, match='^no candidate#9$') as unknown:
            client.candidate(CandidateId(9))
        assert unknown.value.error == Problem.UNKNOWN_CANDIDATE


def test_a_cancelled_evaluation_of_a_baseline_that_never_measured_pauses_its_measurements(
    db: Database, campaign_path: Path, cfg: Settings
) -> None:
    with serving_api(cfg), EveryClient.on(cfg.socket_file, System.HUMAN) as client:
        added = client.register_campaign(Checkout(path=str(campaign_path)))
        client.remeasure(added.baseline, RemeasureBody())
        client.cancel_evaluation(added.baseline)
    with db.session() as s:
        held = s.get_one(Campaign, added.campaign.id)
        assert (held.paused, held.paused_reason) == (
            Pause.ALL,
            'human cancelled the evaluation of the baseline; `piloop resume demo` measures it again',
        ), 'a cancel records nothing: the pause holds the measurement that a head without a score asks for'
        assert s.get_one(Candidate, added.baseline).measurements == []


def test_an_implementer_submits_a_commit_of_its_run_and_reads_only_its_verdict_and_score(
    db: Database, campaign: Campaign, cfg: Settings, tmp_path: Path
) -> None:
    set_config(db, campaign, evaluator={'scoring': 'submit'})
    run_id = running(db, campaign)
    revision = commit(worktree(campaign, tmp_path), 'base\na\n')
    with db.session() as s:
        agent = s.scalars(select(AgentSession).where(working_on(run_id))).one()
        agent.state = AgentSessionState.RUNNING
        me = Agent(role=RoleName('implementer'), session=agent.id)
        token = tokens.mint_agent(s, me)
        s.commit()
    with serving_api(cfg):
        with EveryClient.on(cfg.socket_file, me, token) as client:
            answer = client.submit(SubmitBody(revision=revision))
            assert (answer.verdict, answer.score) == ('pending', None)
            with pytest.raises(Refused, match='^the campaign repo has no f00: submit a commit of your checkout'):
                client.submit(SubmitBody(revision='f00'))
            assert client.submission(answer.candidate, SubmissionQuery(wait=0.2)).verdict == 'pending', 'no verdict yet'
            again = client.submit(SubmitBody(revision=revision))
            assert again.candidate == answer.candidate, 'its own tree again, while it waits'
            with pytest.raises(Refused, match=r'^its tree is candidate#1 already \(keep\): change something first$'):
                client.submit(SubmitBody(revision=base_revision(db, campaign)))
        with db.session() as s:
            candidate = s.get_one(Candidate, answer.candidate)
            s.add(Measurement(candidate_id=candidate.id, evaluator_id=campaign.evaluator_id, metrics={'score': 2.0}))
            write(candidate, Outcome.KEEP, cause=None, reason='+1', by=System.DAEMON)
            s.commit()
        with EveryClient.on(cfg.socket_file, System.HUMAN) as client:
            assert client.submission(answer.candidate, SubmissionQuery(wait=1)) == Submission(
                candidate=answer.candidate, verdict='keep', score=2.0
            )
            with pytest.raises(Refused, match='^human does no run: only an implementer submits$'):
                client.submit(SubmitBody(revision=revision))
            with pytest.raises(Refused, match='^no candidate#99$'):
                client.submission(CandidateId(99), SubmissionQuery())


def base_revision(db: Database, campaign: Campaign) -> str:
    with db.session() as s:
        return (
            s.scalars(
                select(Candidate.revision).where(Candidate.campaign_id == campaign.id).order_by(Candidate.id)
            ).first()
            or ''
        )
