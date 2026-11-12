"""The ``bucket`` leader's term record on moto's S3: conditional takes, renewals and releases, and the fence."""

import threading
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import boto3
import pytest
from botocore.exceptions import EndpointConnectionError
from moto import mock_aws
from mypy_boto3_s3 import S3Client
from pydantic import ValidationError

from piloop.daemon.main import preflight
from piloop.db import terms
from piloop.db.session import Database
from piloop.plugins.cas.cached import Cached
from piloop.plugins.cas.disk import Disk
from piloop.plugins.cas.s3 import S3
from piloop.plugins.host import PluginError
from piloop.plugins.leader.bucket import BucketLeader, BucketTerms
from piloop.plugins.leader.contract import LostTerm, Term, Unreachable
from piloop.plugins.leader.local import LocalLeader
from piloop.settings import Settings
from tests.conftest import leader_host

BUCKET = 'piloop-terms'
URL = f's3://{BUCKET}/homes/one'
TTL, SKEW = timedelta(seconds=30), timedelta(seconds=2)
"""The defaults of `BucketLeader`."""


@dataclass
class Clock:
    now: datetime = datetime(2026, 11, 11, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[S3Client]:
    for name in ('AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY'):
        monkeypatch.setenv(name, 'testing')
    monkeypatch.setenv('AWS_DEFAULT_REGION', 'us-east-1')
    with mock_aws():
        s3 = boto3.client('s3')
        s3.create_bucket(Bucket=BUCKET)
        yield s3


def host(db: Database, client: S3Client, clock: Clock, url: str = URL) -> BucketTerms:
    return BucketTerms(BucketLeader(url=url), leader_host(db), client, clock)


def stored(client: S3Client) -> Term:
    return Term.model_validate_json(client.get_object(Bucket=BUCKET, Key='homes/one/term.json')['Body'].read())


def test_one_of_two_hosts_takes_the_first_term_and_the_other_waits_until_it_ends_plus_the_skew(
    db: Database, client: S3Client
) -> None:
    clock = Clock()
    a, b = host(db, client, clock), host(db, client, clock)
    first = a.attempt('a/1', None)
    assert first == Term(epoch=1, holder='a/1', until=clock.now + TTL)
    assert stored(client) == first and a.current() == first
    assert b.attempt('b/1', None) is None, 'a held term'
    assert first.until is not None
    clock.now = first.until + SKEW - timedelta(microseconds=1)
    assert b.attempt('b/1', None) is None, 'within the skew past its end'
    assert a.current() is None, 'ended, by this clock'
    clock.now += timedelta(microseconds=1)
    second = b.attempt('b/1', 'https://b:7000')
    assert second == Term(epoch=2, holder='b/1', url='https://b:7000', until=clock.now + TTL)
    with pytest.raises(LostTerm):
        a.renew(first)


def test_a_renewal_moves_the_end_and_a_release_ends_the_term_but_keeps_its_epoch(
    db: Database, client: S3Client
) -> None:
    clock = Clock()
    a, b = host(db, client, clock), host(db, client, clock)
    first = a.attempt('a/1', None)
    assert first is not None
    clock.now += timedelta(seconds=2)
    renewed = a.renew(first)
    assert (renewed.epoch, renewed.until) == (1, clock.now + TTL)
    a.release(renewed)
    assert stored(client).until == clock.now, 'the object stays, so the next take counts on from its epoch'
    with pytest.raises(LostTerm):
        a.renew(renewed)
    assert b.attempt('b/1', None) is None, 'a released term is taken once the skew passed'
    clock.now += SKEW
    second = b.attempt('b/1', None)
    assert second is not None and second.epoch == 2


def test_a_write_that_another_beat_is_refused_and_takes_nothing(
    db: Database, client: S3Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both hosts read no record; the first create wins, the second is refused (412), however late it reads."""
    clock = Clock()
    a, b = host(db, client, clock), host(db, client, clock)
    monkeypatch.setattr(b, '_read', lambda: (None, None))  # what it read before a wrote
    assert a.attempt('a/1', None) is not None
    assert b.attempt('b/1', None) is None
    monkeypatch.undo()
    held = a.current()
    assert held is not None
    clock.now += TTL + SKEW
    stale = b._read()
    assert a.renew(held).epoch == 1, 'late, but before any other write: it holds the term again'

    monkeypatch.setattr(b, '_read', lambda: stale)
    assert b.attempt('b/1', None) is None, 'its If-Match names the ETag before the renewal'
    assert stored(client).holder == 'a/1'


def test_a_take_is_above_the_newest_term_of_its_home_too(db: Database, client: S3Client) -> None:
    """A home that served under another provider has terms already: the bucket's epochs go on above them."""
    with db.session() as s:
        terms.begin(s, 7, 1, None)
        s.commit()
    term = host(db, client, Clock()).attempt('a/1', None)
    assert term is not None and term.epoch == 8


def test_a_standby_takes_once_the_term_ends_or_returns_none_when_stopped(db: Database, client: S3Client) -> None:
    clock = Clock()
    a, b = host(db, client, clock), host(db, client, clock)
    b.config = b.config.model_copy(update={'renew': timedelta(milliseconds=10)})
    assert a.attempt('a/1', None) is not None
    stop = threading.Event()
    stop.set()
    assert b.take('b/1', None, stop) is None
    taken: list[Term | None] = []
    waiting = threading.Thread(target=lambda: taken.append(b.take('b/1', None, threading.Event())))
    waiting.start()
    waiting.join(0.1)
    assert waiting.is_alive(), 'a standby while a holds the term'
    clock.now += TTL + SKEW
    waiting.join(5)
    assert [t.epoch if t else None for t in taken] == [2]


def test_errors_other_than_a_refusal_are_unreachable_on_renewal(db: Database, client: S3Client) -> None:
    clock = Clock()
    a = host(db, client, clock)
    term = a.attempt('a/1', None)
    assert term is not None
    client.delete_object(Bucket=BUCKET, Key='homes/one/term.json')
    client.delete_bucket(Bucket=BUCKET)
    with pytest.raises(Unreachable):
        a.renew(term)


def test_s3_that_does_not_answer_or_fails_otherwise_is_unreachable(
    db: Database, client: S3Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = Clock()
    with pytest.raises(Unreachable, match='NoSuchBucket'):
        host(db, client, clock, url='s3://no-such-bucket/x').attempt('a/1', None)
    a = host(db, client, clock)
    term = a.attempt('a/1', None)
    assert term is not None

    def down(**_: object) -> None:
        raise EndpointConnectionError(endpoint_url='https://s3.example')

    monkeypatch.setattr(client, 'put_object', down)
    with pytest.raises(Unreachable, match='Could not connect'):
        a.renew(term)
    monkeypatch.setattr(client, 'get_object', down)
    with pytest.raises(Unreachable, match='Could not connect'):
        a.current()


def test_only_s3_urls_and_timings_in_order(db: Database, client: S3Client) -> None:
    for url in ('gs://bucket/x', 's3:///x'):
        with pytest.raises(ValidationError, match='s3://'):
            BucketLeader(url=url)
    with pytest.raises(ValidationError, match='renew'):
        BucketLeader(url=URL, renew=timedelta(seconds=20))
    assert BucketTerms(BucketLeader(url=f's3://{BUCKET}/'), leader_host(db), client).key == 'term.json', (
        'at the root of the bucket'
    )


def test_a_checkout_that_piloop_did_not_clone_is_refused_and_local_refuses_none(tmp_path: Path) -> None:
    """Another host makes a campaign's repo again from its clone: a checkout of one's own has none."""
    bucket = BucketLeader(url=URL)
    assert bucket.refuses(tmp_path, tmp_path / 'repos' / '3') is None
    assert (why := bucket.refuses(tmp_path, tmp_path / 'mine')) is not None and why.startswith(
        f'{tmp_path / "mine"} is'
    )
    assert LocalLeader().refuses(tmp_path, tmp_path / 'mine') is None


def test_a_cas_that_only_this_host_reaches_is_refused_and_the_daemon_does_not_start(tmp_path: Path) -> None:
    bucket = BucketLeader(url=URL)
    disk, s3 = Disk(), S3(url=f's3://{BUCKET}/cas')
    assert [bucket.refuses_cas(c) is None for c in (disk, s3, Cached(remote=disk), Cached(remote=s3))] == [
        False,
        True,
        False,
        True,
    ]
    assert LocalLeader().refuses_cas(disk) is None
    (tmp_path / 'config.toml').write_text(f'[plugins.leader]\nprovider = "bucket"\nurl = "{URL}"\n')
    with pytest.raises(PluginError, match='config.toml: the CAS is a directory of this host'):
        preflight(Settings(home=tmp_path))
