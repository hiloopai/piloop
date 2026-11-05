"""``news``: what ``piloop inbox wait`` would return to the role since its last agent took the news.

One of the orchestrator's two wake-ups (``triggers``). An escalation, what a person wrote, or an evaluation retry
wakes it. The rest of the news, a score, an anomaly, the stop or an agent's note, comes with the next wake. The news
goes to the role's agent in notes (``Steer``): in its brief when the note is written before it starts, else as it
works. Each note takes the news up to a cursor (``NewsTaken``), and the next one
takes what came after it. A row that failed did not take its news, and an agent that ended did not take a note that it
did not get: the next note takes them again. A cancelled row took the news that its notes held when it was cancelled.
"""

from datetime import datetime, timedelta
from typing import ClassVar, Literal

from sqlalchemy import exists, func, select

from piloop.actor import by_a_person
from piloop.agents.results import PromptOutcome
from piloop.api.views import CommentNews, News, NewsItem
from piloop.campaign import inbox
from piloop.campaign.stop import CancelRequested
from piloop.db import feed as db_feed
from piloop.db.events import has, in_order, latest
from piloop.db.models import OPEN, AgentSession, AgentSessionState, Comment, Event, EventModel, ReadBy
from piloop.ids import TargetKind
from piloop.plugins.campaign_type.kit.candidates.ids import CANDIDATE
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.evaluation.jobs import InfraFailed
from piloop.plugins.escalation.contract import ESCALATION
from piloop.plugins.role.orchestrator.passes import RolePass, Steer


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


def taken(p: RolePass) -> int:
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


def is_open(p: RolePass) -> bool:
    """Whether the role has an agent that a note reaches without a launch: requested, starting or running."""
    return bool(p.live()) or (p.last is not None and p.last.state is AgentSessionState.REQUESTED)


WAKES: frozenset[type[EventModel]] = frozenset({*(topic.model for topic in inbox.WAKES), InfraFailed})
"""The inbox's events and evaluation infrastructure failures whose requests retry."""


def news(p: RolePass, quiet_seconds: float) -> tuple[Steer | None, datetime | None]:
    """New inbox items for the role's party, anomalies, scores and the stop: ``piloop inbox wait``'s news.

    An escalation, what a person wrote, or an evaluation retry wakes the role ``quiet_seconds`` after the first news,
    also while the campaign is paused, held or stopped, and the rest of the news comes with it. An agent of the role that
    is open already gets the news at once, in a note. Returns the note, or when to ask again: None for the next event.
    """
    cursor = taken(p)
    found = inbox.news(p.s, cursor, campaign_id=p.campaign.id, party=p.role)
    retries = in_order(
        p.s,
        InfraFailed,
        Event.id > cursor,
        Event.id <= found.next,
        Event.target_kind == CANDIDATE,
        Event.target_id.in_(select(Candidate.id).where(Candidate.campaign_id == p.campaign.id)),
    )
    due = _due(p, found, cursor, quiet_seconds, retries[0][0].at if retries else None)
    if due is None or due > p.now:
        return None, due
    kinds = sorted({item.kind for item in found.items} | ({'evaluation_retry'} if retries else set()))
    reason = f'news: {len(found.items) + len(retries)} items ({", ".join(kinds)})'
    text = '\n'.join(
        [
            note(found, p.now),
            *(
                f'- candidate#{event.target_id}: evaluation#{retry.evaluation} retry after infrastructure failure:'
                f' {retry.error}'
                for event, retry in retries
            ),
        ]
    )
    return Steer(text, reason, events=(NewsTaken(after=cursor, upto=found.next),)), None


def _due(
    p: RolePass, found: News, cursor: int, quiet_seconds: float, first_retry: datetime | None = None
) -> datetime | None:
    """When the role's agents may take ``found``, the news after ``cursor``, or None when nothing in it wakes them.

    An open agent takes it at once: the quiet window only saves launches.
    """
    if first_retry is None and not any(wakes(item) for item in found.items):
        return None
    page = db_feed.after(p.s, cursor, inbox.WAKES, limit=1)
    first = p.s.scalar(select(Event.at).where(Event.id.in_(page.ids))) or p.now
    if first_retry is not None:
        first = min(first, first_retry)
    return first if is_open(p) else first + timedelta(seconds=quiet_seconds)
