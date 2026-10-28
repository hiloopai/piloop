from datetime import datetime, timedelta
from typing import ClassVar

from pydantic import Field

from piloop.api.views import NewsItem
from piloop.campaign import inbox
from piloop.db.events import latest
from piloop.db.models import AgentSession, Event, EventModel
from piloop.db.triggers.changelog import AgentSessionRequested
from piloop.ids import TargetKind
from piloop.plugins.trigger.contract import Fire, Pass, Trigger


class CronTrigger(Trigger):
    """The role's ``interval`` passed since its last routine agent, one that got no anomaly, and the role has news.

    The news is what ``piloop inbox wait`` would return to the role since that agent was requested, such as a verdict, a
    seed, an anomaly or an inbox item. Without news, the next routine agent starts ``max_interval`` seconds after the
    last one. The first one counts from the registration of the campaign, and needs no news. An ``interval`` of 0 turns
    it off.
    """

    WAKES_ON: ClassVar[frozenset[type[EventModel]]] = frozenset(topic.model for topic in inbox.WAKES)

    max_interval: float = Field(default=14400, ge=0)
    """The most seconds between two routine agents of a role: after this long, one starts without news, so that a
    stuck campaign is still looked at. At or below the role's ``interval``, every one starts on the timer alone."""

    def fire(self, p: Pass) -> Fire | None:
        routine = p.routine() if p.last is not None else None
        if p.interval <= 0 or p.now < _since(p, routine) + timedelta(seconds=p.interval):
            return None
        if routine is None:
            return Fire(f'timer: {p.interval:g}s')
        if found := _news(p, routine):
            kinds = ', '.join(sorted({item.kind for item in found}))
            return Fire(f'timer: {p.interval:g}s; news: {len(found)} items ({kinds})')
        if p.now >= _since(p, routine) + timedelta(seconds=self.max_interval):
            return Fire(f'timer: no news since agent_session#{routine.id}')
        return None

    def due_in(self, p: Pass) -> timedelta | None:
        """When the ``interval`` passes, and after it, when ``max_interval`` does: news comes as an event."""
        if p.interval <= 0:
            return None
        routine = p.routine() if p.last is not None else None
        since = _since(p, routine)
        due = since + timedelta(seconds=p.interval)
        if routine is not None and p.now >= due:
            due = since + timedelta(seconds=max(p.interval, self.max_interval))
        return max(due - p.now, timedelta(0))


def _since(p: Pass, routine: AgentSession | None) -> datetime:
    """When the role's last routine agent started, else when the campaign was registered."""
    return routine.started_at if routine is not None else p.campaign.created_at


def _news(p: Pass, routine: AgentSession) -> list[NewsItem]:
    """The role's news since ``routine`` was requested: the feed after its ``agent_session.requested`` event.

    A row from before that event (0.2) reads the feed from its start, so its next review starts as before.
    """
    requested = latest(
        p.s, AgentSessionRequested, Event.target_kind == TargetKind.AGENT_SESSION, Event.target_id == routine.id
    )
    cursor = 0 if requested is None else requested[0].id
    return inbox.news(p.s, cursor, campaign_id=p.campaign.id, party=p.role).items
