"""Two daemons of one home on two hosts, under the ``bucket`` leader: the second waits as a standby, and takes the
next term once the first stops."""

import os
import signal
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import boto3
import pytest
from moto import mock_aws
from sqlalchemy import select, text

from piloop.actor import System
from piloop.daemon import main
from piloop.daemon.holding import Holding
from piloop.daemon.main import Daemon
from piloop.daemon.rebuild import RebuildFailed
from piloop.db import terms
from piloop.db.events import emit
from piloop.db.ids import EXACT, SHIFT
from piloop.db.models import Event, Term as TermRow
from piloop.ids import HOME
from piloop.plugins.executor.kit.leases import LeaseWorkerJoined
from piloop.plugins.leader import bucket as bucket_module
from piloop.plugins.leader.bucket import BucketTerms, Record, Source
from piloop.plugins.leader.contract import LostTerm, Term, Unreachable
from piloop.settings import Settings
from tests.leader.replica import FakeReplica

BUCKET = 'piloop-standby'
CONFIG = f"""[plugins.leader]
provider = "bucket"
url = "s3://{BUCKET}/cell"
ttl = 2
renew = 0.05
deadline = 1
max_skew = 0
replica_poll = 0.01

[plugins.cas]
provider = "s3"
url = "s3://{BUCKET}/cas"
"""
WAIT = 30


@pytest.fixture
def bucket(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeReplica]:
    """The bucket on moto's S3, and Litestream faked: one replica of every term, which each daemon's provider uses."""
    for name in ('AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY'):
        monkeypatch.setenv(name, 'testing')
    monkeypatch.setenv('AWS_DEFAULT_REGION', 'us-east-1')
    with mock_aws():
        client = boto3.client('s3')
        client.create_bucket(Bucket=BUCKET)
        replica = FakeReplica(client)
        monkeypatch.setattr(bucket_module, 'Litestream', lambda _executable, _region: replica)
        yield replica


def home(tmp_path: Path, name: str) -> Settings:
    path = tmp_path / name
    path.mkdir()
    (path / 'config.toml').write_text(CONFIG)
    return Settings(home=path)


def epochs(daemon: Daemon) -> int:
    with daemon.db.session() as s:
        return terms.newest(s)


def test_a_second_daemon_waits_as_a_standby_and_takes_the_next_term_when_the_first_stops(
    tmp_path: Path, bucket: FakeReplica
) -> None:
    first = Daemon(home(tmp_path, 'a'))
    assert epochs(first) == 1
    standby: list[Daemon] = []
    waiting = threading.Thread(target=lambda: standby.append(Daemon(home(tmp_path, 'b'))), daemon=True)
    waiting.start()
    waiting.join(0.5)
    assert waiting.is_alive(), 'it waits while the first renews its term'
    first.shutdown(timeout=5)
    waiting.join(WAIT)
    assert not waiting.is_alive() and len(standby) == 1, 'the release lets it take the term at once'
    second = standby[0]
    try:
        assert epochs(second) == 2, 'its own home counts on from the epoch in the bucket'
        with second.db.session() as s:
            lifted = s.scalar(text("SELECT min(seq) FROM sqlite_sequence WHERE name IN ('leases', 'events', 'runs')"))
        assert lifted is not None and lifted >= 2 << SHIFT, 'its first transaction lifted the ids past those of term 1'
    finally:
        second.shutdown(timeout=5)


