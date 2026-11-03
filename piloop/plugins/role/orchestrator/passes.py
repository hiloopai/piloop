"""What the orchestrator's two wake-ups read on a pass, and what each answers: a request (``Fire``) or a note (``Steer``)."""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from piloop.db.models import LIVE, AgentSession, Campaign, EventModel, count
from piloop.ids import RoleName
from piloop.plugins.role.contract import Quiet


@dataclass(frozen=True)
class RolePass:
    """The orchestrator of one campaign on one pass: what ``news`` and ``run_failed`` read. They write nothing."""

    s: Session
    now: datetime
    """The time of the pass. A wake-up that compares a time reads this, not the clock."""
    campaign: Campaign
    role: RoleName
    last: AgentSession | None
    """The role's latest row. None before the first."""
    quiet: Quiet | None

    def mine(self) -> Select[AgentSession]:
        """The role's rows in the campaign."""
        return select(AgentSession).where(AgentSession.campaign_id == self.campaign.id, AgentSession.kind == self.role)

    def live(self) -> int:
        """The number of the role's rows that are starting or running."""
        return count(self.s, self.mine().where(AgentSession.state.in_(LIVE)))


def role_pass(s: Session, campaign: Campaign, name: RoleName, now: datetime, quiet: Quiet | None) -> RolePass:
    """Return what the wake-ups of role ``name`` read in this pass."""
    last = s.scalars(
        select(AgentSession)
        .where(AgentSession.campaign_id == campaign.id, AgentSession.kind == name)
        .order_by(AgentSession.id.desc())
        .limit(1)
    ).first()
    return RolePass(s=s, now=now, campaign=campaign, role=name, last=last, quiet=quiet)


@dataclass(frozen=True)
class Fire:
    """A request for an agent: its ``agent_sessions.reason``, and the events to emit on its row."""

    reason: str
    events: tuple[EventModel, ...] = ()


@dataclass(frozen=True)
class Steer:
    """A note for the role's agent: on its newest open row, or else on a row that the daemon requests for it.

    An open row is requested, starting or running. An agent gets the notes written on its row before it starts in its
    brief, and the later ones as it works: in its turn through its harness's hook, or else when its turn ends.
    """

    text: str
    reason: str
    """The ``agent_sessions.reason`` of the row that the daemon requests when the role has none open."""
    events: tuple[EventModel, ...] = ()
    """The events to emit on the note. Whether the agent got it is its ``comment.delivered`` on the agent."""
