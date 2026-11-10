"""The inbox: every comment with a ``priority``. It waits for each party until that party acknowledges it, or it is answered.

A comment addressed to the human has a priority from 0 (urgent) to 4 (someday). Examples are decisions that the
orchestrator took for the human, escalations and notes that ask for the human. Other comments have no priority and form the log.
Nothing waits for the human. An item is a record to review, and ``piloop inbox ack`` only marks it as seen, for the
caller's party (``seer``): the people and the orchestrator each acknowledge for themselves. An answer (``answer``)
settles it for every party, and an escalation takes one.

The orchestrator is any harness that runs the ``piloop-operate`` skill. It learns of new items from the events
table: ``piloop inbox wait`` blocks until ``news`` returns something, so the orchestrator loops on one command.
"""

import re
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Final

from sqlalchemy import ColumnElement, ScalarSelect, Select, case, exists, literal, select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Mapped, Session, aliased, selectinload

from piloop.actor import Actor, System, by_a_person, party_of
from piloop.api.views import (
    AckOutcome,
    AckResult,
    AckView,
    CommentNews,
    CommentView,
    Linked,
    NewsItem,
    Relation,
    ScoreNews,
    StoppedNews,
    Thread,
)
from piloop.db import feed as db_feed
from piloop.db.events import Topic
from piloop.db.models import NOT_RETIRED, Ack, AgentSession, Campaign, Comment, Event, utcnow
from piloop.db.session import write_transaction
from piloop.db.triggers.changelog import CampaignStatusChanged, Commented, CommentEdited
from piloop.ids import CampaignId, CampaignStatus, CommentId, CommentKind, TargetKind, TargetRef
from piloop.plugins.campaign_type.kit.backlog.ids import IDEA
from piloop.plugins.campaign_type.kit.backlog.tables import Idea
from piloop.plugins.campaign_type.kit.candidates.ids import CANDIDATE
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.evaluation.ids import MEASUREMENT
from piloop.plugins.campaign_type.kit.evaluation.tables import Measurement
from piloop.plugins.campaign_type.kit.pool.scored import CandidateScored
from piloop.plugins.campaign_type.kit.runs.ids import RUN
from piloop.plugins.campaign_type.kit.runs.tables import Run
from piloop.plugins.escalation.contract import DECISION, ESCALATION
from piloop.settings import settings
from piloop.text import iso

type Column[T] = Mapped[T] | ColumnElement[T]
"""A column of a mapped class, or an expression of the same type, such as a literal target."""
CITATION = re.compile(r'comment#(\d+)')
"""How a comment refers to another comment in its body. ``thread`` follows these references in both directions."""


HUMAN: Final = System.HUMAN.signature
"""The party that every person acknowledges for: the inbox has one person's side, whoever reads it."""


READERS: Final = (HUMAN, System.ORCHESTRATOR.party)
"""The parties that read the inbox: the people, and the orchestrator."""


def seer(party: str) -> str:
    """The party that ``party`` acknowledges for (``Ack.party``): ``human`` for every person, else ``party``."""
    return HUMAN if by_a_person(party) else party


def items(
    s: Session,
    seen_by: str,
    *,
    campaign_id: CampaignId | None = None,
    kinds: Collection[CommentKind] = (),
    acked: bool = False,
) -> list[tuple[Comment, Campaign | None]]:
    """Return the inbox items that wait for the party ``seen_by`` (``seer``), with their campaigns, most urgent first.

    An item waits until ``seen_by`` acknowledges it, or until a party other than its author's answers it (``answers``):
    an answer settles it for every party. With ``acked``, the items that ``seen_by`` acknowledged or that were answered
    come too. None of a retired campaign comes. ``campaign_id`` and ``kinds`` filter the items. An item whose target has
    no campaign (``campaign_of``) appears only when ``campaign_id`` is None. Items of one priority come oldest first.
    """
    query = _items(campaign_id)
    if not acked:
        query = query.where(~exists().where(Ack.comment_id == Comment.id, Ack.party == seen_by))
    if kinds:
        query = query.where(Comment.kind.in_(kinds))
    found: list[tuple[Comment, Campaign | None]] = [(c, p) for c, p in s.execute(query)]
    if acked:
        return found
    settled = answers(s, [c for c, _ in found])
    return [(c, p) for c, p in found if c.id not in settled]


