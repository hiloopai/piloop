"""The evaluation's routes: a candidate, a measurement by hand, the queue, an evaluation's cancel, and submissions."""

import time
from contextlib import closing
from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import ValidationError
from sqlalchemy.orm import Session

from piloop.api.contract import (
    Problem,
)
from piloop.campaign.campaigns import repository
from piloop.daemon.api import REFUSALS_DOCUMENTED, Acting, Api, Reading, Writing, actor_access, reader
from piloop.db import feed as db_feed
from piloop.db.models import Campaign
from piloop.db.session import Database
from piloop.net import Refusal
from piloop.net.server import changed
from piloop.plugins.campaign_type.kit.candidates.api import AddressedCandidate, ViewedCandidate
from piloop.plugins.campaign_type.kit.candidates.ids import CandidateId
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.candidates.wire import CandidateAnswer
from piloop.plugins.campaign_type.kit.evaluation import ops as evaluation_ops, queue, submissions
from piloop.plugins.campaign_type.kit.evaluation.measure import metrics, rung_results
from piloop.plugins.campaign_type.kit.evaluation.wire import (
    CandidateDetail,
    EvaluationQueue,
    MeasurementAdded,
    Report,
    Submission,
    SubmissionQuery,
    SubmitBody,
)
from piloop.plugins.campaign_type.kit.runs.tables import Run
from piloop.plugins.campaign_type.kit.verdicts.verdict import PENDING, candidate_view, label
from piloop.plugins.cas.contract import Cas
from piloop.plugins.vcs.contract import Repository, Transfer, UnknownRevision, VcsError, Version
from piloop.store.tree import Missing

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
def submit(s: Writing, me: Acting, api: Api, body: SubmitBody) -> Submission:
    """Submit a version of the caller's run, an implementer's: the daemon scores and judges it now.

    A version that the run submitted already answers as it is, also after the run ended: a retry. Any other is refused
    before its ``blob``, from a worker elsewhere, is taken into the repo of record.
    """
    run = submissions.run_of_agent(s, me)
    with closing(repository(run.campaign)) as repo:
        version = _version(repo, body.revision)
        if version is not None and (own := submissions.resubmitted(s, run, version)) is not None:
            return _submission(s, own)
        submissions.refuse(s, run)
        if version is None:
            version = _received(repo, api.cas, run, body)
        candidate = submissions.submit(s, run, version, repo, me)
    s.commit()
    return _submission(s, candidate)


def _version(repo: Repository, revision: str) -> Version | None:
    """The version that ``revision`` names in the repo of record, or None when it lacks it."""
    try:
        return repo.version(revision)
    except UnknownRevision:
        return None


def _received(repo: Repository, cas: Cas, run: Run, body: SubmitBody) -> Version:
    """Take the version of ``body`` into the repo of record from its blob, and return it.

    Raises:
        Refusal: there is no blob, it is no address, the CAS lacks it, or the repo cannot take it.
    """
    if body.blob is None:
        why = f'the campaign repo has no {body.revision}: submit a commit of your checkout'
        raise Refusal(HTTPStatus.CONFLICT, Problem.REFUSED, why)
    try:
        sent = Transfer.model_validate({'blob': body.blob, 'head': body.revision})
    except ValidationError as e:
        raise Refusal(HTTPStatus.CONFLICT, Problem.REFUSED, f'{body.blob} is no blob address: {e}') from e
    try:
        repo.receive(cas, sent, run.base.revision)
        return repo.version(body.revision)
    except (Missing, VcsError) as e:  # UnknownRevision is one: the blob lacked the version
        raise Refusal(HTTPStatus.CONFLICT, Problem.REFUSED, f'{body.revision} did not come: {e}') from e


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
