"""The ``bucket`` leader's replica: each term restores from the source that the record names, pins it, and names its
own replica once that holds the whole database. On moto's S3, with Litestream faked (``tests.leader.replica``)."""

import json
import sqlite3
import stat
import threading
from collections.abc import Iterator
from contextlib import closing
from datetime import timedelta
from pathlib import Path

import boto3
import pytest
from moto import mock_aws
from mypy_boto3_s3 import S3Client

from piloop.db.session import Database
from piloop.plugins.leader.bucket import BucketLeader, BucketTerms, Litestream, Record, Source
from piloop.plugins.leader.contract import LeaderHost, LostTerm, Term, Unreachable
from tests.leader.replica import FakeReplica
from tests.leader.test_bucket import BUCKET, SKEW, TTL, URL, Clock

KEY = 'homes/one/term.json'


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[S3Client]:
    for name in ('AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY'):
        monkeypatch.setenv(name, 'testing')
    monkeypatch.setenv('AWS_DEFAULT_REGION', 'us-east-1')
    with mock_aws():
        s3 = boto3.client('s3')
        s3.create_bucket(Bucket=BUCKET)
        yield s3


def home_of(db: Database, at: Path) -> LeaderHost:
    """A home at ``at`` whose database is a copy of ``db``'s, migrated and empty."""
    at.mkdir(parents=True, exist_ok=True)
    with (
        closing(sqlite3.connect(db.engine.url.database or '')) as src,
        closing(sqlite3.connect(at / 'piloop.db')) as dst,
    ):
        src.backup(dst)
    return LeaderHost(at, at / 'piloop.db')


def record(client: S3Client) -> Record:
    return Record.model_validate_json(client.get_object(Bucket=BUCKET, Key=KEY)['Body'].read())


def notes(path: Path) -> list[str]:
    with closing(sqlite3.connect(path)) as con:
        return [r[0] for r in con.execute('SELECT body FROM notes ORDER BY rowid')]


def note(path: Path, body: str) -> None:
    with closing(sqlite3.connect(path)) as con:
        con.execute('CREATE TABLE IF NOT EXISTS notes (body)')
        con.execute('INSERT INTO notes VALUES (?)', (body,))
        con.commit()


def test_a_term_restores_what_the_term_before_replicated_and_pins_it_until_its_own_replica_is_whole(
    db: Database, client: S3Client, tmp_path: Path
) -> None:
    clock, replica = Clock(), FakeReplica(client)
    a = BucketTerms(BucketLeader(url=URL), home_of(db, tmp_path / 'a'), client, clock, replica)
    first = a.attempt('a/1', None)
    assert first is not None and a.restore(first) is False, 'the first term of the bucket keeps its home as it is'
    note(tmp_path / 'a' / 'piloop.db', 'written by term 1')
    with a.replicate(first):
        assert a.named(first) and record(client).source == Source(epoch=1), 'its replica holds the whole database'
        note(tmp_path / 'a' / 'piloop.db', 'its last write')
    a.release(first)  # after the replica shipped the rest (the fake's stop)
    clock.now += SKEW
    b = BucketTerms(BucketLeader(url=URL), home_of(db, tmp_path / 'b'), client, clock, replica)
    second = b.attempt('b/1', None)
    assert second is not None and second.epoch == 2
    assert b.restore(second) is True
    assert notes(tmp_path / 'b' / 'piloop.db') == ['written by term 1', 'its last write'], 'what term 1 left'
    assert record(client).source == Source(epoch=1, txid=f'{2:016x}'), 'pinned: where it restored from, how far'
    assert replica.restores == [(f'{URL}/terms/1', None)]
    assert not b.named(second), 'its own replica has nothing yet: the pin stays'
    with b.replicate(second):
        assert b.named(second) and record(client).source == Source(epoch=2)


def test_a_term_that_dies_before_its_own_replica_leaves_the_pin_and_the_next_restores_exactly_that(
    db: Database, client: S3Client, tmp_path: Path
) -> None:
    clock, replica = Clock(), FakeReplica(client)
    a = BucketTerms(BucketLeader(url=URL), home_of(db, tmp_path / 'a'), client, clock, replica)
    first = a.attempt('a/1', None)
    assert first is not None
    note(tmp_path / 'a' / 'piloop.db', 'one')
    with a.replicate(first):
        a.named(first)
    clock.now += TTL + SKEW
    b = BucketTerms(BucketLeader(url=URL), home_of(db, tmp_path / 'b'), client, clock, replica)
    second = b.attempt('b/1', None)
    assert second is not None and b.restore(second)
    note(tmp_path / 'a' / 'piloop.db', 'a stale write of term 1')  # it was paused, and ships it after term 2 restored
    replica.copy(f'{URL}/terms/1', tmp_path / 'a' / 'piloop.db')
    clock.now += TTL + SKEW  # term 2 dies before its own replica
    c = BucketTerms(BucketLeader(url=URL), home_of(db, tmp_path / 'c'), client, clock, replica)
    third = c.attempt('c/1', None)
    assert third is not None and c.restore(third)
    assert notes(tmp_path / 'c' / 'piloop.db') == ['one'], 'the pinned position, without the stale write'
    assert replica.restores[-1] == (f'{URL}/terms/1', f'{2:016x}'), 'as far as term 2 restored'


