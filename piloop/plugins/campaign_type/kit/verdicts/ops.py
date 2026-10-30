"""The operations on verdicts that the API and the CLI run: a verdict set by hand."""

from contextlib import closing
from dataclasses import dataclass
from typing import Literal, assert_never

from sqlalchemy import select
from sqlalchemy.orm import Session

from piloop.actor import Actor
from piloop.campaign.campaigns import repository
from piloop.campaign.ops import Refused, existing, required_reason
from piloop.campaign.stop import request_cancel
from piloop.db.events import emit
from piloop.db.models import Campaign
from piloop.ids import Ending, TargetRef
from piloop.plugins.campaign_type.kit.candidates.ids import CandidateId, Cause, Outcome
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.evaluation.measure import metrics
from piloop.plugins.campaign_type.kit.runs.ids import OPEN_RUNS, RUN
from piloop.plugins.campaign_type.kit.runs.stop import HeadReplaced
from piloop.plugins.campaign_type.kit.runs.tables import Run
from piloop.plugins.campaign_type.kit.verdicts.judge import MOVED, Rebased, head, on_moved
from piloop.plugins.campaign_type.kit.verdicts.verdict import (
    Refusal,
    label,
    note,
    override,
    refusal,
    write,
)
from piloop.plugins.vcs.contract import VcsError


@dataclass(frozen=True)
class Overridden:
    """What ``set_verdict`` did: the verdict that the candidate had, and its merge onto the head for a keep off it."""

    old: str
    rebased: Rebased | None = None
    cancelled: tuple[Run, ...] = ()
    """The runs from the head that it replaced, which it cancelled (``cancel_replaced``)."""


def set_verdict(
    s: Session,
    candidate_id: CandidateId,
    to: Outcome,
    *,
    cause: Literal[Cause.NOTE] | None = None,
    reason: str,
    actor: Actor,
) -> Overridden:
    """Set ``actor``'s verdict on a candidate, and return what that did. The one way to override a verdict.

    The verdict is ``actor``'s now, so the daemon never changes it. The ``candidate.verdict`` event (a trigger) has the
    reason. A keep of a trunk run that does not build on the head (``missing_head``) goes the way of the daemon's
    keep against a bar that is no longer the head: the head is the newest trunk keep, and a keep that lacks the
    head's changes would drop them from the trunk. So the candidate is ``interesting`` (``Cause.MOVED``), and the
    verdict rule says what follows (``ledger.on_moved``). By default its merge onto the head is a pending candidate that
    the daemon measures and judges against the head (``ledger.unjudged``); a merge that conflicts is recorded on its
    reason. A reference that builds on the head is kept like this
    once the current evaluator scores it, and is the head then. With ``Cause.NOTE``, it keeps a regressed result in
    view as a lead (``verdict.note``), and the verdict stays whoever's it was unless the note changes it.

    Raises:
        Refused: the reason is blank, ``verdict.refusal`` refuses it, it is the first verdict of a reference that the
            current evaluator does not score, or it is a keep off the head of a candidate that no run made, which no
            idea can compose.
    """
    candidate = existing(s, Candidate, candidate_id, 'candidate')
    why = required_reason(reason)
    name = f'candidate#{candidate.id}'
    match refused := refusal(candidate, to, cause):
        case Refusal.PENDING:
            raise Refused(f'{name} is pending: the daemon judges it now; set its verdict once it is judged')
        case Refusal.NOT_JUDGED:
            raise Refused(f'{name} is {candidate.origin}, which is never judged: only a judgement is overridden')
        case Refusal.SAME:
            raise Refused(f'{name} is {to} already ({candidate.verdict_reason or "no reason"})')
        case Refusal.NO_RESULT:
            raise Refused(
                f'{name} is {candidate.verdict} ({candidate.verdict_reason or "no reason"}):'
                ' only a measured discard or interesting takes a note'
            )
        case None:
            pass
        case _:
            assert_never(refused)
    old = label(candidate)
    campaign = s.get_one(Campaign, candidate.campaign_id)
    if candidate.verdict is None and not metrics(candidate, campaign.evaluator_id):
        raise Refused(
            f'{name} has no score under the current evaluator: wait until the daemon measures it, or record your'
            ' measurement with piloop measurement add'
        )
    was = head(s, campaign)
    if to is Outcome.KEEP and (trunk := missing_head(s, candidate)) is not None:
        if candidate.run is None:
            raise Refused(
                f'{name} does not build on the head candidate#{trunk.id}: as a keep it would drop what'
                ' the head kept. Add an idea that merges it onto the head'
            )
        reason = MOVED.format(head=trunk.id) + f'kept by hand: {why}'
        write(candidate, Outcome.INTERESTING, cause=Cause.MOVED, reason=reason, by=actor)
        with closing(repository(campaign)) as repo:
            return Overridden(old, on_moved(s, campaign.config, repo, candidate, trunk))
    match cause:
        case Cause.NOTE:
            note(s, candidate, why, by=actor)
        case None:
            override(candidate, to, reason=why, by=actor)
        case _:
            assert_never(cause)
    return Overridden(old, cancelled=cancel_replaced(s, campaign, was, actor))


def cancel_replaced(s: Session, campaign: Campaign, was: Candidate | None, actor: Actor) -> tuple[Run, ...]:
    """Cancel the open trunk runs that started from ``was``, if a verdict set by hand replaced it as the head.

    Such a run builds on a tree that is not the head, and its brief named that head. Each cancel is ``actor``'s, as ``piloop run cancel --wrap-up`` would ask it: the implementer
    may save its work, which is judged against the head when it has the head. Once the run ends, its idea is ready
    again (``HeadReplaced``). Return the runs, oldest first.
    """
    now = head(s, campaign)
    if was is None or (now is not None and now.id == was.id):
        return ()
    stale = select(Run).where(
        Run.campaign_id == campaign.id, Run.base_candidate_id == was.id, Run.status.in_(OPEN_RUNS)
    )
    runs = s.scalars(stale.order_by(Run.id)).all()
    for run in runs:
        request_cancel(s, run, actor, ending=Ending.WRAP_UP)
        replaced = HeadReplaced(head=now.id if now is not None else None)
        emit(s, replaced, TargetRef(RUN, run.id), actor)
    return tuple(runs)


def missing_head(s: Session, candidate: Candidate) -> Candidate | None:
    """Return the head if a keep of ``candidate`` would be a trunk keep without it, else None.

    A candidate builds on the head when the head is among its ancestors (``parent_id``): a commit of a run that
    started from it. Else the vcs decides (``Repository.contains``): a merge onto the head, or a compose that took it.

    Raises:
        Refused: the vcs cannot tell, for example because the campaign repo is gone.
    """
    campaign = s.get_one(Campaign, candidate.campaign_id)
    trunk = head(s, campaign)
    ancestor: Candidate | None = candidate
    while trunk is not None and ancestor is not None and ancestor.id != trunk.id:
        ancestor = ancestor.parent
    if trunk is None or ancestor is not None:
        return None
    try:
        with closing(repository(campaign)) as repo:
            builds = repo.contains(candidate.revision, trunk.revision)
    except VcsError as e:
        raise Refused(
            f'the vcs cannot tell if candidate#{candidate.id} builds on the head candidate#{trunk.id}: {e}'
        ) from e
    return None if builds else trunk
