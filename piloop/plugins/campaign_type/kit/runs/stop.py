"""What the runs tell the stop rule and the campaigns' pass: attempts, new bests and runs, and when they settle."""

from datetime import datetime

from sqlalchemy import ColumnElement, exists, func, select
from sqlalchemy.orm import Session

from piloop.campaign.stop import _over_runs, _since_reopen, reopened_at, run_cancel
from piloop.db.models import (
    Campaign,
    Comment,
    EventModel,
)
from piloop.db.triggers import DAEMON_ACTOR
from piloop.ids import CommentKind, TargetRef
from piloop.plugins.campaign_type.kit.backlog.ids import IDEA, IdeaStatus
from piloop.plugins.campaign_type.kit.backlog.tables import Idea
from piloop.plugins.campaign_type.kit.candidates.ids import Origin
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.evaluation.tables import Measurement
from piloop.plugins.campaign_type.kit.pool.pool import UNSCORED
from piloop.plugins.campaign_type.kit.pool.scored import last_best_at, new_bests
from piloop.plugins.campaign_type.kit.runs.ids import ATTEMPTS, FINISHED_RUNS, OPEN_RUNS, RunEnd
from piloop.plugins.campaign_type.kit.runs.tables import Run


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


def settle_cancelled(s: Session, run: Run, idea: Idea) -> None:
    """Open a cancelled run's idea again, with a note on the idea that says why.

    A cancel stops a run, not its idea: another worker may claim it. To take the idea off the board, drop it.
    """
    cancel = run_cancel(s, run.id)
    by = f' by {cancel[0].actor}' if cancel is not None else ''
    idea.status = IdeaStatus.OPEN
    body = f'open: run#{run.id} was cancelled{by}; drop the idea to take it off the board'
    s.add(Comment(target=TargetRef(IDEA, idea.id), author=DAEMON_ACTOR, kind=CommentKind.NOTE, body=body))


def release(s: Session, run: Run, end: RunEnd, last: Candidate | None) -> None:
    """End the claim of a run that ended ``end``, once its commits are judged, with a note on the idea when it reopens.

    The idea is ``done`` after a finished run with a score: ``last``, the newest of its versions' candidates that has
    one, a known tree included. A finished run without one, or any other end, opens it again for another worker, and the
    note says why. A cancel settles as ``settle_cancelled`` says. An idea that someone set to another status meanwhile,
    or a run that claimed nothing, has no claim to end.
    """
    idea = run.idea
    if idea is None or idea.status is not IdeaStatus.CLAIMED:
        return
    match end:
        case RunEnd.CANCELLED:
            settle_cancelled(s, run, idea)
            return
        case RunEnd.FINISHED if last is not None and last.origin not in UNSCORED:
            idea.status = IdeaStatus.DONE
            return
        case RunEnd.FINISHED:
            why = 'it finished, but nothing that it saved has a score'
        case _:
            why = f'it ended {end}' + (f': {run.error}' if run.error else '')
    idea.status = IdeaStatus.OPEN
    body = f'open: run#{run.id} gave the idea back; {why}'
    s.add(Comment(target=TargetRef(IDEA, idea.id), author=DAEMON_ACTOR, kind=CommentKind.NOTE, body=body))


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
