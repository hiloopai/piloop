"""The notifier capability's machinery: it sends each notifier in use the inbox items that are due for it."""

from dataclasses import dataclass
from datetime import datetime

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from piloop.actor import ACTORS, Person, System
from piloop.campaign.campaigns import CampaignAdded
from piloop.campaign.inbox import items, root_of
from piloop.daemon.loop import Consumer, Kinds, Phase, Soonest, Turn
from piloop.db.events import emit, has, in_order, latest
from piloop.db.models import Campaign, Comment, Event
from piloop.db.triggers.changelog import Commented
from piloop.ids import HOME, CommentKind, TargetKind, TargetRef
from piloop.plugins.contribution import Contribution, DaemonHost
from piloop.plugins.notifier.contract import (
    NOTIFIERS,
    NotDelivered,
    Notice,
    NoticeKind,
    NotificationFailed,
    NotificationSent,
    Notifier,
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

    return Contribution(consumers=(Consumer('notify', NOTIFY, notify, Phase.UPKEEP),))


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
    ids = [n.comment for n in found.notices]
    try:
        notifier.send(found.notices, secrets)
    except NotDelivered as e:
        with host.db.session() as s:
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
        rows = [
            emit(
                s,
                NotificationSent(notifier=name, notice=n.kind),
                TargetRef(TargetKind.COMMENT, n.comment),
                System.DAEMON,
            )
            for n in found.notices
        ]
        s.flush()
        sent_at = rows[0].at  # the earliest: each reminder counts from its own event, as ``due`` reads them
        s.commit()
    host.tracer.event('notification.sent', notifier=name, comments=len(ids))
    later = Soonest()
    later.add(found.later, notifier.reminder(sent_at))
    return later.at


def due(s: Session, name: str, notifier: Notifier, now: datetime) -> Due:
    """The items that the notifier ``name`` sends now: the one rule of what is due.

    An item is an escalation, or of priority 0, that nobody has acknowledged and that a person did not write. It is due
    when the notifier never sent it, or sent it ``remind_every`` ago. Nothing is due while the backoff of the
    failures since the notifier's last send lasts.
    """
    failures = _failures(s, name)
    if failures:
        last_at, last = failures[-1]
        if (retry := last_at + notifier.wait(len(failures), last.retry_after)) > now:
            return Due((), len(failures), retry)
    wanted = [
        (comment, campaign)
        for comment, campaign in items(s)
        if (comment.kind is CommentKind.ESCALATION or comment.priority == 0) and not _by_a_person(comment.author)
    ]
    sent = _sent(s, name, [c.id for c, _ in wanted])
    later = Soonest()
    notices: list[Notice] = []
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


def _notice(s: Session, notifier: Notifier, kind: NoticeKind, comment: Comment, campaign: Campaign | None) -> Notice:
    name = campaign.name if campaign is not None else None
    link = notifier.link.format(comment=comment.id, campaign=name or '') if notifier.link is not None else None
    return Notice(
        kind=kind,
        comment=comment.id,
        comment_kind=comment.kind,
        priority=comment.priority if comment.priority is not None else 0,
        campaign=name,
        author=comment.author,
        body=comment.body,
        owner=_owner(s, campaign) if campaign is not None else None,
        link=link,
    )


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
