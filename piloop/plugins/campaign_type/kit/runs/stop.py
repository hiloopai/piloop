"""What the runs tell the stop rule and the campaigns' pass: attempts, new bests and runs, and when they settle."""

from collections.abc import Collection, Sequence
from datetime import datetime

from sqlalchemy import ColumnElement, exists, func, select
from sqlalchemy.orm import Session, aliased

from piloop.campaign.stop import _over_runs, _since_reopen, reopened_at, run_cancel
from piloop.db.events import latest
from piloop.db.models import (
    Campaign,
    Comment,
    Event,
    EventModel,
    among,
)
from piloop.db.triggers import DAEMON_ACTOR
from piloop.ids import CommentKind, TargetRef
from piloop.plugins.campaign_type.contract import CAMPAIGN_TYPES
from piloop.plugins.campaign_type.kit.backlog.ids import IDEA, IdeaStatus
from piloop.plugins.campaign_type.kit.candidates.ids import CANDIDATE, Origin
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.evaluation.tables import Measurement
from piloop.plugins.campaign_type.kit.pool.scored import CandidateScored, last_best_at, new_bests
from piloop.plugins.campaign_type.kit.runs.ids import ATTEMPTS, FINISHED_RUNS, OPEN_RUNS, RunEnd, RunId
from piloop.plugins.campaign_type.kit.runs.tables import Run, RunClaim


def attempt() -> ColumnElement[bool]:
    """Select the runs that were an attempt at their idea: the open runs, and those whose end is an attempt."""
    return Run.end.is_(None) | Run.end.in_(ATTEMPTS)


def scored() -> ColumnElement[bool]:
    """Select the runs that have a candidate that is measured or waiting for its score."""
    return exists().where(
        Candidate.run_id == Run.id,
        (Candidate.origin == Origin.RUN) | exists().where(Measurement.candidate_id == Candidate.id),
    )


def unsettled() -> ColumnElement[bool]:
    """Select the runs whose outcome can still change: the open runs, and the runs that are scored now (``scoring``)."""
    return Run.status.in_(OPEN_RUNS) | Run.scoring


def counted() -> ColumnElement[bool]:
    """Select the runs that count toward ``max_runs``: those that reached the evaluator.

    Such a run has a candidate that is measured or waiting for its score (``scored``). A run that stopped with no
    version, or with only versions that asked to be skipped, does not count: it stopped at a cheap check, or a harness
    or host fault ended it before it saved one. It says nothing about the idea's effect on the metric. ``plateau``
    counts it (``runs_without_best``), and ``hold_at``, ``max_tokens`` and ``max_hours`` limit what it costs.
    """
    return attempt() & scored()


def settled(s: Session, campaign: Campaign) -> bool:
    """Whether no run of ``campaign`` is open, and the commits of each one that ended are judged."""
    busy = select(Run.id).where(Run.campaign_id == campaign.id, Run.status.in_(OPEN_RUNS) | Run.scoring)
    return s.scalar(busy.limit(1)) is None


EventModel.retire('run.head_replaced')  # 0.5 cancelled a run whose head a verdict set by hand replaced


def settle_claims(s: Session, run: Run, end: RunEnd) -> None:
    """Let the run's campaign type settle each claim of ``run``, which ended ``end`` (``settle_claim``). The caller commits."""
    for claim in run.claims:
        settle_claim(s, claim, end)


def settle_claim(s: Session, claim: RunClaim, end: RunEnd) -> None:
    """Let the campaign type settle ``claim``, of a run that ended ``end``, on the run's submissions for it.

    One whose idea someone set to another status meanwhile, or another run claimed since, has nothing to settle. The
    caller commits.
    """
    from piloop.plugins.campaign_type.kit.evaluation.submissions import submitted_for  # it imports this module

    if claim.idea.status is not IdeaStatus.CLAIMED or claim.idea.claims[-1] is not claim:
        return
    kind = CAMPAIGN_TYPES.model_of(claim.run.campaign.type)()
    kind.settle(s, claim, end, [s.get_one(Candidate, c) for c in submitted_for(s, claim)])


