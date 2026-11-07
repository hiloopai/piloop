"""The evaluation queue (``evaluations``): the evaluations that the daemon runs now or next, one row each.

The daemon submits each evaluation to its executor when one of the executor's slots is free (``Executor.capacity``),
in this order (``waiting``), whichever the executor. A free slot goes to the
campaign with the fewest running evaluations, and in that campaign to its baseline first, then to the oldest request. So a
campaign with many requests never holds back the requests of another, and inside a campaign each request waits only
for the ones before it. The queue is in the database: ``view`` answers it, and ``cancel`` takes from it.

Every write of a row is here, one function for each move, each a guarded statement: ``request`` (absent → waiting),
``start`` (waiting → running), ``unmade`` (waiting → absent), ``lost`` (running → waiting, or absent when it was
cancelled), ``cancel`` (waiting → absent, running → running and cancelled), ``withdraw`` (waiting under a retired
evaluator → absent), ``done`` (running → absent) and ``clear`` (every row, when a daemon starts). Only a running row is
cancelled (``EVALUATION_CHECKS``).

A remeasure that someone asked for (``ledger.request_remeasure``) enters the queue only when the daemon gets to it, one
per campaign at a time, as a row of its own (``Evaluation.ask``). Until then ``requested`` lists its pending request, so
``piloop eval queue`` shows it from the moment it is asked.
"""

from collections import Counter
from datetime import datetime

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session

from piloop.actor import Actor
from piloop.db import requests
from piloop.db.models import NOT_RETIRED, Campaign, Event, utcnow
from piloop.ids import CampaignId, TargetRef
from piloop.plugins.campaign_type.kit.candidates.ids import CANDIDATE, CandidateId
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.evaluation import measure as ledger
from piloop.plugins.campaign_type.kit.evaluation.ids import EvaluatorId
from piloop.plugins.campaign_type.kit.evaluation.tables import Cancel, Evaluation
from piloop.plugins.campaign_type.kit.evaluation.wire import (
    CancelledBy,
    EvaluationQueue,
    EvaluationState,
    QueuedEvaluation,
    RunningSince,
    WaitingSince,
)


def request(
    s: Session, candidate_id: CandidateId, evaluator_id: EvaluatorId, ask: int | None = None
) -> tuple[Evaluation, bool]:
    """Queue an evaluation of the candidate under the evaluator, or find the one that waits or runs. The caller commits.

    Returns ``(evaluation, created)``. The requests for one candidate and evaluator share one evaluation, so a tree is
    evaluated once. A new one answers ``ask``, the remeasure that it is made for (``Evaluation.ask``); one that is found
    keeps its own.
    """
    found = s.scalar(
        select(Evaluation).where(Evaluation.candidate_id == candidate_id, Evaluation.evaluator_id == evaluator_id)
    )
    if found is not None:
        return found, False
    evaluation = Evaluation(candidate_id=candidate_id, evaluator_id=evaluator_id, ask=ask)
    s.add(evaluation)
    s.flush()
    return evaluation, True


def start(s: Session, evaluation_id: int) -> bool:
    """Give a waiting evaluation its slot: it runs from now. False when it no longer waits. The caller commits."""
    moved = s.execute(
        update(Evaluation)
        .where(Evaluation.id == evaluation_id, Evaluation.started_at.is_(None))
        .values(started_at=utcnow())
        .returning(Evaluation.id)
        .execution_options(synchronize_session='fetch')
    ).first()
    return moved is not None


def unmade(s: Session, evaluation_id: int) -> bool:
    """A waiting evaluation whose job its request could not make leaves the queue. False when it does not wait.

    The caller commits, with why (``InfraFailed``).
    """
    gone = s.execute(
        delete(Evaluation)
        .where(Evaluation.id == evaluation_id, Evaluation.started_at.is_(None))
        .returning(Evaluation.id)
        .execution_options(synchronize_session='fetch')
    ).first()
    return gone is not None


def lost(s: Session, evaluation_id: int) -> bool:
    """Nothing came back from a running evaluation: it waits again for a slot, or leaves the queue if it was cancelled.

    A cancel stands: a cancelled evaluation never runs again, and its requests find it gone with no measurement.
    False when it does not run. The caller commits.
    """
    gone = s.execute(
        delete(Evaluation)
        .where(Evaluation.id == evaluation_id, Evaluation.started_at.is_not(None), Evaluation.cancelled_by.is_not(None))
        .returning(Evaluation.id)
        .execution_options(synchronize_session='fetch')
    ).first()
    if gone is not None:
        return True
    waits = s.execute(
        update(Evaluation)
        .where(Evaluation.id == evaluation_id, Evaluation.started_at.is_not(None))
        .values(started_at=None)
        .returning(Evaluation.id)
        .execution_options(synchronize_session='fetch')
    ).first()
    return waits is not None


def done(s: Session, evaluation_id: int) -> Evaluation | None:
    """Take a running evaluation out of the queue, as its job ended, and return it. None when it does not run.

    The caller commits.
    """
    found = s.get(Evaluation, evaluation_id)
    if found is None or found.started_at is None:
        return None
    s.delete(found)
    return found


def clear(s: Session) -> None:
    """Take every evaluation out of the queue: a daemon that starts asks again for what it still needs."""
    s.execute(delete(Evaluation))


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
    """Return each remeasure that someone asked for and whose own evaluation is not in the queue yet, and when.

    In campaign order, then oldest request first (``ledger.requested``). Another evaluation of the candidate does not
    answer it (``Evaluation.ask``), nor does one that someone cancelled, so the request is listed while that one runs.
    """
    queued = set(s.scalars(select(Evaluation.ask).where(Evaluation.cancelled_by.is_(None))))
    return [
        (candidate, asked.at)
        for campaign in s.scalars(select(Campaign).where(NOT_RETIRED).order_by(Campaign.id))
        for candidate, asked in ledger.requested(s, campaign)
        if asked.id not in queued
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

    A cancel records no measurement. It records a ``Cancel`` under each evaluator that it cancelled, the current one
    for a pending remeasure: no daemon evaluates the candidate there again unless someone asks (``piloop candidate
    remeasure``), even after a restart. Its origin stays. A pending remeasure request is answered. A waiting evaluation
    leaves the queue. A running one gets ``cancelled_by``, and the daemon stops its evaluator and records nothing.
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
    evaluators = {e.evaluator_id for e in found} | ({current} if asked and current is not None else set())
    for evaluator in evaluators:
        cancelled = {'candidate_id': candidate_id, 'evaluator_id': evaluator, 'cancelled_by': actor.signature}
        s.execute(insert(Cancel).values(cancelled).on_conflict_do_nothing())
    s.expire(candidate, ['cancels'])
    for evaluation in found:
        if evaluation.started_at is None:  # its executor finds it gone when a slot comes (``Results.started``)
            s.delete(evaluation)
        else:
            s.execute(
                update(Evaluation)
                .where(Evaluation.id == evaluation.id, Evaluation.started_at.is_not(None))
                .values(cancelled_by=actor.signature)
                .execution_options(synchronize_session='fetch')
            )
    return max(len(found), 1)
