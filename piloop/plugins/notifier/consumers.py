"""The notifier capability's machinery: it sends each notifier in use the inbox items that are due for it."""

from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import datetime
from typing import assert_never

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from piloop.actor import ACTORS, Person, System
from piloop.campaign import escalations
from piloop.campaign.campaigns import CampaignAdded
from piloop.campaign.inbox import ack, campaign_of, items, root_of
from piloop.campaign.ops import Refused as BacklogRefused
from piloop.daemon.loop import Consumer, Kinds, Phase, Soonest, Turn
from piloop.db.events import emit, has, in_order, latest
from piloop.db.models import Campaign, Comment, Event
from piloop.db.session import Database
from piloop.db.triggers.changelog import Commented
from piloop.ids import HOME, CampaignId, CommentId, CommentKind, TargetKind, TargetRef
from piloop.otel import Tracer
from piloop.plugins.campaign_type.kit.backlog.ops import file_seed
from piloop.plugins.contribution import Contribution, DaemonHost, Leaving, Lifecycle
from piloop.plugins.escalation.contract import At, EscalationView, Never, Now
from piloop.plugins.notifier.contract import (
    NOTIFIERS,
    Accepted,
    Ack,
    Again,
    Declined,
    Edit,
    FileSeed,
    Foreign,
    Inbound,
    ListenHost,
    Listens,
    MessageRef,
    NotDelivered,
    Notice,
    NoticeKind,
    NotificationFailed,
    NotificationReceived,
    NotificationSent,
    Notifier,
    Receipt,
    Reply,
)
from piloop.plugins.secrets.contract import SECRETS, HomeSecrets

NOTIFY: Kinds = frozenset({Commented})
"""What wakes the notifiers: a new comment may be an item to send."""
FAILING_PRIORITY = 1
"""The priority of the inbox item that says a notifier fails: the human reads it today, as a failed send is no news."""


def contribution(host: DaemonHost) -> Contribution:
    """The consumer ``notify``, beside the agents: it sends what is due, and returns when a retry or reminder is."""
    secrets = HomeSecrets(host.db, SECRETS.selected(host.plugins))
    notifiers = NOTIFIERS.instances(host.plugins)
    used = {name: notifiers[name] for name in NOTIFIERS.used(host.plugins)}

    def notify(turn: Turn) -> datetime | None:
        later = Soonest()
        for name, notifier in used.items():
            later.add(send_due(host, secrets, name, notifier, turn.now))
        return later.at

    listeners = tuple(
        notifier.listen(ListenHost(secrets, _answering(host, name, notifier), _failing(host, name)))
        for name, notifier in used.items()
        if isinstance(notifier, Listens)
    )
    return Contribution(
        consumers=(Consumer('notify', NOTIFY, notify, Phase.UPKEEP),),
        lifecycle=Listeners(listeners) if listeners else None,
    )


@dataclass(frozen=True)
class Listeners:
    """The ``Lifecycle`` of every notifier in use that listens: each starts with the daemon and stops with it."""

    each: tuple[Lifecycle, ...]

    def start(self) -> None:
        for listener in self.each:
            listener.start()

    def in_flight(self) -> Mapping[str, Future[None]]:
        return {key: f for listener in self.each for key, f in listener.in_flight().items()}

    def stop(self, leaving: Leaving, timeout: float) -> None:
        for listener in self.each:
            listener.stop(leaving, timeout)


def _answering(host: DaemonHost, name: str, notifier: Notifier) -> Callable[[Inbound], Receipt]:
    def answer(inbound: Inbound) -> Receipt:
        try:
            return received(host.db, host.tracer, name, notifier, inbound)
        except Exception as e:  # it runs in the listener's thread, whose service library would only log what escapes
            host.tracer.event('notification.receive_failed', notifier=name, error=f'{type(e).__name__}: {e}')
            return Declined(f'piloop could not write this: {type(e).__name__}: {e}')

    return answer


def _failing(host: DaemonHost, name: str) -> Callable[[str], None]:
    def failed(error: str) -> None:
        host.tracer.event('notification.listen_failed', notifier=name, error=error)

    return failed


