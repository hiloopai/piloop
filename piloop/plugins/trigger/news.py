"""``news``: what ``piloop inbox wait`` would return to the role since its last agent took the news.

The orchestrator role's trigger. Only what needs a decision wakes it (``wakes``): an escalation, or what a person
wrote. The rest of the news, a verdict, an anomaly, the stop or an agent's note, comes with the next wake. The news
goes to the role's agent in notes (``Steer``): in its brief when the note is written before it starts, else as it
works. Each note takes the news up to a cursor (``NewsTaken``), and the next one
takes what came after it. A row that failed did not take its news, and an agent that ended did not take a note that it
did not get: the next note takes them again. A cancelled row took the news that its notes held when it was cancelled.
"""

from datetime import datetime, timedelta
from typing import ClassVar, Literal

from pydantic import Field, model_validator
from sqlalchemy import exists, func, select

from piloop import deprecated
from piloop.actor import by_a_person
from piloop.agents.results import PromptOutcome
from piloop.api.views import CommentNews, News, NewsItem
from piloop.campaign import inbox
from piloop.campaign.stop import CancelRequested
from piloop.db import feed as db_feed
from piloop.db.events import has, latest
from piloop.db.models import OPEN, AgentSession, AgentSessionState, Comment, Event, EventModel, ReadBy
from piloop.ids import TargetKind
from piloop.plugins.escalation.contract import ESCALATION
from piloop.plugins.trigger.contract import Pass, Quiet, Steer, Trigger


class NewsTaken(EventModel):
    """A note to the role's agent takes the news of the events after ``after``, up to ``upto``."""

    READ_BY: ClassVar[ReadBy] = ReadBy.READERS
    kind: Literal['agent_session.news_taken'] = 'agent_session.news_taken'
    after: int
    upto: int


def wakes(item: NewsItem) -> bool:
    """Whether ``item`` wakes the role: an escalation, or a comment that a person wrote."""
    match item:
        case CommentNews(comment=comment):
            return comment.kind == ESCALATION or by_a_person(comment.author)
        case _:
            return False


def taken(p: Pass) -> int:
    """The cursor up to which the role's agents took the news. 0: never.

    A note's ``NewsTaken`` counts while its row is open, and after that only if the row exited cleanly and the agent got
    the note (``comment.delivered``): in its brief, or as it worked. A cancel (``piloop agent cancel``) takes the news
    of the notes written before it: nobody wants that news handled, so it wakes no other agent. An agent gets its notes
    in order, so the latest one that counts covers those before it.
    """
    mine = select(AgentSession.id).where(AgentSession.campaign_id == p.campaign.id, AgentSession.kind == p.role)
    clean = mine.where((AgentSession.state == AgentSessionState.EXITED) & AgentSession.error.is_(None))
    got = (
        select(func.json_extract(Event.payload, '$.comment'))
        .where(
            has(PromptOutcome, kind='comment.delivered'),
            Event.target_kind == TargetKind.AGENT_SESSION,
            Event.target_id.in_(clean),
        )
        .correlate(None)  # its own events, not the NewsTaken that the outer query reads
    )
    on_open = select(Comment.id).where(
        Comment.target_kind == TargetKind.AGENT_SESSION, Comment.target_id.in_(mine.where(AgentSession.state.in_(OPEN)))
    )
    cancels = (
        select(Event.target_id.label('agent_session'), func.min(Event.id).label('at'))
        .where(has(CancelRequested), Event.target_kind == TargetKind.AGENT_SESSION, Event.target_id.in_(mine))
        .group_by(Event.target_id)
        .subquery()
    )
    before_cancel = exists().where(  # the outer NewsTaken came before the cancel of its note's row
        Comment.id == Event.target_id,
        Comment.target_kind == TargetKind.AGENT_SESSION,
        Comment.target_id == cancels.c.agent_session,
        cancels.c.at > Event.id,
    )
    on_note = Event.target_kind == TargetKind.COMMENT
    last = latest(p.s, NewsTaken, on_note, Event.target_id.in_(on_open.union(got)) | before_cancel)
    return 0 if last is None else last[1].upto


def note(found: News, now: datetime) -> str:
    """The note that hands the news to the role's agent, and the escalations that still wait for it."""
    reminder = found.reminder(now)
    return '\n'.join(
        ['News for you:', *(f'- {i.text()}' for i in found.items), *([f'- {reminder}'] if reminder else [])]
    )


def is_open(p: Pass) -> bool:
    """Whether the role has an agent that a note reaches without a launch: requested, starting or running."""
    return bool(p.live()) or (p.last is not None and p.last.state is AgentSessionState.REQUESTED)


class NewsTrigger(Trigger):
    """New inbox items for the role's party, anomalies, verdicts and the stop: ``piloop inbox wait``'s news.

    An escalation or what a person wrote wakes the role ``quiet_seconds`` after the first news, also while the campaign
    is paused, held or stopped, and the rest of the news comes with it. Nothing else wakes it. An agent of the role that
    is open already gets the news at once, in a note.
    """

    WHILE: ClassVar[frozenset[Quiet]] = frozenset({Quiet.PAUSED, Quiet.HELD, Quiet.STOPPED})
    WAKES_ON: ClassVar[frozenset[type[EventModel]]] = frozenset(topic.model for topic in inbox.WAKES)

    quiet_seconds: float = Field(default=30, ge=0)
    """How long the role waits after the first news to launch an agent, so that one agent takes a burst of it."""

    @model_validator(mode='before')
    @classmethod
    @deprecated.until('0.7.0', 'routine_every of 0.5 is gone: delete NewsTrigger._routine_every')
    def _routine_every(cls, data: object) -> object:
        """Drop ``routine_every``, with a warning: nothing routine wakes the role now, of a file or a stored config."""
        if not isinstance(data, dict) or 'routine_every' not in data:
            return data
        deprecated.dropped(
            'nothing routine wakes the orchestrator now, so routine_every is removed; delete it from its news table',
            removed_in='0.7.0',
        )
        return {k: v for k, v in data.items() if k != 'routine_every'}

    def fire(self, p: Pass) -> Steer | None:
        cursor = taken(p)
        found = inbox.news(p.s, cursor, campaign_id=p.campaign.id, party=p.role)
        due = self._due(p, found, cursor)
        if due is None or due > p.now:
            return None
        kinds = sorted({item.kind for item in found.items})
        reason = f'news: {len(found.items)} items ({", ".join(kinds)})'
        return Steer(note(found, p.now), reason, events=(NewsTaken(after=cursor, upto=found.next),))

    def due_in(self, p: Pass) -> timedelta | None:
        cursor = taken(p)
        due = self._due(p, inbox.news(p.s, cursor, campaign_id=p.campaign.id, party=p.role), cursor)
        return None if due is None else max(due - p.now, timedelta())

    def _due(self, p: Pass, found: News, cursor: int) -> datetime | None:
        """When the role's agents may take ``found``, the news after ``cursor``, or None when nothing in it wakes them.

        An open agent takes it at once: the quiet window only saves launches.
        """
        if not any(wakes(item) for item in found.items):
            return None
        page = db_feed.after(p.s, cursor, inbox.WAKES, limit=1)
        first = p.s.scalar(select(Event.at).where(Event.id.in_(page.ids))) or p.now
        return first if is_open(p) else first + timedelta(seconds=self.quiet_seconds)
