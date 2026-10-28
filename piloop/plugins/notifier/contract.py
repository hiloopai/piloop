"""The notifier capability: it tells a human, outside piloop, that an inbox item needs them.

A **notifier** is a named instance, ``[plugins.notifier.<name>]`` in ``config.toml``, and ``[plugins.notifier] use``
there names those that the daemon sends to. Each pass of its machinery (``consumers``) sends each notifier one message
with every item that is due (``due``): an escalation, or an item of priority 0, that nobody has acknowledged and that a
person did not write. A ``notification.sent`` event on the item records each send, so a restart sends nothing twice.

A failed send is a ``notification.failed`` event, and the items stay due: the next try comes after a backoff that the
events give. Modelled on Alertmanager: its receivers, its notification log, and its ``repeat_interval``.

A notifier that also **listens** (``Listens``) takes answers back: a person replies in the thread of a post, and the
reply lands in piloop as a reply on the item, signed as that person (``consumers.received``). Each of its posts is one
item, and ``notification.sent`` names the post (``MessageRef``), so a thread maps back to its item, as Grafana
OnCall maps a Slack ``ts`` to its alert. piloop's answers in that thread go back as ``FOLLOW_UP`` notices. Its
interface is Hermes Agent's platform adapter: a send that returns the post's id, and one inbound callback.
"""

from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import ClassVar, Literal, Protocol, runtime_checkable

from pydantic import Field, field_validator

from piloop.actor import Person
from piloop.db.models import EventModel, ReadBy
from piloop.ids import CommentId, CommentKind
from piloop.plugins.contribution import Lifecycle
from piloop.plugins.host import Capability, Many, Parameters, Pick, Scope, Use
from piloop.plugins.secrets.contract import HomeSecrets


class NoticeKind(StrEnum):
    """Why an item is sent."""

    NEW = 'new'
    """The notifier has not sent it yet."""
    REMINDER = 'reminder'
    """It waits still, ``remind_every`` after the notifier last sent it."""
    FOLLOW_UP = 'follow_up'
    """A comment in the thread of an item that a notifier that listens posted: it goes to that post's thread."""


@dataclass(frozen=True)
class MessageRef:
    """A post on a chat service, opaque to piloop, such as Slack's ``<channel>/<ts>``."""

    id: str


@dataclass(frozen=True)
class Notice:
    """One inbox item, as a notifier sends it."""

    kind: NoticeKind
    """Whether the notifier sends it for the first time, or again."""
    comment: CommentId
    """The item: ``piloop inbox show <comment>`` shows it."""
    comment_kind: CommentKind
    """What the item is: an ``escalation``, a ``decision``, …"""
    priority: int | None
    """0 (urgent) to 4 (someday). None for a comment of the forum, such as an answer in a thread."""
    campaign: str | None
    """The campaign's name, or None for an item of no campaign."""
    author: str
    """Who wrote the item: its signature, such as ``implementer#12``."""
    body: str
    """The item's text, in full."""
    owner: Person | None
    """Whoever added the campaign, when a person did: whom the item concerns on a shared server."""
    link: str | None
    """The item's page, from the notifier's ``link``. None when it has none: show ``piloop inbox show <comment>``."""
    thread: MessageRef | None = None
    """The post to answer under, for a ``FOLLOW_UP``. None for the others: a post of its own."""


class NotDelivered(Exception):
    """A notifier could not send. The message says why, and never holds a secret."""

    def __init__(
        self, reason: str, retry_after: timedelta | None = None, delivered: Sequence[MessageRef | None] = ()
    ) -> None:
        super().__init__(reason)
        self.retry_after = retry_after
        """How long the receiver asked to wait, such as an HTTP 429's ``Retry-After``. None: the notifier's backoff."""
        self.delivered = tuple(delivered)
        """What the notices before the failure became, in order, when the notifier posts them one by one."""


LINK_FIELDS = {'comment': 1, 'campaign': 'enwik8'}
"""The fields that a ``link`` may hold, with a sample value that checks it."""


