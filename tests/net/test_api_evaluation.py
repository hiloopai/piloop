"""The API of the evaluation queue and of candidates."""

from datetime import timedelta
from pathlib import Path

import pytest

from piloop.actor import System
from piloop.api.client import Refused
from piloop.api.contract import (
    Checkout,
    Problem,
)
from piloop.db.models import (
    Campaign,
    Pause,
    utcnow,
)
from piloop.db.session import Database
from piloop.plugins.campaign_type.kit.candidates.ids import CandidateId
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.evaluation.ids import RungName
from piloop.plugins.campaign_type.kit.evaluation.tables import Evaluation, Measurement
from piloop.plugins.campaign_type.kit.evaluation.wire import CancelledBy, RungResult, RunningSince
from piloop.plugins.campaign_type.kit.verdicts.wire import RemeasureBody
from piloop.settings import Settings
from tests.conftest import EveryClient, serving_api


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
