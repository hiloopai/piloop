"""The notifier capability's machinery: it sends each notifier in use the inbox items that are due for it.

A **route** is a notifier in use and the items that it sends: those of a campaign, by the campaign's
``[plugins.notifier] use``, or those of no campaign, by the ``use`` of ``config.toml``. ``config.toml`` holds the
defaults of every campaign's, so an item goes to the notifiers of exactly one ``use``, and once to each name.
"""

import threading
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import datetime
from typing import assert_never

from pydantic import ValidationError
from sqlalchemy import ColumnElement, func, select
from sqlalchemy.orm import Session

from piloop import deprecated
from piloop.actor import ACTORS, Person, System
from piloop.campaign import escalations
from piloop.campaign.campaigns import CampaignAdded
from piloop.campaign.inbox import ack, campaign_of, items, root_of
from piloop.campaign.ops import Refused as BacklogRefused
from piloop.daemon.loop import Consumer, Kinds, Phase, Soonest, Turn
from piloop.db.events import emit, has, in_order, latest
from piloop.db.models import NOT_RETIRED, Campaign, Comment, Event
from piloop.db.session import Database
from piloop.db.triggers.changelog import CampaignConfigChanged, CampaignStatusChanged, Commented
from piloop.ids import HOME, CampaignId, CampaignStatus, CommentId, CommentKind, TargetKind, TargetRef
from piloop.otel import Tracer
from piloop.plugins.campaign_type.kit.backlog.ids import IDEA
from piloop.plugins.campaign_type.kit.backlog.ops import file_seed
from piloop.plugins.contribution import Contribution, DaemonHost, Leaving, Lifecycle
from piloop.plugins.escalation.contract import At, EscalationView, Never, Now
from piloop.plugins.host import PluginError, Plugins, Scope
from piloop.plugins.notifier.contract import (
    NOTIFIERS,
    Accepted,
    Ack,
    Again,
    Answered,
    Choose,
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
from piloop.plugins.secrets.contract import (
    SECRETS,
    CampaignSecrets,
    HomeSecrets,
    SecretDeleted,
    SecretReader,
    SecretSet,
    SecretsProvider,
)

NOTIFY: Kinds = frozenset({Commented, CampaignStatusChanged})
"""What wakes the notifiers: a new comment may be an item to send, and a reopened campaign's items remind again."""
LISTEN: Kinds = frozenset({CampaignAdded, CampaignConfigChanged, CampaignStatusChanged, SecretSet, SecretDeleted})
"""What wakes the listeners: a campaign's notifiers, or a credential that one listens with, may have changed."""
FAILING_PRIORITY = 1
"""The priority of the inbox item that says a notifier fails: the human reads it today, as a failed send is no news."""


@dataclass(frozen=True)
class Route:
    """A notifier in use, by its name, and whose items it sends with whose credentials."""

    owner: CampaignId | None
    """The campaign whose items it sends, or None for the items of no campaign."""
    name: str
    notifier: Notifier
    secrets: SecretReader
    """The campaign's secrets over the home's, or the home's."""


def home_notifiers(defaults: Mapping[str, object]) -> dict[str, Notifier]:
    """The notifiers that ``config.toml`` uses (``DaemonHost.defaults``), by name: those of the items of no campaign.

    Raises:
        PluginError: Its ``use`` names an instance that it does not define.
    """
    plugins: Plugins = {NOTIFIERS.name: NOTIFIERS.validated_table(defaults.get(NOTIFIERS.name, {}), Scope.CAMPAIGN)}
    return _used(plugins, 'config.toml')


def _used(plugins: Plugins, where: str) -> dict[str, Notifier]:
    found = NOTIFIERS.instances(plugins)
    if unknown := [name for name in NOTIFIERS.used(plugins) if name not in found]:
        raise PluginError(f'{where}: [plugins.notifier] use names {", ".join(unknown)}, which no table defines')
    return {name: found[name] for name in NOTIFIERS.used(plugins)}


def routes(s: Session, db: Database, provider: SecretsProvider, home: Mapping[str, Notifier]) -> list[Route]:
    """Every route: the home's for the items of no campaign, then each campaign's, by its stored config.

    A campaign whose stored config no longer validates has none: the pass over the campaigns traces it.
    """
    found = [Route(None, name, notifier, HomeSecrets(db, provider)) for name, notifier in home.items()]
    for campaign in s.scalars(select(Campaign).where(NOT_RETIRED).order_by(Campaign.id)):
        try:
            plugins = campaign.config.plugins
        except ValidationError:
            continue
        secrets = CampaignSecrets(db, provider, campaign.id, campaign.name)
        used = _used(plugins, campaign.name) if _configured(s, campaign) else _stored_before(home)
        found += [Route(campaign.id, name, notifier, secrets) for name, notifier in used.items()]
    return found


def _configured(s: Session, campaign: Campaign) -> bool:
    """Whether the campaign's stored config has ``[plugins.notifier]``: one that ``piloop init`` stored since it may."""
    stored = select(func.json_type(Campaign.config, '$.plugins.notifier')).where(Campaign.id == campaign.id)
    return s.scalar(stored) is not None


def _stored_before(home: Mapping[str, Notifier]) -> Mapping[str, Notifier]:
    deprecated.until(
        '0.6.0',
        'a campaign that 0.4 stored, before notifiers were per campaign, gets the notifiers of config.toml until its'
        ' next piloop init: delete _configured and this bridge',
    )
    return home


def contribution(host: DaemonHost) -> Contribution:
    """The consumers ``notify``, beside the agents, and ``listeners``, first in a turn.

    ``notify`` sends what is due on each route, and returns when a retry or reminder is. ``listeners`` opens and
    closes the listeners as campaigns and secrets change (``Listeners``).

    Raises:
        PluginError: ``config.toml``'s ``[plugins.notifier]`` names an instance that it does not define.
    """
    home = home_notifiers(host.defaults)
    provider = SECRETS.selected(host.plugins)

    def notify(turn: Turn) -> datetime | None:
        with host.db.session() as s:
            found = routes(s, host.db, provider, home)
        later = Soonest()
        for route in found:
            later.add(send_due(host, route, turn.now))
        return later.at

    listeners = Listeners(host, provider, home)
    return Contribution(
        consumers=(
            Consumer('listeners', LISTEN, listeners.run, Phase.CONFIGURE),
            Consumer('notify', NOTIFY, notify, Phase.UPKEEP),
        ),
        lifecycle=listeners,
    )


class Listeners:
    """The listeners of the routes whose notifier listens: one for each connection (``Listens.connection``).

    Every route of a connection shares its listener, which the first route opens: a service such as Slack splits the
    events of one app across its open connections, so a second one would lose answers. ``run`` opens the connections
    that a route needs, closes those that none needs, and reopens one whose first route changed. It runs when the
    daemon starts, and when a campaign or a secret changes: no restart is needed.
    """

    def __init__(self, host: DaemonHost, provider: SecretsProvider, home: Mapping[str, Notifier]) -> None:
        self._host, self._provider, self._home = host, provider, home
        self._lock = threading.Lock()
        self._open: dict[str, tuple[Route, Lifecycle]] = {}
        """Each open connection: the route that opened it, and its listener."""
        self._shared: dict[str, tuple[Route, ...]] = {}
        """The routes of each connection, which its answers are for."""

    def start(self) -> None:
        self._reconcile()

    def in_flight(self) -> Mapping[str, Future[None]]:
        return {}

    def stop(self, leaving: Leaving, timeout: float) -> None:
        with self._lock:
            for _, listener in self._open.values():
                listener.stop(leaving, timeout)
            self._open.clear()

    def run(self, _turn: Turn) -> datetime | None:
        """The consumer ``listeners``: open, close and reopen the listeners, as the routes now need them."""
        self._reconcile()
        return None

    def _reconcile(self) -> None:
        with self._host.db.session() as s:
            found = routes(s, self._host.db, self._provider, self._home)
        wanted: dict[str, list[tuple[Route, Listens]]] = {}
        for route in found:
            if not isinstance(listens := route.notifier, Listens):
                continue
            if (key := listens.connection(route.secrets)) is None:
                error = 'a credential that it listens with is not set: see `piloop secret list`'
                self._host.tracer.event('notification.listen_failed', notifier=route.name, error=error)
                continue
            wanted.setdefault(key, []).append((route, listens))
        with self._lock:
            for key in [k for k, (first, _) in self._open.items() if k not in wanted or wanted[k][0][0] != first]:
                self._open.pop(key)[1].stop(Leaving.STOP, 0)  # a listener has nothing in flight to wait for
            self._shared = {key: tuple(route for route, _ in shared) for key, shared in wanted.items()}
            for key, [(first, listens), *_] in wanted.items():
                if key not in self._open:
                    listener = listens.listen(
                        ListenHost(first.secrets, self._answering(key), _failing(self._host, first.name))
                    )
                    listener.start()
                    self._open[key] = (first, listener)

    def _answering(self, key: str) -> Callable[[Inbound], Receipt]:
        def shared() -> tuple[Route, ...]:
            with self._lock:
                return self._shared.get(key, ())

        return _answering(self._host, shared)


def _answering(host: DaemonHost, shared: Callable[[], Sequence[Route]]) -> Callable[[Inbound], Receipt]:
    def answer(inbound: Inbound) -> Receipt:
        try:
            return received(host.db, host.tracer, shared(), inbound)
        except Exception as e:  # it runs in the listener's thread, whose service library would only log what escapes
            names = ','.join(sorted({r.name for r in shared()}))
            host.tracer.event('notification.receive_failed', notifier=names, error=f'{type(e).__name__}: {e}')
            return Declined(f'piloop could not write this: {type(e).__name__}: {e}')

    return answer


def _failing(host: DaemonHost, name: str) -> Callable[[str], None]:
    def failed(error: str) -> None:
        host.tracer.event('notification.listen_failed', notifier=name, error=error)

    return failed


def received(db: Database, tracer: Tracer, shared: Sequence[Route], inbound: Inbound) -> Receipt:
    """Write what a person did in the thread of a post of a route of ``shared``, signed as them, and say how it went.

    The thread maps to its item through the ``notification.sent`` event that names the post, and the route of that
    item says who the person is. A choice answers an escalation through ``escalations.choose``, unless someone answered
    or acknowledged it already: then it writes nothing (``Answered``). A post that no route
    of ``shared`` made is ``Foreign``. A redelivered post is accepted once: its ``notification.received`` is there
    already.
    """
    names = tuple(sorted({r.name for r in shared}))
    if isinstance(inbound.act, Edit):
        return _edit(db, tracer, shared, inbound, inbound.act)
    with db.session() as s:
        if _received_already(s, names, inbound.message):
            return Again()
        post = latest(
            s,
            NotificationSent,
            has(
                NotificationSent,
                notifier=names,
                message=inbound.thread.id,
                notice=(NoticeKind.NEW, NoticeKind.REMINDER),
            ),
        )
        if post is None:
            return Foreign()
        item = s.get_one(Comment, post[0].target_id)
        route = _route_of(s, shared, post[1].notifier, item.id)
        if (person := _person(route, inbound.member)) is None:
            return Declined(
                f'you are not in [plugins.notifier.{route.name}] people: ask whoever runs piloop to add you'
            )
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
                    seed = file_seed(
                        s, campaign, hunch, actor=person, provenance=f'comment#{item.id}, via {route.name}'
                    )
                except BacklogRefused as e:
                    return Declined(str(e))
                target = TargetRef(IDEA, seed.id)
                said = f'piloop filed this as seed idea#{seed.id}, for research'
            case Choose(choice=choice):
                if (done := escalations.settled(s, item)) is not None:
                    return _answered(item.id, done)
                try:
                    decision = escalations.choose(s, item.id, choice, author=person)
                except escalations.Refused as e:
                    return Declined(str(e))
                target = TargetRef(TargetKind.COMMENT, decision.id)
                said = f'piloop recorded comment#{decision.id}, {decision.body}'
            case _:
                assert_never(inbound.act)
        emit(s, NotificationReceived(notifier=route.name, message=inbound.message.id), target, person)
        s.commit()
    tracer.event('notification.received', notifier=route.name, act=type(inbound.act).__name__, target=str(target))
    return Accepted(said)


def _answered(item: CommentId, done: escalations.Settled) -> Answered:
    """What a choice on ``item``, which ``done`` settled before, tells the person: it records nothing."""
    match done.decision:
        case None:
            said = f'comment#{item} was acknowledged already, by {done.by}. piloop recorded nothing.'
        case decision:
            said = f'comment#{item} was answered already, by {done.by}: {decision} piloop recorded nothing.'
    return Answered(said, done.by, done.decision)


def _route_of(s: Session, shared: Sequence[Route], name: str, comment: CommentId) -> Route:
    """The route of ``shared`` that sent the item ``comment`` as ``name``: the one of the item's campaign.

    A campaign that changed its notifiers since keeps no route of the name: another route of it answers.
    """
    mine = [r for r in shared if r.name == name]
    owner = _campaign(s, comment)
    return next((r for r in mine if r.owner == owner), mine[0])


def _person(route: Route, member: str) -> Person | None:
    return route.notifier.person(member) if isinstance(route.notifier, Listens) else None


def _edit(db: Database, tracer: Tracer, shared: Sequence[Route], inbound: Inbound, edit: Edit) -> Receipt:
    """Change the text of the reply that ``inbound.message`` became, as its author, and put it back in the inbox.

    ``comment.body`` keeps the old text in the feed, and is news for whoever waits for the item, as a new one is. A
    post that became no reply is ``Foreign``: piloop took no answer from it.
    """
    names = tuple(sorted({r.name for r in shared}))
    with db.session() as s:
        found = latest(s, NotificationReceived, has(NotificationReceived, notifier=names, message=inbound.message.id))
        if found is None or found[0].target_kind != TargetKind.COMMENT:
            return Foreign()
        comment = s.get_one(Comment, found[0].target_id)
        route = _route_of(s, shared, found[1].notifier, comment.id)
        if (person := _person(route, inbound.member)) is None:
            return Declined(
                f'you are not in [plugins.notifier.{route.name}] people: ask whoever runs piloop to add you'
            )
        if comment.author != person.signature:
            return Declined(f'comment#{comment.id} is by {comment.author}: only its author changes it')
        if comment.body == edit.text:
            return Again()
        comment.body = edit.text
        comment.acked_at, comment.acked_by = None, None  # changed, it waits to be read again
        s.commit()
    tracer.event('notification.received', notifier=route.name, act='Edit', target=f'comment#{comment.id}')
    return Accepted(f'piloop updated comment#{comment.id}')


def _received_already(s: Session, names: tuple[str, ...], message: MessageRef) -> bool:
    found = select(Event.id).where(has(NotificationReceived, notifier=names, message=message.id)).limit(1)
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


def send_due(host: DaemonHost, route: Route, now: datetime) -> datetime | None:
    """Send what is due on ``route``, record what happened, and return when it must run again.

    It sends outside any transaction, as a fact source reads: a write lock held for the post would lock out the rest.
    """
    name, notifier = route.name, route.notifier
    with host.db.session() as s:
        found = due(s, route, now)
    if not found.notices:
        return found.later
    try:
        posts = notifier.send(found.notices, route.secrets)
    except NotDelivered as e:
        sent, left = found.notices[: len(e.delivered)], found.notices[len(e.delivered) :]
        ids = [n.comment for n in left]
        with host.db.session() as s:
            _record(s, name, sent, e.delivered)
            failed = NotificationFailed(
                notifier=name, error=str(e), comments=ids, campaign=route.owner, retry_after=e.retry_after
            )
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


def due(s: Session, route: Route, now: datetime) -> Due:
    """The items that ``route`` sends now: the one rule of what is due.

    An item is an escalation whose campaign's escalation provider says it reaches a human now (``reach``), or another
    comment of priority 0, of the route's campaign (of none for the home's), that nobody has acknowledged, that a
    person did not write, and whose priority is the notifier's ``max_priority`` or more urgent. It is due when no notifier of the route's name sent it, or sent it ``remind_every`` ago while
    its campaign is active: a stopped campaign's items wait in the inbox, and remind again once it is reopened.
    Nothing is due while the backoff of the route's failures since its last send lasts.
    """
    name, notifier = route.name, route.notifier
    failures = _failures(s, route)
    if failures:
        last_at, last = failures[-1]
        if (retry := last_at + notifier.wait(len(failures), last.retry_after)) > now:
            return Due((), len(failures), retry)
    follow_ups = [(c, thread) for c, thread in _follow_ups(s, name) if _campaign(s, c.id) == route.owner]
    notices = [
        _notice(s, notifier, NoticeKind.FOLLOW_UP, comment, _campaign_row(s, comment.id), thread)
        for comment, thread in follow_ups
    ]
    threaded = {comment.id for comment, _ in follow_ups}
    later = Soonest()
    wanted: list[tuple[Comment, Campaign | None]] = []
    for comment, campaign in items(s):
        if (
            (campaign.id if campaign is not None else None) != route.owner
            or _by_a_person(comment.author)
            or comment.id in threaded
            or comment.priority is None
            or comment.priority > notifier.max_priority
        ):
            continue
        if comment.kind != CommentKind.ESCALATION:
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
        elif campaign is not None and campaign.status is not CampaignStatus.ACTIVE:
            continue
        elif (again := notifier.reminder(at)) is not None and again <= now:
            notices.append(_notice(s, notifier, NoticeKind.REMINDER, comment, campaign))
        else:
            later.add(again)
    return Due(tuple(notices), len(failures), later.at)


def _failures(s: Session, route: Route) -> list[tuple[datetime, NotificationFailed]]:
    """The failures of ``route`` since its last send, oldest first, with their times."""
    sends = (
        has(NotificationSent, notifier=route.name),
        _is(campaign_of(Event.target_kind, Event.target_id), route.owner),
    )
    last_sent = s.scalar(select(func.max(Event.id)).where(*sends)) or 0
    of = has(NotificationFailed, notifier=route.name), _is(func.json_extract(Event.payload, '$.campaign'), route.owner)
    return [(e.at, failed) for e, failed in in_order(s, NotificationFailed, *of, Event.id > last_sent)]


def _is(column: ColumnElement[int | None], owner: CampaignId | None) -> ColumnElement[bool]:
    """Select where ``column`` is the campaign ``owner``, or no campaign for None."""
    return column.is_(None) if owner is None else column == owner


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
    if comment.kind != CommentKind.ESCALATION or comment.form is None:  # free text that an older piloop filed
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
