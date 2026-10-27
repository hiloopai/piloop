"""What the runs tell the stop rule and the campaigns' pass: attempts, keeps and runs, and when they settle."""

from datetime import datetime

from sqlalchemy import ColumnElement, exists, func, select
from sqlalchemy.orm import Session

from piloop.campaign.stop import _over_runs, _since_reopen, run_cancel
from piloop.db.events import has
from piloop.db.models import (
    Campaign,
    Comment,
    Event,
)
from piloop.db.triggers import DAEMON_ACTOR
from piloop.db.triggers.changelog import CampaignStatusChanged
from piloop.ids import CampaignStatus, CommentKind, TargetKind, TargetRef
from piloop.plugins.campaign_type.kit.backlog.ids import IdeaStatus
from piloop.plugins.campaign_type.kit.backlog.tables import Idea
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.evaluation.ledger import keeps, last_keep_at
from piloop.plugins.campaign_type.kit.evaluation.tables import Measurement
from piloop.plugins.campaign_type.kit.runs.ids import ATTEMPTS, FINISHED_RUNS, OPEN_RUNS
from piloop.plugins.campaign_type.kit.runs.tables import Run
from piloop.plugins.campaign_type.kit.verdicts.verdict import pending_clause


def attempt() -> ColumnElement[bool]:
    """Select the runs that were an attempt at their idea: the open runs, and those whose end is an attempt."""
    return Run.end.is_(None) | Run.end.in_(ATTEMPTS)


def scored() -> ColumnElement[bool]:
    """Select the runs that have a candidate that is measured or waiting for a verdict."""
    return exists().where(
        Candidate.run_id == Run.id,
        pending_clause() | exists().where(Measurement.candidate_id == Candidate.id),
    )


def unsettled() -> ColumnElement[bool]:
    """Select the runs whose outcome can still change: the open runs, and the runs that are scored now (``scoring``)."""
    return Run.status.in_(OPEN_RUNS) | Run.scoring


def counted() -> ColumnElement[bool]:
    """Select the runs that count toward the run limits, ``max_runs`` and ``plateau``: those that reached the evaluator.

    Such a run has a candidate that is measured or waiting for its verdict (``scored``). A run that stopped with no
    version, or with only versions that asked to be skipped, does not count: it stopped at a cheap check, or a harness
    or host fault ended it before it saved one. It says nothing about the idea's effect on the metric. ``hold_at``,
    ``max_tokens`` and ``max_hours`` limit what such runs cost. This is the one rule for both limits.
    """
    return attempt() & scored()


def attempts(idea: Idea) -> int:
    """Count the runs of ``idea`` that were an attempt at it. ``ideas.max_runs`` caps this number."""
    return sum(run.end is None or run.end.attempt for run in idea.runs)


def settled(s: Session, campaign: Campaign) -> bool:
    """Whether no run of ``campaign`` is open, and the commits of each one that ended are judged."""
    busy = select(Run.id).join(Idea).where(Idea.campaign_id == campaign.id, Run.status.in_(OPEN_RUNS) | Run.scoring)
    return s.scalar(busy.limit(1)) is None


def park_cancelled(s: Session, run: Run) -> None:
    """Park the idea of a cancelled run, with a note on the idea: a run is cancelled because it is not wanted now."""
    cancel = run_cancel(s, run.id)
    run.idea.status = IdeaStatus.PARKED
    by = f' by {cancel[0].actor}' if cancel is not None else ''
    body = f'parked: run#{run.id} was cancelled{by}; set the idea ready to run it again'
    s.add(
        Comment(target=TargetRef(TargetKind.IDEA, run.idea_id), author=DAEMON_ACTOR, kind=CommentKind.NOTE, body=body)
    )


def kept(s: Session, campaign: Campaign) -> int:
    """Count the campaign's candidates of runs that are kept, on the trunk or on a branch. The baseline is not one."""
    return s.scalar(keeps(campaign).where(Candidate.run_id.is_not(None)).with_only_columns(func.count())) or 0


def runs(s: Session, campaign: Campaign) -> int:
    """Count the campaign's runs, of every role, that hold a place under ``max_runs``, since the latest reopen.

    These are the runs that count (``counted``) and the unsettled attempts, which may still count. So the queue never
    starts more runs than ``max_runs``.
    """
    holding = counted() | (attempt() & unsettled())
    return _over_runs(s, campaign, func.count(Run.id), holding, *_since_reopen(s, campaign, Run.created_at)) or 0


def runs_without_keep(s: Session, campaign: Campaign, before: datetime | None = None) -> int:
    """Count the finished runs that count (``counted``) since the latest keep or reactivation. ``plateau`` uses this.

    With ``before``, only the runs of that window that finished by then count: 0 if the window opened later.

    The window starts when the run of the latest keep finished, or when the campaign was last set ``active`` again
    after a stop (``piloop init``, or an UPDATE), whichever is later. A reactivated campaign gets a fresh window, so
    a ``plateau`` stop does not fire again at once. An evaluator change alone does not reset the window.
    """
    last_keep = last_keep_at(s, campaign)
    reactivated = s.scalar(
        select(func.max(Event.at)).where(
            has(CampaignStatusChanged, new=CampaignStatus.ACTIVE),
            Event.target == TargetRef(TargetKind.CAMPAIGN, campaign.id),
        )
    )
    since = max((t for t in (last_keep, reactivated) if t is not None), default=None)
    after = (Run.finished_at > since,) if since is not None else ()
    by = (Run.finished_at <= before,) if before is not None else ()
    return _over_runs(s, campaign, func.count(Run.id), Run.status.in_(FINISHED_RUNS), counted(), *after, *by) or 0
