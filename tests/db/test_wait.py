"""``Database.wait_until``: a waiter wakes on an event of its topics, of this process or another, and gives up."""

import asyncio
import gc
import sqlite3
import threading
import time
import weakref
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.pool import PoolProxiedConnection, QueuePool

from piloop.actor import System
from piloop.db import feed, session as session_module
from piloop.db.events import NewEvent, Topic, emit
from piloop.db.models import Campaign, EventModel, Pause
from piloop.db.session import Database, DatabaseClosed
from piloop.db.triggers.changelog import CampaignConfigChanged, CampaignPausedChanged, Changed, LimitSet
from piloop.ids import TargetKind, TargetRef
from piloop.plugins.executor.kit.leases import LeasePrompted, WorkerDrained
from tests.conftest import at_head, wait_until

PAUSED = [Topic(CampaignPausedChanged)]


def rename(db: Database, campaign: Campaign) -> None:
    with db.session() as s:
        s.get_one(Campaign, campaign.id).name = 'renamed'
        s.commit()


def renamed(campaign: Campaign) -> Callable[[Session], bool]:
    return lambda s: s.get_one(Campaign, campaign.id).name == 'renamed'


def pause(db: Database, campaign: Campaign) -> None:
    """A change that adds an event: ``campaign.paused``."""
    with db.session() as s:
        row = s.get_one(Campaign, campaign.id)
        row.paused, row.paused_reason, row.paused_by = Pause.ALL, 'a test', 'human'
        s.commit()


def paused(campaign: Campaign) -> Callable[[Session], bool]:
    return lambda s: s.get_one(Campaign, campaign.id).paused is Pause.ALL


def test_a_waiter_wakes_on_an_event_of_this_process(db: Database, campaign: Campaign) -> None:
    seen: list[bool] = []
    waiting = threading.Thread(target=lambda: seen.append(db.wait_until(paused(campaign), on=PAUSED)))
    waiting.start()
    time.sleep(0.1)
    pause(db, campaign)
    waiting.join(10)
    assert seen == [True]


def config_changed(db: Database, campaign_id: int) -> None:
    """An event on campaign#``campaign_id``, which need not exist: ``campaign.config``."""
    with db.session() as s:
        emit(s, CampaignConfigChanged(), TargetRef(TargetKind.CAMPAIGN, campaign_id), System.DAEMON)
        s.commit()


def limit_set(db: Database, campaign_id: int) -> None:
    """An event of another kind on campaign#``campaign_id``."""
    with db.session() as s:
        emit(s, LimitSet(key='k', value=1), TargetRef(TargetKind.CAMPAIGN, campaign_id), System.DAEMON)
        s.commit()


def test_a_commit_wakes_only_the_waiters_of_its_events(db: Database, campaign: Campaign) -> None:
    """With many idle waiters, a commit without an event and an event of no waiter's topic read nothing again, and an
    event of one waiter's topic reads that one again alone.

    Every commit woke every waiter once, and each read the database again: with one waiter per agent, each heartbeat
    cost a read per agent, and one daemon saturated at about 20 agents.
    """
    idle = 20
    reads, released = [0] * idle, [threading.Event() for _ in range(idle)]

    def waiter(i: int) -> None:
        def done(_s: Session) -> bool:
            reads[i] += 1
            return released[i].is_set()

        on = [Topic(CampaignConfigChanged, target=TargetRef(TargetKind.CAMPAIGN, 100 + i))]
        assert db.wait_until(done, on=on, timeout=30)

    asyncio.run(ringing(db))  # the doorbell's first read, which wakes every waiter, is done
    waiters = [threading.Thread(target=waiter, args=(i,)) for i in range(idle)]
    for w in waiters:
        w.start()
    wait_until(lambda: reads == [1] * idle, 'the first read of each waiter')
    rename(db, campaign)  # a commit without an event: a heartbeat, a lease renewal
    config_changed(db, 99)  # an event of the waiters' kind on another row
    limit_set(db, 100)  # an event of another kind on the first waiter's row
    released[0].set()
    config_changed(db, 100)  # the first waiter's event
    waiters[0].join(10)
    assert not waiters[0].is_alive()
    assert reads == [2] + [1] * (idle - 1), 'only the first waiter read again, once'
    for i in range(1, idle):
        released[i].set()
    db.notify()
    for w in waiters:
        w.join(10)
    assert reads == [2] * idle


