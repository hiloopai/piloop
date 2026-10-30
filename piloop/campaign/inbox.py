"""The human's inbox: every comment with a ``priority`` that nobody has acknowledged yet.

A comment addressed to the human has a priority from 0 (urgent) to 4 (someday). Examples are decisions that the
orchestrator took for the human, escalations and findings. Other comments have no priority and form the log.
Nothing waits for the human. An item is a record to review, and ``piloop inbox ack`` only marks it as seen.

The orchestrator is any harness that runs the ``piloop-operate`` skill. It learns of new items from the events
table: ``piloop inbox wait`` blocks until ``news`` returns something, so the orchestrator loops on one command.
"""

import re
from collections.abc import Collection, Sequence
from datetime import datetime
from typing import Final

from sqlalchemy import ColumnElement, ScalarSelect, case, select
from sqlalchemy.orm import Mapped, Session, aliased

from piloop.actor import Actor, party_of
from piloop.api.views import (
    AckOutcome,
    AckResult,
    AnomalyNews,
    CommentNews,
    CommentView,
    Linked,
    News,
    NewsItem,
    Relation,
    StoppedNews,
    Thread,
    VerdictNews,
)
from piloop.db import feed as db_feed
from piloop.db.events import AnomalyReported
from piloop.db.models import NOT_RETIRED, AgentSession, Campaign, Comment, Event, utcnow
from piloop.db.triggers.changelog import CampaignStatusChanged, Commented, CommentEdited
from piloop.ids import CampaignId, CampaignStatus, CommentId, CommentKind, TargetKind, TargetRef
from piloop.plugins.campaign_type.kit.backlog.ids import IDEA
from piloop.plugins.campaign_type.kit.backlog.tables import Idea
from piloop.plugins.campaign_type.kit.candidates.ids import CANDIDATE
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.evaluation.ids import MEASUREMENT
from piloop.plugins.campaign_type.kit.evaluation.tables import Measurement
from piloop.plugins.campaign_type.kit.runs.ids import RUN
from piloop.plugins.campaign_type.kit.runs.tables import Run
from piloop.plugins.campaign_type.kit.verdicts.judge import CandidateJudged
from piloop.settings import settings
from piloop.text import iso

type Column[T] = Mapped[T] | ColumnElement[T]
"""A column of a mapped class, or an expression of the same type, such as a literal target."""
CITATION = re.compile(r'comment#(\d+)')
"""How a comment refers to another comment in its body. ``thread`` follows these references in both directions."""


def items(
    s: Session, *, campaign_id: CampaignId | None = None, kinds: Collection[CommentKind] = (), acked: bool = False
) -> list[tuple[Comment, Campaign | None]]:
    """Return the inbox items with their campaigns, most urgent first, then oldest first.

    Only unacknowledged items are returned, unless ``acked`` is True, and none of a retired campaign. ``campaign_id``
    and ``kinds`` filter the items.
    An item whose target has no campaign (``campaign_of``) appears only when ``campaign_id`` is None.
    """
    owner = campaign_of(Comment.target_kind, Comment.target_id)
    query = (
        select(Comment, Campaign)
        .outerjoin(Campaign, Campaign.id == owner)
        .where(Comment.priority.is_not(None), Campaign.id.is_(None) | NOT_RETIRED)
        .order_by(Comment.priority, Comment.id)
    )
    if not acked:
        query = query.where(Comment.acked_at.is_(None))
    if campaign_id is not None:
        query = query.where(owner == campaign_id)
    if kinds:
        query = query.where(Comment.kind.in_(kinds))
    return [(c, p) for c, p in s.execute(query)]