def test_a_term_that_another_took_over_does_not_restore_or_pin_and_exits(
    db: Database, client: S3Client, tmp_path: Path
) -> None:
    clock, replica = Clock(), FakeReplica(client)
    a = BucketTerms(BucketLeader(url=URL), home_of(db, tmp_path / 'a'), client, clock, replica)
    first = a.attempt('a/1', None)
    assert first is not None
    clock.now += TTL + SKEW  # term 1 paused before its restore
    b = BucketTerms(BucketLeader(url=URL), home_of(db, tmp_path / 'b'), client, clock, replica)
    assert b.attempt('b/1', None) is not None
    with pytest.raises(LostTerm, match='term 1 is no longer the newest'):
        a.restore(first)
    assert replica.restores == []


def test_a_pin_that_another_take_beat_fails_and_the_term_never_serves(
    db: Database, client: S3Client, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock, replica = Clock(), FakeReplica(client)
    a = BucketTerms(BucketLeader(url=URL), home_of(db, tmp_path / 'a'), client, clock, replica)
    first = a.attempt('a/1', None)
    assert first is not None
    with a.replicate(first):
        a.named(first)
    a.release(first)
    clock.now += SKEW
    b = BucketTerms(BucketLeader(url=URL), home_of(db, tmp_path / 'b'), client, clock, replica)
    second = b.attempt('b/1', None)
    assert second is not None
    c = BucketTerms(BucketLeader(url=URL), home_of(db, tmp_path / 'c'), client, clock, replica)
    restore = replica.restore

    def slow(url: str, txid: str | None, out: Path) -> str:  # term 2 pauses in its restore past its term
        clock.now += TTL + SKEW
        assert c.attempt('c/1', None) is not None
        return restore(url, txid, out)

    monkeypatch.setattr(replica, 'restore', slow)
    with pytest.raises(LostTerm, match='another daemon wrote'):
        b.restore(second)
    assert record(client).epoch == 3 and record(client).source == Source(epoch=1), 'term 3 takes it as term 1 left it'


def test_the_watch_names_the_replica_soon_after_it_is_whole_and_the_stop_ships_the_rest(
    db: Database, client: S3Client, tmp_path: Path
) -> None:
    replica = FakeReplica(client)
    config = BucketLeader(url=URL, replica_poll=timedelta(milliseconds=10))
    a = BucketTerms(config, home_of(db, tmp_path / 'a'), client, Clock(), replica)
    first = a.attempt('a/1', None)
    assert first is not None
    with a.replicate(first):
        for _ in range(500):
            if record(client).source is not None:
                break
            threading.Event().wait(0.01)
        assert record(client).source == Source(epoch=1)
    assert len(replica.segments[f'{URL}/terms/1']) == 2, 'its start and its stop each ship the whole database'


def test_litestream_runs_as_a_child_and_its_errors_are_unreachable(tmp_path: Path) -> None:
    fake = tmp_path / 'litestream'
    fake.write_text(
        '#!/bin/sh\n'
        'echo "$@" >> "$(dirname "$0")/argv"\n'
        'case "$1" in\n'
        '  restore) case "$*" in *s3://gone*) echo "no backups found" >&2; exit 1 ;; esac\n'
        '           echo \'{"txid": "0000000000000007", "replica": "s3"}\' ;;\n'
        '  replicate) trap "exit 0" TERM; while :; do sleep 0.05; done ;;\n'
        'esac\n'
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    litestream = Litestream(str(fake), 'us-east-1')
    assert litestream.restore('s3://b/terms/1', None, tmp_path / 'out.db') == '0000000000000007'
    assert litestream.restore('s3://b/terms/1', '0000000000000003', tmp_path / 'out.db') == '0000000000000007'
    with pytest.raises(Unreachable, match='no backups found'):
        litestream.restore('s3://gone', None, tmp_path / 'out.db')
    replicating = litestream.replicate(tmp_path / 'piloop.db', 's3://b/terms/2')
    for _ in range(1000):  # it runs: its argv is written
        if len((tmp_path / 'argv').read_text().splitlines()) == 4:
            break
        threading.Event().wait(0.01)
    replicating.stop()
    assert (tmp_path / 'argv').read_text().splitlines() == [
        f'restore -json -o {tmp_path / "out.db"} s3://b/terms/1',
        f'restore -json -o {tmp_path / "out.db"} -txid 0000000000000003 s3://b/terms/1',
        f'restore -json -o {tmp_path / "out.db"} s3://gone',
        f'replicate {tmp_path / "piloop.db"} s3://b/terms/2',
    ]


def test_its_check_finds_litestream_on_path_or_says_to_install_it(tmp_path: Path) -> None:
    missing = BucketLeader(url=URL, litestream=str(tmp_path / 'nope')).checks()
    assert [(c.fails, c.found) for c in missing] == [(True, f'{tmp_path / "nope"} is not on PATH')]
    assert not BucketLeader(url=URL, litestream='sh').checks()[0].fails


def test_a_record_of_l5a_without_a_source_reads(client: S3Client) -> None:
    client.put_object(Bucket=BUCKET, Key=KEY, Body=json.dumps({'epoch': 3, 'holder': 'x/1'}).encode())
    assert record(client) == Record(epoch=3, holder='x/1') and isinstance(record(client), Term)
