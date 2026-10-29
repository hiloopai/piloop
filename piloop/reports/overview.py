"""The facts of one campaign that its views share, computed from the database.

The sections (``piloop.plugins.section``), the API's status and ``piloop wake`` read them here, so they always say the
same thing: the summary line, the head's scores, the queue, the live agents and their output, the inbox threads, and the
progress against the stop rule.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from covalence import Cid
from sqlalchemy import select
from sqlalchemy.orm import Session

from piloop.agents.roles import harness_of
from piloop.campaign.inbox import campaign_of
from piloop.campaign.stop import describe, held
from piloop.config import TRANSCRIPT, CampaignConfig
from piloop.db.models import LIVE, AgentSession, Campaign, Event, Pause
from piloop.plugins.campaign_type.kit.runs.tables import Run, run_of
from piloop.plugins.cas.contract import Cas
from piloop.plugins.harness.contract import UsageReader
from piloop.plugins.role.contract import Files
from piloop.runtime.factory import handle_for, runtime_for
from piloop.store import chunks
from piloop.store.tree import Missing
from piloop.text import NONE, cell, metric

DETAIL_CHARS = 160


def holds(campaign: Campaign) -> str:
    """Return what the pause of a paused campaign holds: ``launches``, or ``launches and evaluations``."""
    return 'launches and evaluations' if campaign.paused is Pause.ALL else 'launches'


def progress(s: Session, campaign: Campaign, cfg: CampaignConfig, now: datetime) -> str:
    """Return where the campaign stands against its stop rule, as the rule describes it: ``run 7/200 · 2.1h/12h``.

    piloop formats no fact itself (``StopRule.describe``). When the stop rule holds launches, the line ends with why:
    `` (launches held: spend at its limit)``.
    """
    hold = held(s, campaign, cfg)
    return (' · '.join(describe(s, campaign, cfg, now)) or NONE) + (
        f' (launches held: {hold})' if hold is not None else ''
    )


def live_agent_sessions(s: Session, campaign: Campaign) -> Sequence[AgentSession]:
    """Return the agents of the campaign that are starting or running, oldest first."""
    return s.scalars(
        select(AgentSession)
        .where(AgentSession.campaign_id == campaign.id, AgentSession.state.in_(LIVE))
        .order_by(AgentSession.id)
    ).all()


def on(s: Session, w: AgentSession) -> str:
    """Describe what the agent works on: the idea of its run, or else the reason for its request."""
    if run_of(w) is not None:
        idea = s.get_one(Run, run_of(w)).idea
        return f'idea#{idea.id} {cell(idea.title)}'
    return cell(w.reason or NONE)


def agent_output(s: Session, w: AgentSession, lines: int, cas: Cas) -> list[str]:
    """Return the last ``lines`` non-empty lines of output of the agent.

    The first choice is the text in the latest snapshot of its session file in ``cas`` (``session_file``), on any
    machine. If there is none, or no text in it, the output that the runtime reads is used. The last choice is the
    transcript saved when the agent ended.
    """
    definition = harness_of(s, w)
    reader = definition.sessions() if definition is not None else None
    said = (
        list(_said_in(_Snapshot(w.session_file, reader, cas), lines))
        if reader is not None and w.session_file is not None
        else []
    )
    return said or _filled(_live_output(w, lines))[-lines:] or _filled(_transcript(w.log_dir))[-lines:]


@dataclass(frozen=True)
class _Snapshot:
    """A snapshot of a session file, known by its address alone: where it is read from does not change its bytes."""

    session_file: Cid
    reader: UsageReader = field(compare=False)
    cas: Cas = field(compare=False)


@lru_cache(maxsize=256)
def _said_in(snapshot: _Snapshot, lines: int) -> tuple[str, ...]:
    """The last ``lines`` of the agent's text in a snapshot.

    Read from the CAS once for each snapshot: a watch draws the same snapshot many times.
    """
    try:
        session = chunks.read(snapshot.cas, snapshot.session_file)
    except Missing:  # a part is gone from the store: the screen or the transcript tells more
        return ()
    return () if session is None else tuple(snapshot.reader.said(session, lines))


def _live_output(w: AgentSession, lines: int) -> str:
    try:
        return runtime_for(w).read(handle_for(w), lines=lines)
    except Exception:  # Not launched yet, runtime missing or agent gone: the transcript is the fallback.
        return ''


def _transcript(log_dir: str | None) -> str:
    path = Path(log_dir) / TRANSCRIPT if log_dir else None
    return path.read_text(errors='replace') if path is not None and path.is_file() else ''


def _filled(text: str) -> list[str]:
    """Return the lines of ``text`` that are not blank, without trailing blanks."""
    return [line.rstrip() for line in text.splitlines() if line.strip()]


def latest_events(s: Session, campaign: Campaign, limit: int) -> Sequence[Event]:
    """Return the last ``limit`` events of the campaign, newest first."""
    mine = select(Event).where(campaign_of(Event.target_kind, Event.target_id) == campaign.id)
    return s.scalars(mine.order_by(Event.id.desc()).limit(limit)).all()


def defining(cfg: CampaignConfig) -> list[str]:
    """Return the sweeping roles that define ideas (``files = "ideas"``) and have triggers: the ones that fill the queue."""
    return [name for name, role in cfg.roles.sweeping().items() if role.files is Files.IDEAS and role.triggers]


def minutes(seconds: float) -> str:
    """Format a duration in minutes: ``2.5 min``."""
    return f'{seconds / 60:.1f} min'


def num(value: float | None) -> str:
    """Format a score, ``—`` when unknown."""
    return NONE if value is None else metric(value)


def _four(value: float | None) -> str:
    return 'unmeasured' if value is None else metric(value)
