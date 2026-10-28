"""The evaluation's routes: a candidate, a measurement by hand, the evaluation queue, and an evaluation's cancel."""

from http import HTTPStatus

from fastapi import APIRouter, Depends

from piloop.api.contract import (
    Problem,
)
from piloop.daemon.api import REFUSALS_DOCUMENTED, Acting, Reading, Writing, actor_access, reader
from piloop.db import feed as db_feed
from piloop.db.models import Campaign
from piloop.net import Refusal
from piloop.plugins.campaign_type.kit.candidates.api import AddressedCandidate, ViewedCandidate
from piloop.plugins.campaign_type.kit.candidates.wire import CandidateAnswer
from piloop.plugins.campaign_type.kit.evaluation import ops as evaluation_ops, queue
from piloop.plugins.campaign_type.kit.evaluation.measure import metrics, rung_results
from piloop.plugins.campaign_type.kit.evaluation.wire import CandidateDetail, EvaluationQueue, MeasurementAdded, Report
from piloop.plugins.campaign_type.kit.verdicts.verdict import candidate_view

router = APIRouter(prefix='/v1', dependencies=[Depends(reader)], responses=REFUSALS_DOCUMENTED)
"""The evaluation's routes, beside the API's."""


@router.get('/candidates/{candidate}', operation_id='candidate')
def candidate(s: Reading, candidate: ViewedCandidate) -> CandidateDetail:
    """A candidate: its verdict, its metrics beside its parent's, and each rung of the ladder that it measured."""
    evaluator_id = s.get_one(Campaign, candidate.campaign_id).evaluator_id
    parent = candidate.parent
    return CandidateDetail(
        candidate=candidate_view(candidate),
        revision=candidate.revision,
        parent=candidate.parent_id,
        run=candidate.run_id,
        evaluator=evaluator_id,
        metrics=metrics(candidate, evaluator_id),
        parent_metrics=metrics(parent, evaluator_id) if parent is not None else {},
        rungs=rung_results(candidate, evaluator_id),
    )


@router.post(
    '/candidates/{candidate}/measurements', operation_id='add_measurement', dependencies=[Depends(actor_access)]
)
def add_measurement(s: Writing, me: Acting, candidate: AddressedCandidate, body: Report) -> MeasurementAdded:
    """Record a measurement of a candidate under the campaign's current evaluator, and have its verdict judged again."""
    added = evaluation_ops.add_measurement(s, candidate.id, body, actor=me)
    s.commit()
    with db_feed.snapshot(s) as cursor:
        return MeasurementAdded(
            cursor=cursor, measurement=added.measurement.id, rejudged=added.rejudged, effect=added.effect
        )


@router.get('/evaluations', operation_id='evaluations')
def evaluations(s: Reading) -> EvaluationQueue:
    """The evaluation queue: the running evaluations, then the waiting ones in the order in which they get a slot."""
    return queue.view(s)


@router.post(
    '/candidates/{candidate}/evaluation/cancel',
    operation_id='cancel_evaluation',
    dependencies=[Depends(actor_access)],
)
def cancel_evaluation(s: Writing, me: Acting, candidate: AddressedCandidate) -> CandidateAnswer:
    """Cancel a candidate's evaluation, waiting or running, or its requested remeasure: it measures a crash at once.

    A baseline that never measured pauses its campaign, as when it fails (``ops.hold_on_failed_baseline``).
    """
    if not queue.cancel(s, candidate.id, me):
        raise Refusal(
            HTTPStatus.CONFLICT,
            Problem.REFUSED,
            f'candidate#{candidate.id} has no evaluation that waits or runs: see `piloop eval queue`',
        )
    evaluation_ops.hold_on_failed_baseline(s, candidate, None)
    s.commit()
    with db_feed.snapshot(s) as cursor:
        return CandidateAnswer(cursor=cursor, candidate=candidate_view(candidate))