def waiting_for(
    s: Session,
    party: str,
    *,
    campaign_id: CampaignId | None = None,
    kinds: Collection[CommentKind] = (),
    acked: bool = False,
) -> list[tuple[Comment, Campaign | None]]:
    """Return the inbox items of ``items`` that ``party`` did not write: what its actors have to look at (``Actor.party``).

    A comment is for its recipients. Its author sees it in its thread (``thread_of``), with the answers, and never as
    an item to acknowledge (``ack``). An agent's party is its role (``Agent.party``), so an orchestrator never waits for
    what an earlier orchestrator wrote.
    """
    found = items(s, campaign_id=campaign_id, kinds=kinds, acked=acked)
    return [(c, p) for c, p in found if party_of(c.author) != party]


WAKES: Final = tuple(
    db_feed.Topic(m) for m in (Commented, CommentEdited, CampaignStatusChanged, AnomalyReported, CandidateJudged)
)
"""The events that ``news`` reports: what ends ``piloop inbox wait``."""


def news(s: Session, after: int, *, campaign_id: CampaignId | None = None, party: str, upto: int | None = None) -> News:
    """Return the events after event#``after`` that the orchestrator must look at, and the next cursor.

    The news reports:

    - new or edited unacknowledged inbox items that wait for ``party`` (``waiting_for``). The orchestrator's own
      decisions are for the human.
    - campaigns that the daemon stopped.
    - anomalies that the daemon gave to a watchdog.
    - the verdicts that another party gave: the daemon's, or a person's.

    It reads the ``WAKES`` events from the feed (``piloop.db.feed``), a page at a time until a page gives a line.
    ``campaign_id`` filters all of them. The returned cursor passes every event that it read, so no event is missed.
    The escalations that still wait for ``party`` (``unanswered``) come with it, but end no wait: an orchestrator that
    moved past one sees it again on each wait.
    """
    cursor, found, more, waiting = after, list[NewsItem](), True, []
    while more and not found:
        page = db_feed.after(s, cursor, WAKES, limit=settings.daemon.inbox_page)
        cursor, more = page.next, page.more
        events = s.scalars(select(Event).where(Event.id.in_(page.ids)).order_by(Event.id)).all()
        # Read after the page: each statement sees the latest commit, so an item read before it could miss a comment
        # whose event the page then passes. Read after, it has every comment of the page.
        waiting = waiting_for(s, party, campaign_id=campaign_id)
        found = _news(s, events, waiting, campaign_id, party)
    shown = {item.comment.id for item in found if isinstance(item, CommentNews)}
    escalations = [comment_view(c) for c in unanswered(s, waiting) if c.id not in shown]
    return News(next=cursor, items=found, escalations=escalations)


def unanswered(s: Session, waiting: Sequence[tuple[Comment, Campaign | None]]) -> list[Comment]:
    """The escalations of ``waiting`` that no other party answered, oldest first.

    An answer is a comment on the root of the escalation's thread (``reply``) that is ``in_reply_to`` it, or that is on
    it and answers nothing else.
    """
    asked = {c.id: c for c, _ in waiting if c.kind == CommentKind.ESCALATION}
    roots = {c.target_id if c.target_kind == TargetKind.COMMENT else c.id for c in asked.values()}
    on_roots = (Comment.target_kind == TargetKind.COMMENT) & Comment.target_id.in_(roots)
    answered = set[CommentId]()
    for answer in s.scalars(select(Comment).where(on_roots)):
        to = asked.get(answer.in_reply_to if answer.in_reply_to is not None else CommentId(answer.target_id))
        if to is not None and party_of(answer.author) != party_of(to.author):
            answered.add(to.id)
    return [c for id_, c in sorted(asked.items()) if id_ not in answered]


