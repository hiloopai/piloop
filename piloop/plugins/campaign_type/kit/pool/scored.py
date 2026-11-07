"""The record of a candidate scored into the pool: ``candidate.scored``, what its run's worker hears."""

from datetime import datetime
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from piloop.actor import System
from piloop.db.events import emit, in_order
from piloop.db.models import Campaign, Event, EventModel
from piloop.ids import TargetRef
from piloop.plugins.campaign_type.kit.candidates.ids import CANDIDATE, CandidateId
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.evaluation.ids import TrackName
from piloop.plugins.campaign_type.kit.pool.pool import Pool

EventModel.retire('candidate.judged')  # 0.5 judged a candidate against its bar; 0.6 scores it into the pool
EventModel.retire('candidate.rebased')  # 0.5 merged a keep that lacked the head onto it; 0.6 has no head
EventModel.retire('candidate.noted')  # 0.5 noted a regressed candidate as a lead; a comment says it now
EventModel.retire('candidate.rejudge_requested')  # 0.5 judged a candidate again after a measurement by hand
EventModel.retire('candidate.rejudge_failed')  # its answer when it could not


class CandidateScored(EventModel):
    """The daemon scored a candidate into the pool of its track: where it stands then, and the line its worker hears."""

    kind: Literal['candidate.scored'] = 'candidate.scored'
    track: TrackName
    score: float | None
    """Its mean of the track's primary, or None without a successful evaluation."""
    measured: int
    """Its successful evaluations of the full panel."""
    best: CandidateId | None
    """The best of the track then."""
    rank: int | None
    """Its place among those that may be best, or None."""
    parent_delta: float | None = None
    """How much better its primary is than its parent's: positive when better."""
    best_delta: float | None = None
    """How much better its primary is than the best's then."""
    told: str
    """Where it stands, as its worker hears it (``Standing.told``)."""


def record_scored(s: Session, pool: Pool, candidate: CandidateId) -> CandidateScored:
    """Emit ``CandidateScored`` for ``candidate``, where it stands in ``pool`` now. The caller commits."""
    standing = pool.standing(candidate)
    scored = CandidateScored(
        track=standing.track,
        score=standing.mean,
        measured=standing.status.scored,
        best=standing.best,
        rank=standing.rank,
        parent_delta=standing.vs_parent,
        best_delta=standing.vs_best,
        told=standing.told(),
    )
    emit(s, scored, TargetRef(CANDIDATE, candidate), System.DAEMON)
    return scored


def new_bests(s: Session, campaign: Campaign, track: TrackName | None = None) -> list[tuple[Event, CandidateScored]]:
    """The scores of ``campaign`` that made their candidate the best of its track, oldest first: of ``track`` only, if set.

    A score is one when its candidate is the best then, and was not before: the baseline is the first best. The best
    is a query, so a later measurement can move it without such a record: these are the moments that a candidate was
    scored best.
    """
    ours = select(Candidate.id).where(Candidate.campaign_id == campaign.id)
    first = s.scalar(ours.order_by(Candidate.id).limit(1))
    found = in_order(s, CandidateScored, Event.target_kind == CANDIDATE, Event.target_id.in_(ours))
    before: dict[TrackName, int | None] = {}
    bests: list[tuple[Event, CandidateScored]] = []
    for e, scored in found:
        if scored.best == e.target_id and before.get(scored.track, first) != e.target_id:
            bests.append((e, scored))
        before[scored.track] = scored.best
    return [(e, scored) for e, scored in bests if track in (None, scored.track)]


def last_best_at(s: Session, campaign: Campaign) -> datetime | None:
    """When the run whose candidate last became the best of its track finished, or None before the first new best.

    While that run goes on, it is when the candidate was scored best: the plateau counts the runs after it.
    """
    found = new_bests(s, campaign)
    if not found:
        return None
    event = found[-1][0]
    run = s.get_one(Candidate, event.target_id).run
    return max(event.at, run.finished_at) if run is not None and run.finished_at is not None else event.at
