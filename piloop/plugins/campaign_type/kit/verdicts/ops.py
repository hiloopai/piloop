"""The operations on verdicts that the API and the CLI run: a verdict set by hand."""

from contextlib import closing
from dataclasses import dataclass, replace
from typing import Literal, assert_never

from sqlalchemy.orm import Session

from piloop.actor import Actor
from piloop.campaign.campaigns import repository
from piloop.campaign.ops import Refused, _get, _reason
from piloop.db.models import Campaign
from piloop.plugins.campaign_type.kit.candidates.ids import CandidateId, Cause, Outcome
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.verdicts.judge import MOVED, Rebased, head, node, on_moved, verdict_rule
from piloop.plugins.campaign_type.kit.verdicts.verdict import (
    Refusal,
    label,
    override,
    refusal,
    write,
)
from piloop.plugins.campaign_type.kit.verdicts.verdict_rule.contract import Trunk
from piloop.plugins.vcs.contract import VcsError


@dataclass(frozen=True)
class Overridden:
    """What ``set_verdict`` did: the verdict that the candidate had, and its merge onto the head for a keep off it."""

    old: str
    rebased: Rebased | None = None


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
    the daemon measures and judges against the head (``ledger.unjudged``), and a merge that conflicts files a seed to
    compose it. A keep of a branch run never moves the
    head. With ``Cause.NOTE``, it keeps a regressed result in view as a lead: an ``interesting`` verdict whose reason
    is followed by the one that it notes.

    Raises:
        Refused: the reason is blank, ``verdict.refusal`` refuses it, or it is a keep off the head of a candidate that
            no run made, which no seed can compose.
    """
    candidate = _get(s, Candidate, candidate_id, 'candidate')
    why = _reason(reason)
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
    if to is Outcome.KEEP and (trunk := missing_head(s, candidate)) is not None:
        if candidate.run is None:
            raise Refused(
                f'{name} does not build on the head candidate#{trunk.id}: as a keep it would drop what'
                ' the head kept. Mark it interesting, or add an idea that merges it onto the head'
            )
        reason = MOVED.format(head=trunk.id) + f'kept by hand: {why}'
        write(candidate, Outcome.INTERESTING, cause=Cause.MOVED, reason=reason, by=actor)
        campaign = s.get_one(Campaign, candidate.campaign_id)
        with closing(repository(campaign)) as repo:
            return Overridden(old, on_moved(s, campaign.config, repo, candidate, trunk))
    override(candidate, to, cause=cause, reason=why, by=actor)
    return Overridden(old)


def missing_head(s: Session, candidate: Candidate) -> Candidate | None:
    """Return the head if a keep of ``candidate`` would be a trunk keep without it, else None.

    A candidate builds on the head when the head is among its ancestors (``parent_id``): a commit of a run that
    started from it. Else the vcs decides (``Repository.contains``): a merge onto the head, or a compose that took it.
    A keep that would not raise the trunk's bar (``VerdictRule.raises_bar``), such as a branch run's, never moves the
    head.

    Raises:
        Refused: the vcs cannot tell, for example because the campaign repo is gone.
    """
    campaign = s.get_one(Campaign, candidate.campaign_id)
    kept = replace(node(s, candidate), verdict=Outcome.KEEP)
    if not verdict_rule(campaign.config).raises_bar(kept, Trunk()):
        return None
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
