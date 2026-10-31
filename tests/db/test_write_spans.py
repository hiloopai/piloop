import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from datetime import timedelta
from pathlib import Path

import pytest
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sqlalchemy import create_engine, exc, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from piloop.daemon.main import ACTOR
from piloop.db.models import Campaign
from piloop.db.session import (
    POOL_CONNECTIONS,
    POOL_WAIT_SPAN,
    WRITE_SPAN,
    Database,
    DatabaseClosed,
    MeasuredConnection,
    Patient,
    WriteSpans,
    write_transaction,
)
from piloop.otel import Tracer
from piloop.settings import TracingSettings

RELEASE_SECONDS = 0.05
"""How long the lock is held: well over the default ``slow_write``."""


@pytest.fixture
def measured(db: Database, tracer: Tracer) -> Iterator[Database]:
    """A second ``Database`` on the file of ``db`` that measures its writes into ``tracer``."""
    assert tracer.measures is not None
    database = Database(
        str(db.engine.url), actor=ACTOR, writes=WriteSpans(tracer.measures, TracingSettings().slow_write)
    )
    yield database
    database.close()


@pytest.fixture
def holder(measured: Database) -> Iterator[sqlite3.Connection]:
    """Another connection that holds the write lock until it commits. ``measured`` connects first: its connect-time
    pragmas need the lock."""
    measured.engine.connect().close()
    con = sqlite3.connect(measured.engine.url.database or '', isolation_level=None, check_same_thread=False)
    con.execute('BEGIN IMMEDIATE')
    yield con
    con.close()


def writes(spans: InMemorySpanExporter) -> list[ReadableSpan]:
    return [s for s in spans.get_finished_spans() if s.name == WRITE_SPAN]


def ms(span: ReadableSpan, key: str) -> float:
    value = (span.attributes or {})[key]
    assert isinstance(value, float)
    return value


def explicit(s: Session, campaign: Campaign) -> None:
    write_transaction(s)
    s.get(Campaign, campaign.id)


def implicit(s: Session, campaign: Campaign) -> None:
    s.execute(update(Campaign).where(Campaign.id == campaign.id).values(name='renamed'))


def many(s: Session, campaign: Campaign) -> None:
    s.connection().exec_driver_sql(
        'UPDATE campaigns SET name = ? WHERE id = ?', [('a', campaign.id), ('b', campaign.id)]
    )