def test_a_waiter_wakes_on_an_event_of_another_process(db: Database, campaign: Campaign) -> None:
    other = Database(str(db.engine.url), actor=System.HUMAN)  # its commits notify its own waiters, not this one's
    seen: list[bool] = []
    waiting = threading.Thread(target=lambda: seen.append(db.wait_until(paused(campaign), on=PAUSED)))
    waiting.start()
    time.sleep(0.1)
    began = time.monotonic()
    pause(other, campaign)
    other.close()
    waiting.join(10)
    assert seen == [True]
    assert time.monotonic() - began < 5, 'the doorbell rang'


async def ringing(db: Database) -> None:
    """Start the doorbell and wait for its first read, which wakes the waiters."""
    with db.watching([]) as watch:
        await watch.changed(30)


def test_an_event_before_the_doorbells_first_read_wakes_the_waiters(
    db: Database, campaign: Campaign, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A waiter reads, then waits: the doorbell that its wait starts reads the feed's head after that read."""
    other = Database(str(db.engine.url), actor=System.HUMAN)
    newest = session_module._newest_event

    def late(conn: PoolProxiedConnection) -> int:
        monkeypatch.setattr(session_module, '_newest_event', newest)
        pause(other, campaign)  # after the waiter read, before the doorbell's first read
        return newest(conn)

    async def waits() -> None:
        with db.watching(PAUSED) as watch:
            began = time.monotonic()
            await watch.changed(10)
            assert time.monotonic() - began < 5, "the doorbell's first read woke it: the event is in its head"

    monkeypatch.setattr(session_module, '_newest_event', late)
    asyncio.run(waits())
    other.close()


def test_a_commit_of_another_process_without_an_event_wakes_no_waiter(db: Database, campaign: Campaign) -> None:
    """A heartbeat or a renewal of another process must not wake every waiter: only new events do."""
    asyncio.run(ringing(db))
    other = Database(str(db.engine.url), actor=System.HUMAN)
    waiting = threading.Thread(target=lambda: db.wait_until(renamed(campaign), on=[Topic()], timeout=1))
    began = time.monotonic()
    waiting.start()
    time.sleep(0.1)
    rename(other, campaign)
    other.close()
    waiting.join(10)
    assert time.monotonic() - began >= 0.9, 'it read again only at its timeout'


def test_notify_wakes_a_waiter_to_check_its_give_up(db: Database) -> None:
    asked = threading.Event()
    seen: list[bool] = []
    waiting = threading.Thread(target=lambda: seen.append(db.wait_until(lambda _s: False, on=[], give_up=asked.is_set)))
    waiting.start()
    time.sleep(0.1)
    asked.set()
    db.notify()
    waiting.join(5)
    assert seen == [False]


def test_a_closed_database_makes_its_waiters_give_up(db: Database) -> None:
    asyncio.run(ringing(db))  # the doorbell holds its connection when the close comes
    seen: list[bool] = []
    waiting = threading.Thread(target=lambda: seen.append(db.wait_until(lambda _s: False, on=[Topic()])))
    waiting.start()
    time.sleep(0.1)
    db.close()
    waiting.join(10)
    assert not waiting.is_alive() and seen == [False]
    assert db.wait_until(lambda _s: True, on=[Topic()]) is False, 'and a new waiter gives up at once'
    assert db._doorbell is not None
    db._doorbell.join(5)
    assert not db._doorbell.is_alive(), 'the doorbell stops, and its connection went back before the pool closed'


def test_a_waiter_gives_up_when_asked(db: Database) -> None:
    """Whoever sets what ``give_up`` reads calls ``notify``: a waiter that checked it already waits for a wake."""
    asked, checked = threading.Event(), threading.Event()

    def give_up() -> bool:
        checked.set()
        return asked.is_set()

    seen: list[bool] = []
    waiting = threading.Thread(target=lambda: seen.append(db.wait_until(lambda _s: False, on=[], give_up=give_up)))
    waiting.start()
    assert checked.wait(10)  # it checked before it was asked, and waits for a wake now
    asked.set()
    db.notify()
    waiting.join(10)
    assert seen == [False]


def test_close_lets_a_read_in_flight_end_before_it_closes_the_connections(db: Database) -> None:
    """A connection that a read holds while the pool goes is never closed, and Python warns when it collects it."""
    reading, release = threading.Event(), threading.Event()

    def slow(_s: Session) -> bool:
        reading.set()
        release.wait(10)
        return False

    waiting = threading.Thread(target=lambda: db.wait_until(slow, on=[]))
    waiting.start()
    assert reading.wait(10)
    closing = threading.Thread(target=db.close)
    closing.start()
    closing.join(0.2)
    assert closing.is_alive(), 'it waits for the read'
    release.set()
    closing.join(10)
    waiting.join(10)
    assert not closing.is_alive() and not waiting.is_alive()


def test_close_lets_a_session_in_flight_end_before_it_closes_the_connections(db: Database) -> None:
    """Any session, not only a waiter's read: a thread that the daemon did not join can still be in one."""
    holding, release = threading.Event(), threading.Event()
    held: list[sqlite3.Connection] = []

    def hold() -> None:
        with db.session() as s:
            s.execute(select(1))
            raw = s.connection().connection.dbapi_connection
            assert isinstance(raw, sqlite3.Connection)
            held.append(raw)
            holding.set()
            release.wait(10)

    holder = threading.Thread(target=hold)
    holder.start()
    assert holding.wait(10)
    closing = threading.Thread(target=db.close)
    closing.start()
    closing.join(0.2)
    assert closing.is_alive(), 'it waits for the session'
    release.set()
    closing.join(10)
    holder.join(10)
    with pytest.raises(sqlite3.ProgrammingError, match='closed database'):
        held[0].execute('SELECT 1')


def test_a_session_opened_before_the_close_gets_no_connection_after_it(db: Database) -> None:
    """Its first statement fails loudly, and opens no connection that nothing would close."""
    with db.session() as s:
        db.close()
        with pytest.raises(DatabaseClosed, match='the database is closed'):
            s.execute(select(1))


def test_a_closed_database_refuses_a_new_session(db: Database) -> None:
    """A late caller fails loudly: a session after the close would open a connection that nothing closes."""
    db.close()
    with pytest.raises(DatabaseClosed, match='the database is closed'):
        db.session()
    with pytest.raises(DatabaseClosed, match='the database is closed'), db.acting(System.HUMAN):
        pass


def test_a_closed_database_that_committed_is_freed_and_closes_twice(tmp_path: Path, schema: Path) -> None:
    """Its commit listener sits on the sessionmaker's class, which lives as long as the process: each database that a
    process opened, with its engine's compiled statements, stayed in memory."""

    def committed_and_closed() -> weakref.ref[Database]:
        db = Database(f'sqlite:///{at_head(schema, tmp_path / "piloop.db")}', actor=System.HUMAN)
        with db.session() as s:
            s.execute(select(1))
            s.commit()
        db.close()
        db.close()
        return weakref.ref(db)

    freed = committed_and_closed()
    gc.collect()
    assert freed() is None


def test_a_waiter_gives_up_at_its_timeout(db: Database) -> None:
    began = time.monotonic()
    assert db.wait_until(lambda _s: False, on=[Topic()], timeout=0.1) is False
    assert time.monotonic() - began < 5


def test_changed_returns_on_an_event_of_this_process_and_holds_no_thread(db: Database, campaign: Campaign) -> None:
    async def waits() -> None:
        await ringing(db)
        with db.watching(PAUSED) as watch:
            began = time.monotonic()
            await watch.changed(0.01)
            assert time.monotonic() - began < 5, 'nothing committed: it returns at its timeout'
            threading.Timer(0.1, pause, [db, campaign]).start()  # another thread, as the daemon's writers are
            began = time.monotonic()
            await watch.changed(30)
            assert time.monotonic() - began < 10

    asyncio.run(waits())


def test_a_wake_while_the_waiter_reads_is_kept_for_its_next_wait(db: Database, campaign: Campaign) -> None:
    """The event came after the read began, so the read may have missed it: the next wait returns at once."""

    async def waits() -> None:
        await ringing(db)
        with db.watching(PAUSED) as watch:
            pause(db, campaign)  # as the waiter reads
            wait_until(lambda: watch._woken, 'the wake')
            began = time.monotonic()
            await watch.changed(30)
            assert time.monotonic() - began < 5

    asyncio.run(waits())


def test_two_wakes_before_the_waiters_loop_runs_resolve_its_wait_once(db: Database) -> None:
    async def waits() -> None:
        await ringing(db)  # else the doorbell's first wake can come before the wait, which then never awaits
        with db.watching([]) as watch:
            waiting = asyncio.create_task(watch.changed(30))
            await asyncio.sleep(0)  # it waits on its future now
            watch.wake(2)
            watch.wake(1)  # its callback finds the future resolved
            assert watch.generation == 2, 'the latest wake of the two, in whichever order they came'
            await asyncio.wait_for(waiting, 10)

    asyncio.run(waits())


@dataclass(frozen=True)
class Read:
    """A read of the feed for ``feed.Shared``: its span."""

    after: int
    next: int
    more: bool = False


def test_the_waiters_of_one_wake_share_one_read_and_a_later_wake_or_another_span_reads_again(db: Database) -> None:
    shared = feed.Shared[Read](db)
    reads: list[int] = []
    gate = asyncio.Event()

    async def read(after: int) -> Read:
        reads.append(after)
        await gate.wait()
        return Read(after, after + 5, more=after == 2)

    async def waits() -> None:
        await ringing(db)  # its first wake comes before the reads
        with db.watching([]) as first, db.watching([]) as second, db.watching([]) as gone:
            leading = asyncio.create_task(shared.read(3, first, read))
            following = asyncio.create_task(shared.read(4, second, read))
            cancelled = asyncio.create_task(shared.read(4, gone, read))
            await asyncio.sleep(0)  # the first reads, the others wait for its read
            cancelled.cancel()
            gate.set()
            assert await leading is await following and reads == [3], 'one read for both cursors in its span'
            assert await shared.read(8, first, read) is await leading, 'a cursor at its end, and no more to read'
            db.notify()  # a wake after that read: it did not see what woke them
            assert (await shared.read(8, first, read), reads) == (Read(8, 13), [3, 8])
            assert (await shared.read(2, second, read), reads) == (Read(2, 7, more=True), [3, 8, 2]), 'before it'
            assert (await shared.read(7, second, read), reads) == (Read(7, 12), [3, 8, 2, 7]), 'its next page'
        assert cancelled.cancelled()

    asyncio.run(waits())


def test_a_read_that_fails_fails_its_reader_and_a_waiter_for_it_reads_again(db: Database) -> None:
    shared = feed.Shared[Read](db)
    reads: list[int] = []
    gate = asyncio.Event()

    async def read(after: int) -> Read:
        reads.append(after)
        if len(reads) == 1:
            await gate.wait()
            raise sqlite3.OperationalError('disk I/O error')
        return Read(after, after + 1)

    async def waits() -> None:
        await ringing(db)  # its first wake comes before the reads
        with db.watching([]) as first, db.watching([]) as second:
            leading = asyncio.create_task(shared.read(1, first, read))
            following = asyncio.create_task(shared.read(1, second, read))
            await asyncio.sleep(0)
            gate.set()
            with pytest.raises(sqlite3.OperationalError, match='disk I/O error'):
                await leading
            assert (await following, reads) == (Read(1, 2), [1, 1])

    asyncio.run(waits())


def test_changed_waits_to_its_timeout_and_returns_at_once_once_closed(db: Database, campaign: Campaign) -> None:
    async def waits() -> None:
        await ringing(db)
        with db.watching(PAUSED) as watch:
            rename(db, campaign)
            began = time.monotonic()
            await watch.changed(0.5)
            assert time.monotonic() - began >= 0.4, 'a commit without an event does not wake it'
            threading.Timer(0.1, db.close).start()
            began = time.monotonic()
            await watch.changed(30)
            assert time.monotonic() - began < 10, 'the close resolves it'
            began = time.monotonic()
            await watch.changed(30)
            assert time.monotonic() - began < 5, 'once closed, at once'
        with db.watching(PAUSED) as late:
            began = time.monotonic()
            late.wait(30)
            assert time.monotonic() - began < 5, 'a watch made after the close returns at once'

    asyncio.run(waits())


def test_a_waiter_that_starts_the_doorbell_when_the_pool_is_empty_blocks_no_connection_from_coming_back(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first waiter took the doorbell's connection while it held the waiters' lock, and a connection that came back
    to the pool needed that lock: with every connection out, both waited the pool's 30 s timeout. A server's
    long-polls then held its threads, and a heartbeat waited as long."""
    pool = db.engine.pool
    assert isinstance(pool, QueuePool)
    held: list[PoolProxiedConnection] = []

    def take() -> None:  # one connection for each thread: a thread never holds two
        held.append(db.engine.raw_connection())

    takers = [threading.Thread(target=take) for _ in range(pool.size() + pool._max_overflow)]
    for t in takers:
        t.start()
    for t in takers:
        t.join(5)
    assert len(held) == pool.size() + pool._max_overflow  # every connection is out
    asked, connect = threading.Event(), db.engine.raw_connection

    def doorbell_connects() -> PoolProxiedConnection:
        asked.set()
        return connect()

    monkeypatch.setattr(db.engine, 'raw_connection', doorbell_connects)
    waiter = threading.Thread(target=db.wait_until, args=(lambda _s: True,), kwargs={'on': []}, daemon=True)
    waiter.start()
    assert asked.wait(5), 'the waiter starts the doorbell, which waits for a connection'
    back = threading.Thread(target=held.pop().close, daemon=True)
    back.start()
    back.join(5)
    assert not back.is_alive(), 'a connection came back to the pool while the doorbell waits for one'
    for conn in held:
        conn.close()
    waiter.join(10)
    assert not waiter.is_alive()


def test_a_doorbell_that_got_no_connection_wakes_its_waiter_which_starts_it_again(
    db: Database, campaign: Campaign, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The doorbell takes its connection in its own thread: one that gets none must not leave a waiter deaf."""
    connect, refused = db.engine.raw_connection, threading.Event()

    def refused_once() -> PoolProxiedConnection:
        if threading.current_thread().name == 'doorbell' and not refused.is_set():
            refused.set()
            raise DatabaseClosed('the database is closed')
        return connect()

    monkeypatch.setattr(db.engine, 'raw_connection', refused_once)
    other = Database(str(db.engine.url), actor=System.HUMAN)
    seen: list[bool] = []
    waiting = threading.Thread(target=lambda: seen.append(db.wait_until(paused(campaign), on=PAUSED)))
    waiting.start()
    assert refused.wait(5)
    wait_until(lambda: any(t.name == 'doorbell' and t.is_alive() for t in threading.enumerate()))
    pause(other, campaign)
    other.close()
    waiting.join(10)
    assert seen == [True], 'the doorbell that the waiter started again rang'


def new_events(db: Database) -> list[NewEvent]:
    """Every event, as the doorbell reads it."""
    conn = db.engine.raw_connection()
    try:
        return session_module._events_after(conn, 0)
    finally:
        conn.close()


@pytest.mark.parametrize(
    'topic',
    [
        Topic(),
        Topic(CampaignConfigChanged),
        Topic(Changed),
        Topic(CampaignConfigChanged, target=TargetRef(TargetKind.CAMPAIGN, 7)),
        Topic(EventModel, target=TargetRef(TargetKind.CAMPAIGN, 7)),
        Topic(LeasePrompted, to='w1'),
        Topic(WorkerDrained, to='w2'),
    ],
    ids=str,
)
def test_a_topic_admits_in_memory_the_events_that_it_matches_in_sql(
    db: Database, campaign: Campaign, topic: Topic
) -> None:
    """The doorbell wakes a waiter for what its read then finds: ``admits`` and ``matches`` must agree."""
    pause(db, campaign)
    config_changed(db, 7)
    config_changed(db, 8)
    limit_set(db, 7)
    on = TargetRef(TargetKind.CAMPAIGN, 7)
    with db.session() as s:
        for worker in ('w1', 'w2'):
            emit(s, LeasePrompted(lease=1, worker=worker, comment=1, text='hi'), on, System.DAEMON)
            emit(s, WorkerDrained(worker=worker), on, System.DAEMON)
        s.commit()
    events = new_events(db)
    with db.session() as s:
        matched = feed.after(s, 0, [topic], limit=1000).ids
    assert [e.id for e in events if topic.admits(e)] == matched
    assert matched, 'a topic that admits nothing proves nothing'
