"""The notifier capability: it tells a human, outside piloop, that an inbox item needs them.

A **notifier** is a named instance, ``[plugins.notifier.<name>]`` in ``config.toml``, and ``[plugins.notifier] use``
there names those that the daemon sends to. Each pass of its machinery (``consumers``) sends each notifier one message
with every item that is due (``due``): an escalation, or an item of priority 0, that nobody has acknowledged and that a
person did not write. A ``notification.sent`` event on the item records each send, so a restart sends nothing twice.

A failed send is a ``notification.failed`` event, and the items stay due: the next try comes after a backoff that the
events give. Modelled on Alertmanager: its receivers, its notification log, and its ``repeat_interval``.
"""

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import ClassVar, Literal

from pydantic import Field, field_validator

from piloop.actor import Person
from piloop.db.models import EventModel, ReadBy
from piloop.ids import CommentId, CommentKind
from piloop.plugins.host import Capability, Many, Parameters, Pick, Scope, Use
from piloop.plugins.secrets.contract import HomeSecrets


class NoticeKind(StrEnum):
    """Why an item is sent."""

    NEW = 'new'
    """The notifier has not sent it yet."""
    REMINDER = 'reminder'
    """It waits still, ``remind_every`` after the notifier last sent it."""


@dataclass(frozen=True)
class Notice:
    """One inbox item, as a notifier sends it."""

    kind: NoticeKind
    """Whether the notifier sends it for the first time, or again."""
    comment: CommentId
    """The item: ``piloop inbox show <comment>`` shows it."""
    comment_kind: CommentKind
    """What the item is: an ``escalation``, a ``decision``, …"""
    priority: int
    """0 (urgent) to 4 (someday)."""
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


class NotDelivered(Exception):
    """A notifier could not send. The message says why, and never holds a secret."""

    def __init__(self, reason: str, retry_after: timedelta | None = None) -> None:
        super().__init__(reason)
        self.retry_after = retry_after
        """How long the receiver asked to wait, such as an HTTP 429's ``Retry-After``. None: the notifier's backoff."""


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
    def send(self, notices: Sequence[Notice], secrets: HomeSecrets) -> None:
        """Send ``notices`` in one message. ``secrets`` has the credentials that the notifier's keys name.

        Raises:
            NotDelivered: Nothing was sent: the reason, and how long to wait when the receiver said.
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


class NotificationFailed(EventModel):
    """On the home: the notifier ``notifier`` could not send the items ``comments``."""

    READ_BY: ClassVar[ReadBy] = ReadBy.READERS
    kind: Literal['notification.failed'] = 'notification.failed'
    notifier: str
    error: str
    comments: list[CommentId]
    retry_after: timedelta | None = None
    """How long the receiver asked to wait (``NotDelivered.retry_after``)."""