def _news(
    s: Session,
    events: Sequence[Event],
    waiting: Sequence[tuple[Comment, Campaign | None]],
    campaign_id: CampaignId | None,
    party: str,
) -> list[NewsItem]:
    fresh = {e.target_id for e in events if isinstance(e.payload, (Commented, CommentEdited))}
    found: list[NewsItem] = [
        CommentNews(comment=comment_view(c), campaign_name=campaign.name if campaign else None)
        for c, campaign in waiting
        if c.id in fresh
    ]
    for e in events:
        match e.payload:
            case CampaignStatusChanged(new=CampaignStatus.STOPPED, stopped_reason=why):
                campaign = s.get(Campaign, e.target_id)
                if campaign is not None and campaign_id in (None, campaign.id):
                    found.append(StoppedNews(campaign_name=campaign.name, reason=why))
            case AnomalyReported(line=line):
                agent_session = s.get(AgentSession, e.target_id)
                if agent_session is not None and campaign_id in (None, agent_session.campaign_id):
                    found.append(AnomalyNews(role=agent_session.kind, agent_session=agent_session.id, line=line))
            case CandidateJudged(verdict=verdict, reason=reason, track=track) if party_of(e.actor) != party:
                candidate = s.get(Candidate, e.target_id)
                # one judgement per track: the news is the one of the candidate's verdict
                mine = track is None or candidate is None or candidate.track in (None, track)
                if candidate is not None and mine and campaign_id in (None, candidate.campaign_id):
                    found.append(VerdictNews(candidate=candidate.id, verdict=str(verdict), reason=str(reason)))
    return found


def ack(s: Session, ids: Sequence[CommentId], actor: Actor) -> list[AckResult]:
    """Acknowledge the inbox items ``ids`` as ``actor`` and commit. Return what happened to each id.

    An id that is not an inbox item, that ``actor``'s party wrote, or that is already acknowledged, is reported and not
    changed.
    """
    now = utcnow()
    results = []
    for comment_id in ids:
        match comment := s.get(Comment, comment_id):
            case None | Comment(priority=None):
                results.append(AckResult(id=comment_id, outcome=AckOutcome.NOT_IN_INBOX, acked_by=None))
            case Comment(author=author) if party_of(author) == actor.party:
                results.append(AckResult(id=comment_id, outcome=AckOutcome.OWN, acked_by=comment.acked_by))
            case Comment(acked_at=datetime()):
                results.append(AckResult(id=comment_id, outcome=AckOutcome.ALREADY, acked_by=comment.acked_by))
            case _:
                comment.acked_at, comment.acked_by = now, actor.signature
                results.append(AckResult(id=comment_id, outcome=AckOutcome.ACKED, acked_by=actor.signature))
    s.commit()
    return results


def reply(s: Session, comment_id: CommentId, body: str, *, author: Actor, priority: int) -> Comment:
    """Answer inbox item ``comment_id`` as ``author`` with a ``decision`` on the root of its thread, and commit.

    As in email, the answer is ``in_reply_to`` the item, and its target is the root (``root_of``): threads stay flat,
    so ``campaign_of`` finds the campaign of every reply. The priority makes the answer an inbox item, so ``news``
    reports it to every orchestrator except its author until it is acknowledged.
    """
    answer = Comment(
        target=TargetRef(TargetKind.COMMENT, root_of(s, s.get_one(Comment, comment_id)).id),
        in_reply_to=comment_id,
        author=author.signature,
        kind=CommentKind.DECISION,
        priority=priority,
        body=body.strip(),
    )
    s.add(answer)
    s.commit()
    return answer


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
        acked_at=iso(c.acked_at) if c.acked_at else None,
        acked_by=c.acked_by,
        body=c.body,
    )


def thread_of(s: Session, comment: Comment) -> Thread:
    """The thread that ``comment`` is in: its root (``root_of``) and the comments linked to the root (``thread``)."""
    root = root_of(s, comment)
    return Thread(
        comment=comment_view(root),
        linked=[Linked(**comment_view(c).model_dump(), relation=relation) for c, relation in thread(s, root)],
    )


def thread(s: Session, root: Comment) -> list[tuple[Comment, Relation]]:
    """Return the comments linked to the root of a thread, with how: its replies, then those it cites or that cite it.

    Each part is oldest first. A citation is ``comment#<id>`` in the body, for example ``Re: comment#12`` in a
    decision. Other comments on the same target are not part of the thread. A campaign collects every research
    finding, and the answer would be lost among them.
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
