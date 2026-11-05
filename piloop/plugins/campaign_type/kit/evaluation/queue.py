"""The evaluation queue (``evaluations``): the evaluations that the daemon runs now or next, one row each.

The daemon submits each evaluation to its executor when one of the executor's slots is free (``Executor.capacity``),
in this order (``waiting``), whichever the executor. A free slot goes to the
campaign with the fewest running evaluations, and in that campaign to its baseline first, then to the oldest request. So a
campaign with many requests never holds back the requests of another, and inside a campaign each request waits only
for the ones before it. The queue is in the database: ``view`` answers it, and ``cancel`` takes from it.

A remeasure that someone asked for (``ledger.request_remeasure``) enters the queue only when the daemon gets to it, one
per campaign at a time. Until then ``requested`` lists its pending request, so ``piloop eval queue`` shows it from the
moment it is asked.
"""

from collections import Counter
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from piloop.actor import Actor
from piloop.db import requests
from piloop.db.models import NOT_RETIRED, Campaign, Event
from piloop.ids import CampaignId, TargetRef
from piloop.plugins.campaign_type.kit.candidates.ids import CANDIDATE, CandidateId, Origin
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.evaluation import measure as ledger
from piloop.plugins.campaign_type.kit.evaluation.ids import EvaluatorId
from piloop.plugins.campaign_type.kit.evaluation.tables import Evaluation
from piloop.plugins.campaign_type.kit.evaluation.wire import (
    CancelledBy,
    EvaluationQueue,
    EvaluationState,
    QueuedEvaluation,
    RunningSince,
    WaitingSince,
)


def request(s: Session, candidate_id: CandidateId, evaluator_id: EvaluatorId) -> tuple[Evaluation, bool]:
    """Queue an evaluation of the candidate under the evaluator, or find the one that waits or runs. The caller commits.

    Returns ``(evaluation, created)``. The requests for one candidate and evaluator share one evaluation, so a tree is
    evaluated once.
    """
    found = s.scalar(
        select(Evaluation).where(Evaluation.candidate_id == candidate_id, Evaluation.evaluator_id == evaluator_id)
    )
    if found is not None:
        return found, False
    evaluation = Evaluation(candidate_id=candidate_id, evaluator_id=evaluator_id)
    s.add(evaluation)
    s.flush()
    return evaluation, True


def _first(s: Session, campaign_id: CampaignId) -> CandidateId | None:
    """The id of the campaign's baseline, its first candidate: runs wait for it."""
    first = select(Candidate.id).where(Candidate.campaign_id == campaign_id).order_by(Candidate.id).limit(1)
    return s.scalar(first)


def backlog(s: Session) -> list[Evaluation]:
    """Return the evaluations that wait for a slot under an evaluator that their campaign still measures with, by id.

    One of a retired evaluator (``ledger.retired``) is no backlog: ``withdraw`` takes it from the queue.
    """
    return [e for e in _unstarted(s) if not ledger.retired(s, e.evaluator_id)]


def withdraw(s: Session) -> int:
    """Take each evaluation that waits under a retired evaluator (``ledger.retired``) from the queue.

    The caller commits. Return how many. Each of its requests finds it gone with no measurement, and asks the current
    evaluator. One that runs finishes under its own evaluator, and its measurement keeps it.
    """
    gone = [e for e in _unstarted(s) if ledger.retired(s, e.evaluator_id)]
    for evaluation in gone:
        s.delete(evaluation)
    return len(gone)


def _unstarted(s: Session) -> list[Evaluation]:
    return list(s.scalars(select(Evaluation).where(Evaluation.started_at.is_(None)).order_by(Evaluation.id)))


def waiting(s: Session) -> list[Evaluation]:
    """Return the evaluations that wait for a slot, in the order in which they get one if none ends meanwhile.

    Only the ``backlog``: a retired evaluator's never gets one.
    """
    rows = backlog(s)
    campaign = {e.id: e.candidate.campaign_id for e in rows}
    running = Counter(
        s.scalars(
            select(Candidate.campaign_id)
            .join(Evaluation, Evaluation.candidate_id == Candidate.id)
            .where(Evaluation.started_at.is_not(None))
        )
    )
    firsts = {p: _first(s, p) for p in set(campaign.values())}
    order: list[Evaluation] = []
    while rows:
        nxt = min(rows, key=lambda e: (running[campaign[e.id]], e.candidate_id != firsts[campaign[e.id]], e.id))
        order.append(nxt)
        rows.remove(nxt)
        running[campaign[nxt.id]] += 1
    return order


