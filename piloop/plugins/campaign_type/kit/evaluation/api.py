"""The evaluation's routes: a candidate, a measurement by hand, the queue, an evaluation's cancel, and submissions."""

import time
from contextlib import closing
from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from piloop.actor import Agent
from piloop.api.contract import (
    Problem,
)
from piloop.campaign.campaigns import repository
from piloop.daemon.api import REFUSALS_DOCUMENTED, Acting, Api, Reading, Writing, actor_access, reader
from piloop.db import feed as db_feed
from piloop.db.models import AgentSession, Campaign
from piloop.db.session import Database
from piloop.net import Refusal
from piloop.net.server import changed
from piloop.plugins.campaign_type.kit.candidates.api import AddressedCandidate, ViewedCandidate
from piloop.plugins.campaign_type.kit.candidates.ids import CandidateId
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.candidates.wire import CandidateAnswer
from piloop.plugins.campaign_type.kit.evaluation import ops as evaluation_ops, queue
from piloop.plugins.campaign_type.kit.evaluation.measure import metrics, rung_results
from piloop.plugins.campaign_type.kit.evaluation.submissions import SubmitRefused, submit as submit_version
from piloop.plugins.campaign_type.kit.evaluation.wire import (
    CandidateDetail,
    EvaluationQueue,
    MeasurementAdded,
    Report,
    Submission,
    SubmissionQuery,
    SubmitBody,
)
from piloop.plugins.campaign_type.kit.runs.tables import Run, run_of
from piloop.plugins.campaign_type.kit.verdicts.verdict import PENDING, candidate_view, label
from piloop.plugins.vcs.contract import UnknownRevision

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
    """Cancel a candidate's evaluation, waiting or running, or its requested remeasure. A cancel is not a verdict.

    A run's candidate that waits for its verdict is ``skipped``. A baseline that never measured pauses every
    measurement of its campaign (``ops.hold_on_cancelled_baseline``).
    """
    if not queue.cancel(s, candidate.id, me):
        raise Refusal(
            HTTPStatus.CONFLICT,
            Problem.REFUSED,
            f'candidate#{candidate.id} has no evaluation that waits or runs: see `piloop eval queue`',
        )
    evaluation_ops.hold_on_cancelled_baseline(s, candidate, me)
    s.commit()
    with db_feed.snapshot(s) as cursor:
        return CandidateAnswer(cursor=cursor, candidate=candidate_view(candidate))


@router.post('/submissions', operation_id='submit', dependencies=[Depends(actor_access)])
def submit(s: Writing, me: Acting, body: SubmitBody) -> Submission:
    """Submit a version of the caller's run, an implementer's: the daemon scores and judges it now."""
    match me:
        case Agent(session=session) if (agent := s.get(AgentSession, session)) is not None and (
            run_id := run_of(agent)
        ) is not None:
            run = s.get_one(Run, run_id)
        case _:
            raise Refusal(HTTPStatus.CONFLICT, Problem.REFUSED, f'{me} does no run: only an implementer submits')
    with closing(repository(run.campaign)) as repo:
        try:
            candidate = submit_version(s, run, repo.version(body.revision), repo, me)
        except UnknownRevision as e:
            why = f'the campaign repo has no {body.revision}: submit a commit of your checkout ({e})'
            raise Refusal(HTTPStatus.CONFLICT, Problem.REFUSED, why) from e
        except SubmitRefused as e:
            raise Refusal(HTTPStatus.CONFLICT, Problem.REFUSED, str(e)) from e
    s.commit()
    return _submission(s, candidate)


@router.get('/submissions/{candidate}', operation_id='submission')
async def submission(
    request: Request, api: Api, candidate: CandidateId, query: Annotated[SubmissionQuery, Query()]
) -> Submission:
    """A submission's verdict and score. With wait, a long-poll that answers once the verdict is in."""
    deadline, seen = time.monotonic() + query.wait, 0
    found = await run_in_threadpool(_read, api.db, candidate)
    while (
        found.verdict == PENDING
        and (left := deadline - time.monotonic()) > 0
        and (woke := await changed(request, api.db.changed, seen, left)) is not None
    ):
        seen = woke
        found = await run_in_threadpool(_read, api.db, candidate)
    return found


def _read(db: Database, candidate: CandidateId) -> Submission:
    with db.session() as s:
        if (found := s.get(Candidate, candidate)) is None:
            raise Refusal(HTTPStatus.NOT_FOUND, Problem.UNKNOWN_CANDIDATE, f'no candidate#{candidate}')
        return _submission(s, found)


def _submission(s: Session, candidate: Candidate) -> Submission:
    """What its implementer reads of a submission: the verdict, and the primary of the campaign's first track."""
    campaign = s.get_one(Campaign, candidate.campaign_id)
    value = metrics(candidate, campaign.evaluator_id).get(campaign.config.all_tracks[0].primary.name)
    return Submission(candidate=candidate.id, verdict=label(candidate), score=value)