def _items(campaign_id: CampaignId | None) -> Select[Comment, Campaign]:
    """Every inbox item, for every party, with its campaign, most urgent first: ``items`` before acks and answers."""
    owner = campaign_of(Comment.target_kind, Comment.target_id)
    query = (
        select(Comment, Campaign)
        .outerjoin(Campaign, Campaign.id == owner)
        .where(Comment.priority.is_not(None), Campaign.id.is_(None) | NOT_RETIRED)
        .order_by(Comment.priority, Comment.id)
    )
    return query if campaign_id is None else query.where(owner == campaign_id)


def waiting_for(
    s: Session,
    party: str,
    *,
    campaign_id: CampaignId | None = None,
    kinds: Collection[CommentKind] = (),
    acked: bool = False,
) -> list[tuple[Comment, Campaign | None]]:
    """Return the inbox items of ``items`` that wait for ``party`` and that it did not write (``Actor.party``).

    A comment is for its recipients. Its author sees it in its thread (``thread_of``), with the answers, and never as
    an item to acknowledge (``ack``). An agent's party is its role (``Agent.party``), so an orchestrator never waits for
    what an earlier orchestrator wrote.
    """
    found = items(s, seer(party), campaign_id=campaign_id, kinds=kinds, acked=acked)
    return [(c, p) for c, p in found if party_of(c.author) != party]


def answers(s: Session, asked: Sequence[Comment]) -> dict[CommentId, Comment]:
    """The latest answer of each of ``asked`` that has one, by its id: the one definition of an answered item.

    An answer is a comment on the root of the item's thread (``reply``) that is ``in_reply_to`` the item, or that is
    on the item and answers nothing else. Its author is of a party that reads the inbox (``READERS``), other than the
    item author's: an agent's follow-up or the daemon's report on an item answers nothing.
    """
    by_id = {c.id: c for c in asked}
    roots = {c.target_id if c.target_kind == TargetKind.COMMENT else c.id for c in asked}
    on_roots = (Comment.target_kind == TargetKind.COMMENT) & Comment.target_id.in_(roots)
    found: dict[CommentId, Comment] = {}
    for answer in s.scalars(select(Comment).where(on_roots).order_by(Comment.id)):
        to = by_id.get(answer.in_reply_to if answer.in_reply_to is not None else CommentId(answer.target_id))
        party = party_of(answer.author)
        if to is not None and seer(party) in READERS and party != party_of(to.author):
            found[to.id] = answer
    return found


WAKES: Final = tuple(Topic(m) for m in (Commented, CommentEdited, CampaignStatusChanged, CandidateScored))
"""The events that ``news`` reports: what ends ``piloop inbox wait``."""


@dataclass(frozen=True)
class Line:
    """A line of a ``Change``, as every party sees it, with what decides whom it is news to (``news_to``)."""

    event: int
    """The newest event of the change that names it."""
    campaign_id: CampaignId | None
    item: NewsItem
    author: str | None = None
    """The party that it is no news to: of its author, the item's or the event's."""
    seen_by: frozenset[str] = frozenset()
    """The parties (``seer``) that acknowledged it."""

    def news_to(self, party: str, campaign_id: CampaignId | None) -> bool:
        """Whether it is news to ``party`` in campaign ``campaign_id``, None for every campaign."""
        return campaign_id in (None, self.campaign_id) and party != self.author and seer(party) not in self.seen_by


@dataclass(frozen=True)
class Change:
    """What one page of the ``WAKES`` events after event#``after`` says to every party (``changes``).

    It is one read, which each reader filters to its party and campaign (``news``): the news long-polls of one wake
    share it (``piloop.db.feed.Shared``).
    """

    after: int
    next: int
    more: bool
    lines: tuple[Line, ...]
    """The inbox items that its events name, most urgent first, then the stops and scores, oldest first."""

    def news(self, cursor: int, *, party: str, campaign_id: CampaignId | None) -> list[NewsItem]:
        """Its lines after event#``cursor`` (at least ``after``) that are news to ``party`` in ``campaign_id``."""
        return [line.item for line in self.lines if line.event > cursor and line.news_to(party, campaign_id)]