def listing(s: Session) -> list[Evaluation]:
    """Return the running evaluations, oldest start first, then the waiting ones in the order of ``waiting``."""
    running = select(Evaluation).where(Evaluation.started_at.is_not(None)).order_by(Evaluation.started_at)
    return [*s.scalars(running), *waiting(s)]


def requested(s: Session) -> list[tuple[Candidate, datetime]]:
    """Return each remeasure that someone asked for and that has no evaluation in the queue yet, with when it was asked.

    In campaign order, then oldest request first (``ledger.requested``). An evaluation that someone cancelled does not
    answer a request that came after the cancel, so the request is listed while that evaluation stops.
    """
    queued = set(
        s.execute(
            select(Evaluation.candidate_id, Evaluation.evaluator_id).where(Evaluation.cancelled_by.is_(None))
        ).all()
    )
    return [
        (candidate, asked.at)
        for campaign in s.scalars(select(Campaign).where(NOT_RETIRED).order_by(Campaign.id))
        for candidate, asked in ledger.requested(s, campaign)
        if (candidate.id, campaign.evaluator_id) not in queued
    ]


def view(s: Session) -> EvaluationQueue:
    """``listing``, then each remeasure of ``requested`` as waiting, as the API answers them.

    Each names its evaluator, and whether that is retired (``ledger.retired``): what runs finishes under its own. A
    requested remeasure gets its campaign's current one.
    """
    campaigns = {c.id: c for c in s.scalars(select(Campaign))}
    rows = [(e.candidate, e.evaluator_id, _state(e)) for e in listing(s)]
    for c, asked in requested(s):
        current = campaigns[c.campaign_id].evaluator_id
        assert current is not None  # a campaign has an evaluator from its ``piloop init``
        rows.append((c, current, WaitingSince(since=asked)))
    return EvaluationQueue(
        evaluations=[
            QueuedEvaluation(
                candidate=c.id,
                campaign=campaigns[c.campaign_id].name,
                summary=c.summary,
                evaluator=evaluator,
                retired=ledger.retired(s, evaluator),
                state=state,
            )
            for c, evaluator, state in rows
        ]
    )


def _state(e: Evaluation) -> EvaluationState:
    match e.cancelled_by, e.started_at:
        case str() as by, _:
            return CancelledBy(by=by)
        case None, datetime() as since:
            return RunningSince(since=since)
        case _:
            return WaitingSince(since=e.created_at)


def cancel(s: Session, candidate_id: CandidateId, actor: Actor) -> int:
    """Cancel what the candidate waits for: its evaluations that wait or run, or its remeasure that is not queued yet.

    Signed by ``actor``. Return how many it cancelled. The caller commits.

    A cancel records no measurement. A run's candidate that has none under the current evaluator becomes ``skipped``,
    so no daemon measures it again unless someone asks (``piloop candidate remeasure``), even after a restart. The
    baseline is asked for again when a run needs it. A pending remeasure request is answered. A waiting evaluation leaves the queue. A running one gets
    ``cancelled_by``, and the daemon stops its evaluator and records nothing.
    """
    found = s.scalars(
        select(Evaluation).where(Evaluation.candidate_id == candidate_id, Evaluation.cancelled_by.is_(None))
    ).all()
    asked = requests.pending(s, ledger.RemeasureRequested, Event.target == TargetRef(CANDIDATE, candidate_id))
    if not found and not asked:
        return 0
    candidate = s.get_one(Candidate, candidate_id)
    for ask, _ in asked:
        requests.answer(s, ask)
    current = s.get_one(Campaign, candidate.campaign_id).evaluator_id
    if candidate.origin is Origin.RUN and not any(m.evaluator_id == current for m in candidate.measurements):
        candidate.origin = Origin.SKIPPED
    for evaluation in found:
        if evaluation.started_at is None:  # its executor finds it gone when a slot comes (``Results.started``)
            s.delete(evaluation)
        else:
            evaluation.cancelled_by = actor.signature
    return max(len(found), 1)