class Notifier(Parameters, ABC):
    """A notifier: the keys of ``[plugins.notifier.<name>]`` besides ``provider``, and how it sends."""

    link: str | None = None
    """The page of an item, such as ``https://piloop.example.com/inbox/{comment}``. ``{comment}`` is the item's id and
    ``{campaign}`` its campaign's name. Unset: a message names the command that shows the item."""
    remind_every: timedelta | Literal['never'] = timedelta(hours=24)
    """How long after a send an item that nobody acknowledged is sent again. ``"never"``: once."""
    retry: timedelta = Field(default=timedelta(minutes=1), gt=timedelta(0))
    """How long after a failed send the next try comes. It doubles with each failure in a row, up to ``retry_max``."""
    retry_max: timedelta = Field(default=timedelta(hours=1), gt=timedelta(0))
    """The longest wait between two tries."""

    @field_validator('link')
    @classmethod
    def _fields_known(cls, link: str | None) -> str | None:
        if link is not None:
            try:
                link.format(**LINK_FIELDS)
            except (KeyError, IndexError, ValueError) as e:
                fields = ', '.join(f'{{{name}}}' for name in LINK_FIELDS)
                raise ValueError(f'{link!r} is not a link that piloop fills in ({e!r}); it may hold {fields}') from e
        return link

    @abstractmethod
    def send(self, notices: Sequence[Notice], secrets: HomeSecrets) -> Sequence[MessageRef | None]:
        """Send ``notices``, and say what each became: its post, or None when the service gives no id (a webhook).

        ``secrets`` has the credentials that the notifier's keys name. A notifier that listens posts each notice on its
        own, so that a reply in a post's thread names one item.

        Raises:
            NotDelivered: The rest was not sent: the reason, how long to wait when the receiver said, and the posts
                of the notices before it.
        """

    def reminder(self, sent: datetime) -> datetime | None:
        """When an item that was ``sent`` then, and that still waits, is sent again: None for ``never``."""
        return sent + every if isinstance(every := self.remind_every, timedelta) else None

    def wait(self, failures: int, asked: timedelta | None) -> timedelta:
        """How long to wait after the ``failures``-th failure in a row: what the receiver ``asked``, else the backoff."""
        if asked is not None:
            return asked
        return min(self.retry * 2 ** (failures - 1), self.retry_max)


NOTIFIERS = Capability(
    'notifier',
    Notifier,
    table=Many(Scope.HOME, use=Use(Scope.HOME, Pick.ORDERED)),
)
"""Every notifier by name. ``config.toml`` has them, and its ``[plugins.notifier] use`` names those in use."""


class NotificationSent(EventModel):
    """On an inbox item: the notifier ``notifier`` sent it."""

    READ_BY: ClassVar[ReadBy] = ReadBy.READERS
    kind: Literal['notification.sent'] = 'notification.sent'
    notifier: str
    notice: NoticeKind
    message: str | None = None
    """The post that it became (``MessageRef.id``): a reply in its thread is a reply on the item."""


class NotificationReceived(EventModel):
    """On what an answer from a chat service wrote (a reply, an ack's item, a seed): the post ``message`` it came from.

    A redelivered post is dropped, and a reply that came from the service is never sent back to it.
    """

    READ_BY: ClassVar[ReadBy] = ReadBy.READERS
    kind: Literal['notification.received'] = 'notification.received'
    notifier: str
    message: str


@dataclass(frozen=True)
class Reply:
    """A message to the orchestrator in the item's thread, which it answers there (``piloop inbox reply``)."""

    text: str
    priority: int
    """The answer's priority in the inbox, so that it is news for the orchestrator."""


@dataclass(frozen=True)
class Ack:
    """Acknowledge the item, as ``piloop inbox ack`` does."""


@dataclass(frozen=True)
class FileSeed:
    """File a seed in the item's campaign, as ``piloop idea seed`` does, with the item as its pointer."""

    hunch: str


@dataclass(frozen=True)
class Edit:
    """Change the text of a reply that the person wrote before: ``Inbound.message`` is that reply's post."""

    text: str


type Act = Reply | Ack | FileSeed | Edit
"""What a person does from a chat service: the provider maps the service's gestures to one."""


@dataclass(frozen=True)
class Inbound:
    """What a person did in the thread of a post of a notifier that listens."""

    thread: MessageRef
    """The post that the notifier made, whose thread it is in."""
    message: MessageRef
    """What the person posted: a redelivery has the same one."""
    person: Person | None
    """Who did it, from the notifier's map of people. None: the service's user is in no map, so it is refused."""
    act: Act


@dataclass(frozen=True)
class Accepted:
    """piloop wrote what the answer asked: ``said`` tells the person what, such as ``recorded as comment#3``."""

    said: str


@dataclass(frozen=True)
class Again:
    """A redelivery of an answer that piloop wrote before: it writes nothing, and says nothing again."""


@dataclass(frozen=True)
class Declined:
    """piloop wrote nothing: ``reason`` says why, for the person."""

    reason: str


@dataclass(frozen=True)
class Foreign:
    """The thread is no post of the notifier's, such as a talk in its channel: piloop wrote nothing, and says nothing."""


type Receipt = Accepted | Again | Declined | Foreign


@dataclass(frozen=True)
class ListenHost:
    """What the notifier machinery gives a notifier that listens."""

    secrets: HomeSecrets
    received: Callable[[Inbound], Receipt]
    """Write what a person did. Safe from any thread."""
    failed: Callable[[str], None]
    """Record what the listener could not do, such as an answer it could not post: a span event."""


@runtime_checkable
class Listens(Protocol):
    """A notifier that takes answers back from its service."""

    def listen(self, host: ListenHost) -> Lifecycle:
        """The listener: ``start`` connects, and calls ``host.received`` for each answer until ``stop``."""
        ...


class NotificationFailed(EventModel):
    """On the home: the notifier ``notifier`` could not send the items ``comments``."""

    READ_BY: ClassVar[ReadBy] = ReadBy.READERS
    kind: Literal['notification.failed'] = 'notification.failed'
    notifier: str
    error: str
    comments: list[CommentId]
    retry_after: timedelta | None = None
    """How long the receiver asked to wait (``NotDelivered.retry_after``)."""
