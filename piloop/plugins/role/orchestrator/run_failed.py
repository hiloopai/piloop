"""``run_failed``: a run failed or timed out, and no agent of the campaign got it yet: an orchestrator's wake-up.

Each such run is an incident: the requested row gets one ``anomaly.reported`` event for it, which the agent's brief
lists (the ``incidents`` section). Each incident goes to one agent only.
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from piloop.db.events import AnomalyReported, in_order
from piloop.db.models import AgentSession, Event
from piloop.ids import AgentSessionId, TargetKind, TargetRef
from piloop.plugins.campaign_type.kit.runs.ids import RUN, RunEnd
from piloop.plugins.campaign_type.kit.runs.tables import Run, campaign_runs
from piloop.plugins.role.orchestrator.passes import Fire, RolePass
from piloop.settings import settings
from piloop.text import age, cell

RUN_FAILED = 'run_failed'
"""The wake-up's name, in ``triggers`` and in each ``anomaly.reported`` that it writes."""


@dataclass(frozen=True)
class Incident:
    """A failed run: the row it is about, since when, and what happened."""

    subject: TargetRef
    """The row: ``run#36``."""
    at: datetime
    what: str
    """What happened, after the row: ``failed``."""
    detail: str
    """The evidence in one line."""

    def line(self, now: datetime) -> str:
        """The incident as a brief and the inbox show it: ``- run#36 failed, 1h ago: <detail>``."""
        return f'- {self.subject} {self.what}, {age(self.at, now)} ago: {self.detail}'


def run_failed(p: RolePass) -> Fire | None:
    """Request an agent for the failed runs that no agent of the campaign got yet, oldest first, or None."""
    failed = p.s.scalars(campaign_runs(p.campaign.id).where(Run.end.in_((RunEnd.FAILED, RunEnd.TIMED_OUT))))
    found = [
        Incident(
            TargetRef(RUN, run.id),
            run.finished_at or run.created_at,
            'failed',
            f'{run.end} on {run.harness} (idea#{run.idea_id}): {cell(run.error or "no error recorded", settings.agents.detail_chars)}',
        )
        for run in failed
    ]
    known = {a.subject for _, a in given(p)}
    new = sorted((i for i in found if i.subject not in known), key=lambda i: i.at)
    if not new:
        return None
    events = tuple(AnomalyReported(subject=i.subject, trigger=RUN_FAILED, line=i.line(p.now)) for i in new)
    return Fire(f'{RUN_FAILED}: ' + ', '.join(f'{i.subject} {i.what}' for i in new), events=events)


def given(p: RolePass) -> list[tuple[Event, AnomalyReported]]:
    """The failed runs that agents of the campaign got, oldest first: each is investigated once."""
    rows = in_order(
        p.s,
        AnomalyReported,
        Event.target_kind == TargetKind.AGENT_SESSION,
        Event.target_id.in_(select(AgentSession.id).where(AgentSession.campaign_id == p.campaign.id)),
    )
    return [(e, a) for e, a in rows if a.trigger == RUN_FAILED]


def assigned(s: Session, agent_session_id: AgentSessionId) -> list[str]:
    """Return the incident lines that an agent got, in order. An agent that no failed run woke got none."""
    rows = in_order(s, AnomalyReported, Event.target == TargetRef(TargetKind.AGENT_SESSION, agent_session_id))
    return [a.line for _, a in rows]