def received(db: Database, tracer: Tracer, name: str, notifier: Notifier, inbound: Inbound) -> Receipt:
    """Write what a person did in the thread of a post of the notifier ``name``, signed as them, and say how it went.

    The thread maps to its item through the ``notification.sent`` event that names the post. A post that the notifier
    did not make is ``Foreign``. A redelivered post is accepted once: its ``notification.received`` is there already.
    """
    if isinstance(inbound.act, Edit):
        return _edit(db, tracer, name, inbound, inbound.act)
    with db.session() as s:
        if _received_already(s, name, inbound.message):
            return Again()
        post = latest(
            s,
            NotificationSent,
            has(
                NotificationSent, notifier=name, message=inbound.thread.id, notice=(NoticeKind.NEW, NoticeKind.REMINDER)
            ),
        )
        if post is None:
            return Foreign()
        if (person := inbound.person) is None:
            return Declined(f'you are not in [plugins.notifier.{name}] people: ask whoever runs piloop to add you')
        item = s.get_one(Comment, post[0].target_id)
        match inbound.act:
            case Reply(text=text, priority=priority):
                answer = Comment(
                    target=TargetRef(TargetKind.COMMENT, root_of(s, item).id),
                    in_reply_to=item.id,
                    author=person.signature,
                    kind=CommentKind.MESSAGE,
                    priority=priority,
                    body=text,
                )
                s.add(answer)
                s.flush()
                target = TargetRef(TargetKind.COMMENT, answer.id)
                said = f'piloop recorded this as comment#{answer.id}, a reply to comment#{item.id}'
            case Ack():
                ack(s, [item.id], person)
                target = TargetRef(TargetKind.COMMENT, item.id)
                said = f'piloop acknowledged comment#{item.id}'
            case FileSeed(hunch=hunch):
                campaign = _campaign(s, item.id)
                if campaign is None or not hunch:
                    return Declined('write the hunch after "idea:", in the thread of an item of a campaign')
                try:
                    seed = file_seed(s, campaign, hunch, actor=person, provenance=f'comment#{item.id}, via {name}')
                except BacklogRefused as e:
                    return Declined(str(e))
                target = TargetRef(TargetKind.IDEA, seed.id)
                said = f'piloop filed this as seed idea#{seed.id}, for research'
            case _:
                assert_never(inbound.act)
        emit(s, NotificationReceived(notifier=name, message=inbound.message.id), target, person)
        s.commit()
    tracer.event('notification.received', notifier=name, act=type(inbound.act).__name__, target=str(target))
    return Accepted(said)


def _edit(db: Database, tracer: Tracer, name: str, inbound: Inbound, edit: Edit) -> Receipt:
    """Change the text of the reply that ``inbound.message`` became, as its author, and put it back in the inbox.

    ``comment.body`` keeps the old text in the feed, and is news for whoever waits for the item, as a new one is. A
    post that became no reply is ``Foreign``: piloop took no answer from it.
    """
    with db.session() as s:
        found = latest(s, NotificationReceived, has(NotificationReceived, notifier=name, message=inbound.message.id))
        if found is None or found[0].target_kind is not TargetKind.COMMENT:
            return Foreign()
        if (person := inbound.person) is None:
            return Declined(f'you are not in [plugins.notifier.{name}] people: ask whoever runs piloop to add you')
        comment = s.get_one(Comment, found[0].target_id)
        if comment.author != person.signature:
            return Declined(f'comment#{comment.id} is by {comment.author}: only its author changes it')
        if comment.body == edit.text:
            return Again()
        comment.body = edit.text
        comment.acked_at, comment.acked_by = None, None  # changed, it waits to be read again
        s.commit()
    tracer.event('notification.received', notifier=name, act='Edit', target=f'comment#{comment.id}')
    return Accepted(f'piloop updated comment#{comment.id}')


def _received_already(s: Session, name: str, message: MessageRef) -> bool:
    found = select(Event.id).where(has(NotificationReceived, notifier=name, message=message.id)).limit(1)
    return s.scalar(found) is not None


def _campaign_row(s: Session, comment: CommentId) -> Campaign | None:
    """The campaign of the comment ``comment``, or None."""
    found = _campaign(s, comment)
    return s.get(Campaign, found) if found is not None else None


def _campaign(s: Session, comment: CommentId) -> CampaignId | None:
    """The campaign of the comment ``comment``, through its target (``inbox.campaign_of``)."""
    found = s.scalar(select(campaign_of(Comment.target_kind, Comment.target_id)).where(Comment.id == comment))
    return CampaignId(found) if found is not None else None


@dataclass(frozen=True)
class Due:
    """What a notifier sends now, the failures in a row before it, and when it has more to send for time alone."""

    notices: tuple[Notice, ...]
    failures: int
    later: datetime | None