def settle_cancelled(s: Session, claim: RunClaim) -> None:
    """Open the idea of a cancelled run's claim again, with a note on the idea that says why.

    A cancel stops a run, not its ideas: another worker may claim them. To take an idea off the board, drop it.
    """
    cancel = run_cancel(s, claim.run_id)
    by = f' by {cancel[0].actor}' if cancel is not None else ''
    claim.idea.status = IdeaStatus.OPEN
    body = f'open: run#{claim.run_id} was cancelled{by}; drop the idea to take it off the board'
    s.add(Comment(target=TargetRef(IDEA, claim.idea_id), author=DAEMON_ACTOR, kind=CommentKind.NOTE, body=body))


def release(s: Session, claim: RunClaim, end: RunEnd, submissions: Sequence[Candidate]) -> None:
    """Settle ``claim`` of a run that ended ``end``, once its run's submissions are scored.

    The idea is ``done`` once one of ``submissions``, the claim's own, has a score, whatever the run's end. Else a
    cancel settles as ``settle_cancelled`` says, and any other end opens it again for another worker, with a note that
    says why.
    """
    if any(score_of(s, c) is not None for c in submissions):
        claim.idea.status = IdeaStatus.DONE
        return
    if end is RunEnd.CANCELLED:
        settle_cancelled(s, claim)
        return
    why = f'it ended {end}' + (f': {claim.run.error}' if claim.run.error else '')
    reopen(s, claim, why if not submissions else f'nothing that it submitted for it has a score; {why}')


def reopen(s: Session, claim: RunClaim, why: str) -> None:
    """Open the idea that the run of ``claim`` gave back again, for another worker, with a note on it that says ``why``."""
    claim.idea.status = IdeaStatus.OPEN
    body = f'open: run#{claim.run_id} gave the idea back; {why}'
    s.add(Comment(target=TargetRef(IDEA, claim.idea_id), author=DAEMON_ACTOR, kind=CommentKind.NOTE, body=body))


def score_of(s: Session, candidate: Candidate) -> CandidateScored | None:
    """The newest score of ``candidate`` (``candidate.scored``), or None before it has one."""
    found = latest(s, CandidateScored, Event.target == TargetRef(CANDIDATE, candidate.id))
    return found[1] if found is not None else None


def stalls(s: Session, runs: Collection[RunId]) -> dict[RunId, int]:
    """Count, for each of ``runs`` that claimed an idea, the other runs of its ideas that ended ``stalled``."""
    mine, theirs, other = aliased(RunClaim), aliased(RunClaim), aliased(Run)
    rows = s.execute(
        select(mine.run_id, func.count(other.id.distinct()))
        .join(theirs, (theirs.idea_id == mine.idea_id) & (theirs.run_id != mine.run_id))
        .join(other, (other.id == theirs.run_id) & (other.end == RunEnd.STALLED))
        .where(among(mine.run_id, runs))
        .group_by(mine.run_id)
    )
    return {RunId(run): count for run, count in rows}


def kept(s: Session, campaign: Campaign) -> int:
    """Count the times that a run's candidate of the campaign became the best of its track when it was scored."""
    return len(new_bests(s, campaign))


def runs(s: Session, campaign: Campaign) -> int:
    """Count the campaign's runs, of every role, that hold a place under ``max_runs``, since the latest reopen.

    These are the runs that count (``counted``) and the unsettled attempts, which may still count. So the queue never
    starts more runs than ``max_runs``.
    """
    holding = counted() | (attempt() & unsettled())
    return _over_runs(s, campaign, func.count(Run.id), holding, *_since_reopen(s, campaign, Run.created_at)) or 0


def runs_without_best(s: Session, campaign: Campaign, before: datetime | None = None) -> int:
    """Count the finished attempts since the latest new best or reactivation. ``plateau`` uses this.

    An attempt that never reached the evaluator counts: a campaign whose runs never submit has a plateau too.

    With ``before``, only the runs of that window that finished by then count: 0 if the window opened later.

    The window starts when a run's candidate last became the best (``last_best_at``), or at the campaign's latest
    reopen (``reopened_at``), whichever is later: a reopened campaign gets a fresh window, as it gets its runs, hours
    and tokens again. An UPDATE that sets it active is no reopen, so it stops again at once. An evaluator change alone
    does not reset the window.
    """
    last_best = last_best_at(s, campaign)
    since = max((t for t in (last_best, reopened_at(s, campaign)) if t is not None), default=None)
    after = (Run.finished_at > since,) if since is not None else ()
    by = (Run.finished_at <= before,) if before is not None else ()
    return _over_runs(s, campaign, func.count(Run.id), Run.status.in_(FINISHED_RUNS), attempt(), *after, *by) or 0