def test_a_standby_stops_on_sigterm_and_a_holder_that_cannot_release_says_so(
    tmp_path: Path, bucket: FakeReplica, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = Daemon(home(tmp_path, 'a'))
    try:
        before = signal.getsignal(signal.SIGTERM)

        def stop_once_it_waits() -> None:  # a SIGTERM before its handler would end the test run
            for _ in range(WAIT * 100):
                if signal.getsignal(signal.SIGTERM) is not before:
                    os.kill(os.getpid(), signal.SIGTERM)
                    return
                threading.Event().wait(0.01)

        threading.Thread(target=stop_once_it_waits, daemon=True).start()
        with pytest.raises(SystemExit, match='stopped while it waited'):
            Daemon(home(tmp_path, 'b'))
        assert signal.getsignal(signal.SIGTERM) is before, 'its handler is gone again'
        first._lost_term('as the test says')  # what its renewals call past their deadline
        assert first.scheduler._stopped(), 'it stops, as on SIGTERM'
    finally:
        events: list[tuple[str, dict[str, object]]] = []
        monkeypatch.setattr(first.tracer, 'event', lambda name, **fields: events.append((name, fields)))

        def unreachable(term: Term) -> None:
            raise Unreachable('as the test says')

        monkeypatch.setattr(first._leader, 'release', unreachable)
        first.shutdown(timeout=5)
    assert ('term.not_released', {'error': 'as the test says'}) in events


def test_a_daemon_whose_term_would_hand_out_ids_past_2_53_does_not_start(tmp_path: Path, bucket: FakeReplica) -> None:
    last = EXACT // (1 << SHIFT) - 1
    ended = Term(epoch=last, holder='old/1', until=datetime(2026, 1, 1, tzinfo=UTC))
    boto3.client('s3').put_object(Bucket=BUCKET, Key='cell/term.json', Body=ended.model_dump_json().encode())
    with pytest.raises(SystemExit, match=f'the daemon does not start: term {last + 1} would hand out ids past 2'):
        Daemon(home(tmp_path, 'a'))


def test_a_daemon_with_a_campaign_whose_checkout_is_its_own_does_not_start(tmp_path: Path, bucket: FakeReplica) -> None:
    """Another host could not make its repo again: the daemon refuses before it takes the term."""
    first = home(tmp_path, 'a')
    daemon = Daemon(first)
    with daemon.db.session() as s:  # a campaign that a daemon of an older piloop registered: in its replica too
        s.execute(text("INSERT INTO campaigns (name, path, config) VALUES ('mine', '/srv/mine', '{}')"))
        s.commit()
    daemon.shutdown(timeout=5)
    with pytest.raises(SystemExit, match='^the daemon does not start: /srv/mine is a checkout of your own'):
        Daemon(first)


def test_the_next_term_restores_what_the_first_wrote_and_names_its_own_replica_once_it_is_whole(
    tmp_path: Path, bucket: FakeReplica
) -> None:
    first = Daemon(home(tmp_path, 'a'))
    with first.db.session() as s:
        written = emit(s, LeaseWorkerJoined(worker='w1', pool='p'), HOME, System.DAEMON)
        s.commit()
        mark = written.id
    first.shutdown(timeout=5)  # its replica ships the rest, then it releases the term
    second = Daemon(home(tmp_path, 'b'))
    try:
        with second.db.session() as s:
            assert s.get(Event, mark) is not None, 'what term 1 wrote, from its replica'
            assert [t.epoch for t in s.scalars(select(TermRow).order_by(TermRow.epoch))] == [1, 2]
        for _ in range(WAIT * 100):
            if stored().source == Source(epoch=2):
                break
            threading.Event().wait(0.01)
        assert stored().source == Source(epoch=2), 'its own replica, once it holds the whole database'
        assert bucket.restores == [(f's3://{BUCKET}/cell/terms/1', None)]
    finally:
        second.shutdown(timeout=5)


def stored() -> Record:
    body = boto3.client('s3').get_object(Bucket=BUCKET, Key='cell/term.json')['Body'].read()
    return Record.model_validate_json(body)


def test_a_daemon_that_loses_its_term_restores_badly_or_cannot_renew_while_it_starts_does_not_serve(
    tmp_path: Path, bucket: FakeReplica, monkeypatch: pytest.MonkeyPatch
) -> None:
    def taken(self: BucketTerms, term: Term) -> bool:
        raise LostTerm('another daemon took over')

    with monkeypatch.context() as m:
        m.setattr(BucketTerms, 'restore', taken)
        with pytest.raises(SystemExit, match='^the daemon does not start: another daemon took over$'):
            Daemon(home(tmp_path, 'a'))

    def failed(*_: object) -> None:
        raise RebuildFailed('campaign c: the origin is gone')

    with monkeypatch.context() as m:
        m.setattr(BucketTerms, 'restore', lambda self, term: True)
        m.setattr(main, 'rebuild', failed)
        with pytest.raises(SystemExit, match='^the daemon does not start: campaign c: the origin is gone$'):
            Daemon(home(tmp_path, 'b'))

    start = Holding.start

    def unrenewed(self: Holding) -> None:
        start(self)
        self._lost('term 3 was not renewed')

    with monkeypatch.context() as m:
        m.setattr(Holding, 'start', unrenewed)
        with pytest.raises(SystemExit, match='^the daemon does not start: term 3 was not renewed$'):
            Daemon(home(tmp_path, 'c'))