def send_due(host: DaemonHost, secrets: HomeSecrets, name: str, notifier: Notifier, now: datetime) -> datetime | None:
    """Send what is due for the notifier ``name``, record what happened, and return when it must run again.

    It sends outside any transaction, as a fact source reads: a write lock held for the post would lock out the rest.
    """
    with host.db.session() as s:
        found = due(s, name, notifier, now)
    if not found.notices:
        return found.later
    try:
        posts = notifier.send(found.notices, secrets)
    except NotDelivered as e:
        sent, left = found.notices[: len(e.delivered)], found.notices[len(e.delivered) :]
        ids = [n.comment for n in left]
        with host.db.session() as s:
            _record(s, name, sent, e.delivered)
            failed = NotificationFailed(notifier=name, error=str(e), comments=ids, retry_after=e.retry_after)
            row = emit(s, failed, HOME, System.DAEMON)
            if not found.failures:
                _report(s, name, ids[0], str(e))
            s.flush()
            retry = row.at + notifier.wait(found.failures + 1, e.retry_after)  # as ``due`` reads it
            s.commit()
        host.tracer.event('notification.failed', notifier=name, comments=len(ids), error=str(e))
        return retry
    with host.db.session() as s:
        sent_at = _record(s, name, found.notices, posts)
        s.commit()
    host.tracer.event('notification.sent', notifier=name, comments=len(found.notices))
    later = Soonest()
    later.add(found.later, notifier.reminder(sent_at) if sent_at is not None else None)
    return later.at


def _record(s: Session, name: str, notices: Sequence[Notice], posts: Sequence[MessageRef | None]) -> datetime | None:
    """Record that ``notices`` were sent as ``posts``, and return when: the earliest event, as ``due`` reads them."""
    rows = [
        emit(
            s,
            NotificationSent(notifier=name, notice=n.kind, message=post.id if post is not None else None),
            TargetRef(TargetKind.COMMENT, n.comment),
            System.DAEMON,
        )
        for n, post in zip(notices, posts, strict=True)
    ]
    s.flush()
    return rows[0].at if rows else None


def due(s: Session, name: str, notifier: Notifier, now: datetime) -> Due:
    """The items that the notifier ``name`` sends now: the one rule of what is due.

    An item is an escalation whose campaign's escalation provider says it reaches a human now (``reach``), or another
    comment of priority 0, that nobody has acknowledged and that a person did not write. It is due when the notifier
    never sent it, or sent it ``remind_every`` ago. Nothing is due while the backoff of the
    failures since the notifier's last send lasts.
    """
    failures = _failures(s, name)
    if failures:
        last_at, last = failures[-1]
        if (retry := last_at + notifier.wait(len(failures), last.retry_after)) > now:
            return Due((), len(failures), retry)
    follow_ups = _follow_ups(s, name)
    notices = [
        _notice(s, notifier, NoticeKind.FOLLOW_UP, comment, _campaign_row(s, comment.id), thread)
        for comment, thread in follow_ups
    ]
    threaded = {comment.id for comment, _ in follow_ups}
    later = Soonest()
    wanted: list[tuple[Comment, Campaign | None]] = []
    for comment, campaign in items(s):
        if _by_a_person(comment.author) or comment.id in threaded:
            continue
        if comment.kind is not CommentKind.ESCALATION:
            wanted += [(comment, campaign)] if comment.priority == 0 else []
            continue
        match escalations.policy(s, campaign.id if campaign else None).reach(escalations.escalated(s, comment), now):
            case Now():
                wanted.append((comment, campaign))
            case At(at=at) if at <= now:
                wanted.append((comment, campaign))
            case At(at=at):
                later.add(at)
            case Never():
                pass
            case reach:
                assert_never(reach)
    sent = _sent(s, name, [c.id for c, _ in wanted])
    for comment, campaign in wanted:
        if (at := sent.get(comment.id)) is None:
            notices.append(_notice(s, notifier, NoticeKind.NEW, comment, campaign))
        elif (again := notifier.reminder(at)) is not None and again <= now:
            notices.append(_notice(s, notifier, NoticeKind.REMINDER, comment, campaign))
        else:
            later.add(again)
    return Due(tuple(notices), len(failures), later.at)


def _failures(s: Session, name: str) -> list[tuple[datetime, NotificationFailed]]:
    """The failures of the notifier ``name`` since its last send, oldest first, with their times."""
    last_sent = s.scalar(select(func.max(Event.id)).where(has(NotificationSent, notifier=name))) or 0
    return [
        (e.at, failed)
        for e, failed in in_order(s, NotificationFailed, has(NotificationFailed, notifier=name), Event.id > last_sent)
    ]