def changes(s: Session, after: int) -> Change:
    """Read the page of the ``WAKES`` events after event#``after`` (``piloop.db.feed``), and what they say to anyone.

    That is:

    - the new or edited inbox items that no answer settled (``answers``). Who acknowledged each, and who wrote it,
      decide whom it waits for (``waiting_for``).
    - campaigns that the daemon stopped.
    - the daemon's scores of candidates (``CandidateScored``), news to every party but the scorer's.
    """
    page = db_feed.after(s, after, WAKES, limit=settings.daemon.inbox_page)
    events = s.scalars(select(Event).where(Event.id.in_(page.ids)).order_by(Event.id)).all()
    named = {e.target_id: e.id for e in events if isinstance(e.payload, (Commented, CommentEdited))}
    # Read after the page: each statement sees the latest commit, so an item read before it could miss a comment
    # whose event the page then passes. Read after, it has every comment of the page.
    query = _items(None).where(Comment.id.in_(named)).options(selectinload(Comment.acks))
    found: list[tuple[Comment, Campaign | None]] = [(c, p) for c, p in s.execute(query)]
    settled = answers(s, [c for c, _ in found])
    lines = [
        Line(
            event=named[c.id],
            campaign_id=None if campaign is None else campaign.id,
            item=CommentNews(comment=comment_view(c), campaign_name=None if campaign is None else campaign.name),
            author=party_of(c.author),
            seen_by=frozenset(a.party for a in c.acks),
        )
        for c, campaign in found
        if c.id not in settled
    ]
    for e in events:
        match e.payload:
            case CampaignStatusChanged(new=CampaignStatus.STOPPED, stopped_reason=why) if (
                campaign := s.get(Campaign, e.target_id)
            ) is not None:
                lines.append(Line(e.id, campaign.id, StoppedNews(campaign_name=campaign.name, reason=why)))
            case CandidateScored(told=told) if (candidate := s.get(Candidate, e.target_id)) is not None:
                item = ScoreNews(candidate=candidate.id, told=told)
                lines.append(Line(e.id, candidate.campaign_id, item, author=party_of(e.actor)))
    return Change(after=after, next=page.next, more=page.more, lines=tuple(lines))


def news_after(s: Session, after: int, *, campaign_id: CampaignId | None, party: str) -> tuple[list[NewsItem], int]:
    """The news to ``party`` in ``campaign_id`` (None: every campaign) after event#``after``, and the next cursor.

    It reads the pages of ``changes`` one at a time until a page gives a line. The orchestrator's own decisions are for
    the human. The cursor passes every event that it read, so no event is missed.
    """
    cursor, found, more = after, list[NewsItem](), True
    while more and not found:
        change = changes(s, cursor)
        found, cursor, more = change.news(cursor, party=party, campaign_id=campaign_id), change.next, change.more
    return found, cursor


def escalations(
    s: Session, party: str, *, campaign_id: CampaignId | None, shown: Sequence[NewsItem]
) -> list[CommentView]:
    """The escalations that still wait for ``party`` (``waiting_for``) and that ``shown`` does not show.

    They end no wait: an orchestrator that moved past one sees it again with each news.
    """
    ids = {item.comment.id for item in shown if isinstance(item, CommentNews)}
    waiting = waiting_for(s, party, campaign_id=campaign_id, kinds=(ESCALATION,))
    return [comment_view(c) for c, _ in waiting if c.id not in ids]


def ack(s: Session, ids: Sequence[CommentId], actor: Actor) -> list[AckResult]:
    """Acknowledge the inbox items ``ids`` for ``actor``'s party (``seer``), and return what happened to each id.

    Each party acknowledges for itself: the item waits on for every other party. An id that is not an inbox item, that
    ``actor``'s party wrote, or that its party acknowledged already, is reported and not changed. Each ack is one
    ``INSERT … ON CONFLICT DO NOTHING``, so of two that race, one acknowledges and the other learns who did. The caller
    commits.
    """
    party = seer(actor.party)
    results = []
    for comment_id in ids:
        match comment := s.get(Comment, comment_id):
            case None | Comment(priority=None):
                results.append(AckResult(id=comment_id, outcome=AckOutcome.NOT_IN_INBOX, acked_by=None))
            case Comment(author=author) if party_of(author) == actor.party:
                results.append(AckResult(id=comment_id, outcome=AckOutcome.OWN, acked_by=None))
            case _:
                s.expire(comment, ['acks'])  # read again, as the insert leaves them
                added = s.execute(
                    insert(Ack)
                    .values(comment_id=comment.id, party=party, acked_by=actor.signature, acked_at=utcnow())
                    .on_conflict_do_nothing()
                    .returning(Ack.acked_by)
                ).scalar()
                if added is not None:
                    results.append(AckResult(id=comment_id, outcome=AckOutcome.ACKED, acked_by=added))
                    continue
                first = s.scalars(select(Ack.acked_by).where(Ack.comment_id == comment.id, Ack.party == party)).one()
                results.append(AckResult(id=comment_id, outcome=AckOutcome.ALREADY, acked_by=first))
    return results


