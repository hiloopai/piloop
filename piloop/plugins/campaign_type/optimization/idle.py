"""Free slots that sit idle while ideas wait for review: one inbox item for each role that does runs, while it lasts.

A **spell** starts when the daemon sees free slots of a role with no runnable idea for them, while ideas that the role
would take are ``proposed``. After the role's ``idle_alert``, the spell raises an escalation of priority 1. The spell
ends when the slots fill, when no such idea waits, or when the campaign stops running: paused, held or stopped. Then
the daemon acknowledges the escalation.
"""

from datetime import datetime
from typing import ClassVar, Literal, assert_never

from sqlalchemy import select
from sqlalchemy.orm import Session

from piloop.actor import System
from piloop.campaign import escalations
from piloop.campaign.stop import acted, asked, check, requested
from piloop.config import CampaignConfig
from piloop.daemon.loop import Soonest
from piloop.db.events import emit, has, latest
from piloop.db.models import Campaign, Comment, Event, EventModel, ReadBy, count
from piloop.ids import CampaignStatus, CommentId, RoleName, TargetKind, TargetRef
from piloop.plugins.campaign_type.kit.backlog.ids import IdeaStatus
from piloop.plugins.campaign_type.kit.backlog.tables import Idea
from piloop.plugins.campaign_type.optimization.planner import free_slots, offered, taken_by
from piloop.plugins.escalation.contract import Choice, EscalationView
from piloop.plugins.fact_source import facts
from piloop.plugins.fact_source.facts import Facts
from piloop.plugins.role.contract import ImplementerRole
from piloop.plugins.stop_rule.contract import GO, Go, Hold, Stop

PRIORITY = 1
"""The escalation's priority: idle slots cost time today."""
SHOWN = 5
"""How many of the proposed ideas the escalation names."""


class SlotsIdle(EventModel):
    """On a campaign: a spell of the free slots of ``role`` behind proposed ideas starts, raises its item, or ends."""

    READ_BY: ClassVar[ReadBy] = ReadBy.READERS
    kind: Literal['slots_idle.seen', 'slots_idle.raised', 'slots_idle.ended']
    role: RoleName
    comment: CommentId | None = None
    """The escalation: on ``raised``, and on the ``ended`` of a spell that raised one."""


def watch(s: Session, campaign: Campaign, cfg: CampaignConfig, now: datetime) -> datetime | None:
    """Start, raise or end the spell of each role that does runs, and return when a spell raises next. The caller commits."""
    roles = cfg.roles.doing_runs()
    waiting = {name: _proposed(s, campaign, role) for name, role in roles.items() if role.idle_alert}
    left = _running(s, campaign, cfg) if any(waiting.values()) else None
    later = Soonest()
    for name, role in roles.items():
        ideas = waiting.get(name, [])
        idle = 0
        if left is not None and ideas:
            free = free_slots(s, campaign, {name: role}, left.get(facts.RUNS))
            idle = max(free - count(s, offered(s, campaign, cfg, role)), 0)
        later.add(_spell(s, campaign, name, role, idle, ideas if idle else [], now))
    return later.at


def _proposed(s: Session, campaign: Campaign, role: ImplementerRole) -> list[Idea]:
    """The proposed ideas that ``role`` would take, oldest first."""
    query = select(Idea).where(Idea.campaign_id == campaign.id, Idea.status == IdeaStatus.PROPOSED).order_by(Idea.id)
    return list(s.scalars(taken_by(s, role, query)))


def _running(s: Session, campaign: Campaign, cfg: CampaignConfig) -> Facts | None:
    """What the stop rule leaves while the campaign runs, or None when it does not: paused, held, stopped or stopping."""
    if campaign.status is not CampaignStatus.ACTIVE or campaign.paused is not None or requested(s, campaign):
        return None
    answer = acted(check(s, campaign, cfg)) if asked(s, campaign, cfg) else GO
    match answer:
        case Go(left=left):
            return left
        case Hold() | Stop():
            return None
        case _:
            assert_never(answer)


def _spell(
    s: Session, campaign: Campaign, name: RoleName, role: ImplementerRole, idle: int, ideas: list[Idea], now: datetime
) -> datetime | None:
    """Move the spell of role ``name`` on: ``idle`` slots behind ``ideas``. Return when it raises, if it waits to."""
    on = TargetRef(TargetKind.CAMPAIGN, campaign.id)
    last = latest(s, SlotsIdle, Event.target == on, has(SlotsIdle, role=name))
    spell = last if last is not None and last[1].kind != 'slots_idle.ended' else None
    match spell:
        case None if ideas:
            # stamped with the pass's clock, which its due is counted on, not with the write's
            emit(s, SlotsIdle(kind='slots_idle.seen', role=name), on, System.DAEMON).at = now
            return now + role.idle_alert
        case None:
            return None
        case (_, SlotsIdle(comment=comment)) if not ideas:
            emit(s, SlotsIdle(kind='slots_idle.ended', role=name, comment=comment), on, System.DAEMON)
            item = s.get(Comment, comment) if comment is not None else None
            if item is not None and item.acked_at is None:
                item.acked_at, item.acked_by = now, System.DAEMON.signature
            return None
        case (seen, SlotsIdle(kind='slots_idle.seen')) if now < (due := seen.at + role.idle_alert):
            return due
        case (_, SlotsIdle(kind='slots_idle.seen')):
            item = escalations.escalate(s, on, _asked(name, idle, ideas), author=System.DAEMON, priority=PRIORITY)
            emit(s, SlotsIdle(kind='slots_idle.raised', role=name, comment=item.id), on, System.DAEMON)
            return None
        case _:  # raised: one item for the spell
            return None


def _asked(name: RoleName, idle: int, ideas: list[Idea]) -> EscalationView:
    shown = ', '.join(f'idea#{i.id}' for i in ideas[:SHOWN]) + (', …' if len(ideas) > SHOWN else '')
    return EscalationView(
        headline=f'{len(ideas)} ideas wait for your review while {idle} {name} slots are free. Do you review them?',
        why='piloop runs only ideas that are ready. Until you review these, the free slots do nothing.',
        choices=(
            Choice(id='A', label='Review the ideas now.', result='The ideas that you set ready start at once.'),
            Choice(id='B', label='Let the slots wait.', result='The slots stay free until you review.'),
        ),
        details=(
            f'{len(ideas)} proposed ideas await review while {idle} {name} slots are idle ({shown}). Set each one'
            " ready, parked or rejected: piloop idea status <id> ready --reason '…'."
        ),
    )