def _follow_ups(s: Session, name: str) -> list[tuple[Comment, MessageRef]]:
    """The comments that go to a thread of the notifier ``name``, oldest first, with the post of that thread.

    Each is a reply on an item that the notifier posted with an id (it listens), written after that post. It did not
    come from that thread (``notification.received``), the daemon did not write it, and the notifier has not sent it.
    """
    posts: dict[int, tuple[datetime, MessageRef]] = {}
    """Each posted item: its first post's time, and its newest post, which a reminder makes."""
    roots = has(NotificationSent, notifier=name, notice=(NoticeKind.NEW, NoticeKind.REMINDER))
    for event, sent in in_order(s, NotificationSent, roots):
        if sent.message is not None:
            first = posts.get(event.target_id, (event.at, MessageRef(sent.message)))[0]
            posts[event.target_id] = (first, MessageRef(sent.message))
    if not posts:
        return []
    replies = s.scalars(
        select(Comment)
        .where(
            Comment.in_reply_to.in_(posts)
            | ((Comment.target_kind == TargetKind.COMMENT) & Comment.target_id.in_(posts)),
            Comment.author != System.DAEMON.signature,
        )
        .order_by(Comment.id)
    ).all()
    done = set(_sent(s, name, [c.id for c in replies])) | _received(s, name, [c.id for c in replies])
    found: list[tuple[Comment, MessageRef]] = []
    for reply in replies:
        item = reply.in_reply_to if reply.in_reply_to in posts else reply.target_id
        first, post = posts[item]
        if reply.id not in done and reply.created_at > first:
            found.append((reply, post))
    return found


def _received(s: Session, name: str, comments: list[int]) -> set[int]:
    """Those of ``comments`` that came from a thread of the notifier ``name``."""
    query = select(Event.target_id).where(
        has(NotificationReceived, notifier=name),
        Event.target_kind == TargetKind.COMMENT,
        Event.target_id.in_(comments),
    )
    return set(s.scalars(query))


def _sent(s: Session, name: str, comments: list[int]) -> dict[int, datetime]:
    """When the notifier ``name`` last sent each of ``comments`` that it sent."""
    query = (
        select(Event.target_id, func.max(Event.at))
        .where(
            has(NotificationSent, notifier=name),
            Event.target_kind == TargetKind.COMMENT,
            Event.target_id.in_(comments),
        )
        .group_by(Event.target_id)
    )
    return {target: at for target, at in s.execute(query)}


def _by_a_person(signature: str) -> bool:
    """Whether a person wrote it: the human at the terminal, or one whom the network named. They need no notice."""
    try:
        actor = ACTORS.validate_python(signature)
    except ValidationError:  # a signature that an older piloop wrote: no person's
        return False
    return isinstance(actor, Person) or actor is System.HUMAN


def _notice(
    s: Session,
    notifier: Notifier,
    kind: NoticeKind,
    comment: Comment,
    campaign: Campaign | None,
    thread: MessageRef | None = None,
) -> Notice:
    name = campaign.name if campaign is not None else None
    link = notifier.link.format(comment=comment.id, campaign=name or '') if notifier.link is not None else None
    return Notice(
        kind=kind,
        comment=comment.id,
        comment_kind=comment.kind,
        priority=comment.priority,
        campaign=name,
        author=comment.author,
        body=comment.body,
        owner=_owner(s, campaign) if campaign is not None else None,
        link=link,
        thread=thread,
        view=_view(s, comment, campaign),
    )


def _view(s: Session, comment: Comment, campaign: Campaign | None) -> EscalationView | None:
    """How an escalation shows, as its campaign's provider says, or None for another comment."""
    if comment.kind is not CommentKind.ESCALATION:
        return None
    return escalations.policy(s, campaign.id if campaign else None).view(escalations.escalated(s, comment))


def _owner(s: Session, campaign: Campaign) -> Person | None:
    """The person who added ``campaign``: the signer of its ``campaign.added`` event, when a person signed it."""
    found = latest(s, CampaignAdded, Event.target == TargetRef(TargetKind.CAMPAIGN, campaign.id))
    if found is None:
        return None
    try:
        actor = ACTORS.validate_python(found[0].actor)
    except ValidationError:  # a signature that an older piloop wrote: no person's
        return None
    return actor if isinstance(actor, Person) else None


def _report(s: Session, name: str, item: int, error: str) -> None:
    """File the first failure in a row as an inbox item: a reply on ``item``, the first item that was not sent."""
    comment = s.get_one(Comment, item)
    s.add(
        Comment(
            target=TargetRef(TargetKind.COMMENT, root_of(s, comment).id),
            in_reply_to=comment.id,
            author=System.DAEMON.signature,
            kind=CommentKind.NOTE,
            priority=FAILING_PRIORITY,
            body=f'notifier {name!r} could not send this: {error}. Fix [plugins.notifier.{name}] in config.toml, or'
            ' the secret that it names; piloop tries again.',
        )
    )