def withdraw(s: Session, item_id: CommentId) -> None:
    """Take piloop's own item ``item_id`` out of every inbox: it asks nothing any more. The caller commits.

    It is acknowledged for each of the ``READERS`` that has not acknowledged it, signed by the daemon: an answer
    would come from another party, and nobody answers what nothing asks. An item that is gone (a raw DELETE) takes
    nothing.
    """
    for party in READERS:
        row = select(Comment.id, literal(party), literal(System.DAEMON.signature), literal(utcnow())).where(
            Comment.id == item_id
        )
        s.execute(
            insert(Ack).from_select(['comment_id', 'party', 'acked_by', 'acked_at'], row).on_conflict_do_nothing()
        )


def ack_views(comment: Comment) -> list[AckView]:
    """The ``acks`` of ``comment`` as a view shows them, oldest first."""
    return [AckView(party=a.party, by=a.acked_by, at=iso(a.acked_at)) for a in comment.acks]


@dataclass(frozen=True)
class Settled:
    """An escalation that someone answered already (``answers``)."""

    by: str
    """The signature of whoever wrote its latest answer."""
    answer: str
    """The text of its latest answer."""


class Answered(Exception):
    """An escalation that ``answer`` refused: another party answered it first (``settled``)."""

    def __init__(self, item: CommentId, settled: Settled) -> None:
        super().__init__(f'comment#{item} was answered already, by {settled.by}: {settled.answer}')
        self.item, self.settled = item, settled


def settled(s: Session, item: Comment) -> Settled | None:
    """The latest answer of ``item`` (``answers``), or None: then an answer would be a second one."""
    found = answers(s, [item]).get(item.id)
    return None if found is None else Settled(found.author, found.body)


def answer(s: Session, item: Comment, body: str, *, author: Actor, kind: CommentKind, priority: int | None) -> Comment:
    """Answer inbox item ``item`` as ``author``: a reply of ``kind`` on the root of its thread. The caller commits.

    An escalation takes one answer: a second one is refused, whoever writes it. The caller holds the write lock
    (``write_transaction``), so the check and the reply are in one write transaction: of two answers that race, the
    second sees the first.

    Raises:
        Answered: ``item`` is an escalation that another party answered already.
    """
    if item.kind == ESCALATION and (done := settled(s, item)) is not None:
        raise Answered(item.id, done)
    reply = Comment(
        target=TargetRef(TargetKind.COMMENT, root_of(s, item).id),
        in_reply_to=item.id,
        author=author.signature,
        kind=kind,
        priority=priority,
        body=body.strip(),
    )
    s.add(reply)
    s.flush()
    return reply


def reply(s: Session, comment_id: CommentId, body: str, *, author: Actor, priority: int) -> Comment:
    """Answer inbox item ``comment_id`` as ``author`` with a ``decision`` (``answer``), and commit.

    As in email, the answer is ``in_reply_to`` the item, and its target is the root (``root_of``): threads stay flat,
    so ``campaign_of`` finds the campaign of every reply. The priority makes the answer an inbox item, so ``news``
    reports it to every orchestrator except its author until it is acknowledged.

    Raises:
        Answered: the item is an escalation that another party answered already.
    """
    write_transaction(s)
    decision = answer(s, s.get_one(Comment, comment_id), body, author=author, kind=DECISION, priority=priority)
    s.commit()
    return decision


def message(s: Session, campaign_id: CampaignId, body: str, *, author: Actor, priority: int) -> Comment:
    """Write ``body`` to the orchestrator of campaign ``campaign_id`` as ``author``: a ``message`` on it, and commit.

    The priority makes it an inbox item, so ``news`` reports it to every orchestrator except its author. The
    orchestrator answers with ``reply``, which the author then sees in the inbox.
    """
    sent = Comment(
        target=TargetRef(TargetKind.CAMPAIGN, campaign_id),
        author=author.signature,
        kind=CommentKind.MESSAGE,
        priority=priority,
        body=body.strip(),
    )
    s.add(sent)
    s.commit()
    return sent