@pytest.mark.parametrize('write', [explicit, implicit, many])
def test_a_contended_write_records_its_wait_for_the_lock(
    measured: Database,
    campaign: Campaign,
    holder: sqlite3.Connection,
    spans: InMemorySpanExporter,
    write: Callable[[Session, Campaign], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = MeasuredConnection.run

    def release_once_asked(con: MeasuredConnection, sql: str, statement: Callable[[], object]) -> object:
        def asked() -> object:  # the wait started before this: the release comes later however late this runs
            threading.Timer(RELEASE_SECONDS, holder.commit).start()
            return statement()

        monkeypatch.setattr(MeasuredConnection, 'run', run)
        return run(con, sql, asked)

    with measured.session() as s:
        s.get(Campaign, campaign.id)  # a read asks for no lock
        monkeypatch.setattr(MeasuredConnection, 'run', release_once_asked)
        write(s, campaign)
        s.commit()

    [span] = writes(spans)
    assert dict(span.attributes or {}).keys() == {'wait_ms', 'hold_ms'}
    assert ms(span, 'wait_ms') >= RELEASE_SECONDS * 1000


def test_a_write_that_gives_up_on_the_lock_records_its_wait(
    measured: Database, holder: sqlite3.Connection, spans: InMemorySpanExporter
) -> None:
    with measured.session() as s:
        s.connection().exec_driver_sql('PRAGMA busy_timeout = 20')
        with pytest.raises(OperationalError, match='database is locked'):
            write_transaction(s)

    [span] = writes(spans)
    assert ms(span, 'wait_ms') >= 20


def test_a_long_write_records_its_hold_and_prints_nothing(
    measured: Database, spans: InMemorySpanExporter, capsys: pytest.CaptureFixture[str]
) -> None:
    with measured.session() as s:
        write_transaction(s)
        time.sleep(RELEASE_SECONDS)
        s.commit()

    [span] = writes(spans)
    assert ms(span, 'hold_ms') >= RELEASE_SECONDS * 1000
    assert WRITE_SPAN not in capsys.readouterr().err  # exported, never printed


@pytest.mark.parametrize(('slow', 'recorded'), [(timedelta(0), 1), (timedelta(days=1), 0)])
def test_only_a_slow_write_records_a_span_and_a_read_never_does(
    db: Database, tracer: Tracer, campaign: Campaign, spans: InMemorySpanExporter, slow: timedelta, recorded: int
) -> None:
    assert tracer.measures is not None
    measured = Database(str(db.engine.url), actor=ACTOR, writes=WriteSpans(tracer.measures, slow))
    with measured.session() as s:
        s.scalars(select(Campaign)).all()
    with measured.session() as s:
        implicit(s, campaign)
        s.commit()
    measured.close()

    assert len(writes(spans)) == recorded


def test_a_session_waits_out_a_stall_that_holds_every_connection_and_records_each_wait(
    db: Database, tracer: Tracer, spans: InMemorySpanExporter
) -> None:
    """A stalled disk holds every connection that a thread took. A session that needs one waits until one comes back,
    however long, instead of failing the work it does: each round of the pool's timeout is a span."""
    assert tracer.measures is not None
    patient = Database(
        str(db.engine.url),
        actor=ACTOR,
        writes=WriteSpans(tracer.measures, TracingSettings().slow_write),
        pool_round=timedelta(seconds=0.05),
    )
    stalled, holding = threading.Event(), threading.Semaphore(0)

    def hold() -> None:
        with patient.session() as s:
            s.execute(select(1))
            holding.release()
            stalled.wait()

    holders = [threading.Thread(target=hold) for _ in range(POOL_CONNECTIONS)]
    for t in holders:
        t.start()
    for _ in holders:
        assert holding.acquire(timeout=10)
    answered: list[int] = []

    def read() -> None:
        with patient.session() as s:
            answered.append(s.scalar(select(1)) or 0)

    reader = threading.Thread(target=read)
    reader.start()
    time.sleep(0.3)  # several rounds of the pool's timeout
    assert answered == [], 'it waits while every connection is held'
    stalled.set()
    reader.join(10)
    for t in holders:
        t.join(10)
    patient.close()
    assert answered == [1], 'it got a connection once the stall ended, and its work went on'
    waits = [s for s in spans.get_finished_spans() if s.name == POOL_WAIT_SPAN]
    assert len(waits) >= 2 and {(s.attributes or {})['checked_out'] for s in waits} == {POOL_CONNECTIONS}


def test_a_checkout_that_waits_gives_up_once_its_database_closes(db: Database) -> None:
    patient = Database(str(db.engine.url), actor=ACTOR, pool_round=timedelta(seconds=0.05))
    stalled, holding = threading.Event(), threading.Semaphore(0)

    def hold() -> None:
        with patient.session() as s:
            s.execute(select(1))
            holding.release()
            stalled.wait()

    holders = [threading.Thread(target=hold) for _ in range(POOL_CONNECTIONS)]
    for t in holders:
        t.start()
    for _ in holders:
        assert holding.acquire(timeout=10)
    met: list[BaseException] = []

    def read() -> None:
        try:
            with patient.session() as s:
                s.execute(select(1))
        except DatabaseClosed as e:
            met.append(e)

    reader = threading.Thread(target=read)
    reader.start()
    time.sleep(0.2)  # rounds that it waits, which this database records nowhere
    closing = threading.Thread(target=patient.close)
    closing.start()
    reader.join(10)
    assert [str(e) for e in met] == ['the database is closed'], 'the waiter gives up: the daemon stops'
    stalled.set()
    for t in (*holders, closing):
        t.join(10)


def test_a_patient_pool_of_no_database_waits_one_round_as_a_plain_pool(tmp_path: Path) -> None:
    engine = create_engine(
        f'sqlite:///{tmp_path / "plain.db"}', poolclass=Patient, pool_size=1, max_overflow=0, pool_timeout=0.01
    )
    held = engine.raw_connection()
    with pytest.raises(exc.TimeoutError):
        engine.raw_connection()
    held.close()
    engine.dispose()
