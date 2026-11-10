"""The events feed after a cursor: the events of some topics, oldest first, and the cursor to read after next.

The event id is the cursor. SQLite has one writer at a time and allocates an id inside the writing transaction, so
the events at or below the newest id that a reader sees are all committed: a later commit never takes a smaller id.
Every piloop connection, ``piloop sql`` included, keeps it so: events are append-only, and a new event takes the next
id (``piloop.db.triggers.immutable``). Otherwise a reader whose cursor had passed a deleted, moved or explicit id would
miss the event that takes it.
"""

import asyncio
import concurrent.futures
import sqlite3
import threading
from collections.abc import Awaitable, Callable, Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import ColumnElement, func, or_, select
from sqlalchemy.orm import Session

from piloop.db.events import Topic
from piloop.db.models import Event
from piloop.db.session import Database, Watch, read_transaction


@dataclass(frozen=True)
class Page:
    """The events of a read, by id, oldest first."""

    ids: list[int]
    next: int
    """The cursor for the next read. It passes every event that the read examined, those that it left out too, so a
    read that returns nothing still moves on."""
    more: bool
    """Events newer than ``next`` exist already: read again without waiting. They may not match."""


class CursorAhead(Exception):
    """The cursor is past the newest event: it is not a cursor of this database. Read a new snapshot."""

    code = 'cursor_ahead'

    def __init__(self, after: int, newest: int) -> None:
        super().__init__(
            f'cursor {after} is ahead of the feed, which ends at {newest}: read a new snapshot and use its cursor'
        )


def newest(s: Session) -> int:
    """The id of the newest event, 0 when there is none."""
    return s.scalar(select(func.max(Event.id))) or 0


@contextmanager
def snapshot(s: Session) -> Generator[int]:
    """Hold one read transaction on ``s`` and yield its cursor: the id of the newest event that it sees.

    A session that wrote holds its transaction already, which sees one state: its own writes on its snapshot. Its
    cursor can pass its own events, which are committed with whatever it writes from that cursor, or not at all.
    """
    driver = s.connection().connection.driver_connection
    assert isinstance(driver, sqlite3.Connection)  # make_engine opens SQLite only
    if driver.in_transaction:
        yield newest(s)
        return
    with read_transaction(s):
        yield newest(s)


def after(
    s: Session, cursor: int, topics: Sequence[Topic] = (), *, limit: int, where: Sequence[ColumnElement[bool]] = ()
) -> Page:
    """The ids of at most ``limit`` events after ``cursor`` that match ``where`` and a topic, oldest first.

    No topics is every event. Events never change, so a caller reads their rows by id afterwards.

    Raises:
        ValueError: ``cursor`` is negative.
        CursorAhead: ``cursor`` is past the newest event.
    """
    if cursor < 0:
        raise ValueError(f'cursor must be 0 or more, not {cursor}')
    query = select(Event.id).where(Event.id > cursor, *where).order_by(Event.id).limit(limit)
    if topics:
        query = query.where(or_(*(topic.matches() for topic in topics)))
    with snapshot(s) as end:
        if cursor > end:
            raise CursorAhead(cursor, end)
        ids = list(s.scalars(query.where(Event.id <= end)))
    last = ids[-1] if len(ids) == limit else end
    return Page(ids=ids, next=last, more=last < end)


class Span(Protocol):
    """A read of the feed: of the events after ``after``, up to ``next``, as ``Page`` says them."""

    @property
    def after(self) -> int:
        """The cursor that it read after."""
        ...

    @property
    def next(self) -> int:
        """The cursor for the next read: ``Page.next``."""
        ...

    @property
    def more(self) -> bool:
        """Whether events after ``next`` exist already: ``Page.more``."""
        ...


class Shared[S: Span]:
    """The latest read of the feed, which each waiter that it serves takes in place of a read of its own.

    The long-polls of a route wait for the same topics, so one new event wakes all of them at once. A read serves a
    waiter whose cursor is in its span, and that started at the generation of the waiter's latest wake or after it
    (``Watch.generation``): it saw every event that woke the waiter. A cursor at the end of a span with ``more`` needs
    the next page. One read runs at a time. A waiter that no read serves reads, and the others wait for that read
    and look again: K waiters of one wake make one read, not K.
    """

    def __init__(self, db: Database) -> None:
        self._db = db
        self._lock = threading.Lock()
        self._last: tuple[int, S] | None = None
        """The latest read, with the generation that it started at."""
        self._reading: concurrent.futures.Future[None] | None = None
        """Done when the read that runs ends: the long-polls of every server thread's event loop await it."""

    async def read(self, cursor: int, watch: Watch, read: Callable[[int], Awaitable[S]]) -> S:
        """A read after ``cursor`` that saw every event that woke ``watch``: the latest one, else ``read``'s."""
        while True:
            with self._lock:
                if self._last is not None and self._serves(*self._last, cursor, watch):
                    return self._last[1]
                if (reading := self._reading) is None:
                    self._reading = done = concurrent.futures.Future[None]()
                    break
            await asyncio.wait([asyncio.wrap_future(reading)])  # a cancelled wait leaves the read to its reader
        generation = self._db.generation
        try:
            found = await read(cursor)
            with self._lock:
                self._last = generation, found
            return found
        finally:
            with self._lock:
                self._reading = None
            done.set_result(None)

    @staticmethod
    def _serves(generation: int, span: S, cursor: int, watch: Watch) -> bool:
        within = span.after <= cursor < span.next or (cursor == span.next and not span.more)
        return within and generation >= watch.generation