def root_of(s: Session, comment: Comment) -> Comment:
    """The root of ``comment``'s thread: the first comment up the chain of targets that is not on a comment."""
    while comment.target_kind == TargetKind.COMMENT and (parent := s.get(Comment, comment.target_id)) is not None:
        comment = parent
    return comment


def comment_view(c: Comment) -> CommentView:
    """``c`` as the API shows it."""
    return CommentView(
        id=c.id,
        kind=str(c.kind),
        target=str(c.target),
        in_reply_to=c.in_reply_to,
        author=c.author,
        created=iso(c.created_at),
        priority=c.priority,
        body=c.body,
        acks=ack_views(c),
    )


def thread_of(s: Session, comment: Comment) -> Thread:
    """The thread that ``comment`` is in: its root (``root_of``) and the comments linked to the root (``thread``)."""
    root = root_of(s, comment)
    answered = answers(s, [root]).get(root.id)
    return Thread(
        answer=None if answered is None else answered.id,
        comment=comment_view(root),
        linked=[Linked(**comment_view(c).model_dump(), relation=relation) for c, relation in thread(s, root)],
    )


def thread(s: Session, root: Comment) -> list[tuple[Comment, Relation]]:
    """Return the comments linked to the root of a thread, with how: its replies, then those it cites or that cite it.

    Each part is oldest first. A citation is ``comment#<id>`` in the body, for example ``Re: comment#12`` in a
    decision. Other comments on the same target are not part of the thread. A campaign collects every note on
    the board, and the answer would be lost among them.
    """
    ref = f'comment#{root.id}'
    reply = Comment.target == TargetRef(TargetKind.COMMENT, root.id)
    cites = Comment.body.op('GLOB', is_comparison=True)(f'*{ref}') | Comment.body.op('GLOB', is_comparison=True)(
        f'*{ref}[^0-9]*'
    )
    cited = Comment.id.in_(int(n) for n in CITATION.findall(root.body))
    relation = case((reply, Relation.REPLY.value), (cites, Relation.CITES.value), else_=Relation.CITED_BY.value)
    rows = s.execute(
        select(Comment, relation)
        .where(reply | cites | cited, Comment.id != root.id)
        .order_by(case((reply, 0), else_=1), Comment.id)
    )
    return [(c, Relation(r)) for c, r in rows]


def campaign_of(target_kind: Column[TargetKind], target_id: Column[int]) -> ColumnElement[int | None]:
    """Return the campaign that the target of a comment or event belongs to.

    A ``comment`` target is resolved through that comment's target, and through one more comment. That covers every
    reply, because ``reply`` keeps threads flat: a reply is on its root, and the event of a reply is on the reply.
    Deeper comments have no campaign.
    """
    return case(*_owner_branches(target_kind, target_id, comments=2))


def _owner_of_comment(comment_id: Column[int], comments: int) -> ScalarSelect[int | None]:
    parent = aliased(Comment)
    return (
        select(case(*_owner_branches(parent.target_kind, parent.target_id, comments)))
        .where(parent.id == comment_id)
        .scalar_subquery()
    )


def _owner_branches(
    target_kind: Column[TargetKind], target_id: Column[int], comments: int
) -> list[tuple[ColumnElement[bool], Column[int] | ScalarSelect[int] | ScalarSelect[int | None]]]:
    through = [(target_kind == TargetKind.COMMENT, _owner_of_comment(target_id, comments - 1))] if comments else []
    return [
        *through,
        (target_kind == TargetKind.CAMPAIGN, target_id),
        (target_kind == IDEA, select(Idea.campaign_id).where(Idea.id == target_id).scalar_subquery()),
        (
            target_kind == RUN,
            select(Run.campaign_id).where(Run.id == target_id).scalar_subquery(),
        ),
        (
            target_kind == CANDIDATE,
            select(Candidate.campaign_id).where(Candidate.id == target_id).scalar_subquery(),
        ),
        (
            target_kind == MEASUREMENT,
            select(Candidate.campaign_id)
            .join(Measurement.candidate)
            .where(Measurement.id == target_id)
            .scalar_subquery(),
        ),
        (
            target_kind == TargetKind.AGENT_SESSION,
            select(AgentSession.campaign_id).where(AgentSession.id == target_id).scalar_subquery(),
        ),
    ]
