"""Two daemons of one home on two hosts, under the ``bucket`` leader: the second waits as a standby, and takes the
next term once the first stops."""

import os
import signal
import socket
import threading
from collections.abc import Iterator
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path

import boto3
import httpx2 as httpx
import pytest
from moto import mock_aws
from sqlalchemy import select, text

from piloop.actor import System
from piloop.api.client import Client
from piloop.api.contract import Access, Selection, TokenBody
from piloop.daemon import holding, main
from piloop.daemon.holding import Holding
from piloop.daemon.main import Daemon
from piloop.daemon.rebuild import RebuildFailed
from piloop.db import terms
from piloop.db.events import emit
from piloop.db.ids import EXACT, SHIFT
from piloop.db.models import Event, Term as TermRow
from piloop.ids import HOME
from piloop.otel import Tracer
from piloop.plugins.executor.kit.client import WorkerInfo
from piloop.plugins.executor.kit.leases import LeaseWorkerJoined, NoLease
from piloop.plugins.executor.kit.remote import Remote
from piloop.plugins.leader import bucket as bucket_module
from piloop.plugins.leader.bucket import BucketTerms, Record, Source
from piloop.plugins.leader.contract import LostTerm, Term, Unreachable
from piloop.plugins.network.contract import Address
from piloop.settings import Settings
from tests.conftest import Dial, wait_until
from tests.fixtures.netfault import Proxy, Reset
from tests.leader.replica import FakeReplica
from tests.test_lease import HOST, PROBE
from tests.test_lease_client import secret

BUCKET = 'piloop-standby'
CONFIG = f"""[plugins.leader]
provider = "bucket"
url = "s3://{BUCKET}/cell"
ttl = 10
renew = 0.05
deadline = 5
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
        ended: list[str] = []
        monkeypatch.setattr(holding, '_exit', ended.append)  # a term's end would end the test's worker process
        yield replica
    assert ended == [], 'no term ends inside a test: its daemon would exit at once'


def home(tmp_path: Path, name: str, config: str = CONFIG) -> Settings:
    path = tmp_path / name
    path.mkdir()
    (path / 'config.toml').write_text(config)
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


def stops_and_releases(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """What a daemon's renewals and its provider do, in order: ``stop`` (the renewals) and ``release`` (the term)."""
    done: list[str] = []
    stop, release = Holding.stop, BucketTerms.release

    def stopping(self: Holding) -> None:
        done.append('stop')
        stop(self)

    def releasing(self: BucketTerms, term: Term) -> None:
        done.append('release')
        release(self, term)

    monkeypatch.setattr(Holding, 'stop', stopping)
    monkeypatch.setattr(BucketTerms, 'release', releasing)
    return done


def test_a_daemon_with_a_campaign_whose_checkout_is_its_own_does_not_start(
    tmp_path: Path, bucket: FakeReplica, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another host could not make its repo again: the daemon refuses, and gives the term back."""
    first = home(tmp_path, 'a')
    daemon = Daemon(first)
    with daemon.db.session() as s:  # a campaign that a daemon of an older piloop registered: in its replica too
        s.execute(text("INSERT INTO campaigns (name, path, config) VALUES ('mine', '/srv/mine', '{}')"))
        s.commit()
    daemon.shutdown(timeout=5)
    done = stops_and_releases(monkeypatch)
    with pytest.raises(SystemExit, match='^the daemon does not start: /srv/mine is a checkout of your own'):
        Daemon(first)
    assert done[:2] == ['stop', 'release'], 'no renewal after the release: it would end the process at once'
    until = stored().until
    assert until is not None and until <= datetime.now(UTC), 'released: the next daemon takes it at once'


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
        done = stops_and_releases(m)
        with pytest.raises(SystemExit, match='^the daemon does not start: campaign c: the origin is gone$'):
            Daemon(home(tmp_path, 'b'))
        assert done[:2] == ['stop', 'release'], 'no renewal after the release'

    start = Holding.start

    def unrenewed(self: Holding) -> None:
        start(self)
        self._lost('term 3 was not renewed')

    with monkeypatch.context() as m:
        m.setattr(Holding, 'start', unrenewed)
        with pytest.raises(SystemExit, match='^the daemon does not start: term 3 was not renewed$'):
            Daemon(home(tmp_path, 'c'))


FORWARDING = f"""[plugins.leader]
provider = "bucket"
url = "s3://{BUCKET}/cell"
ttl = 3
renew = 0.05
deadline = 2
max_skew = 0
replica_poll = 0.01

[plugins.cas]
provider = "s3"
url = "s3://{BUCKET}/cas"

[plugins.executor.pools.remote]
agents = 0
slots = {{ evaluation = 1 }}
"""


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


def readiness(url: str) -> dict[str, object] | None:
    try:
        return httpx.get(f'{url}/v1/ready', timeout=WAIT).json()
    except httpx.TransportError:
        return None


def test_a_standby_forwards_a_worker_and_an_agent_to_the_holder_then_serves_once_the_holder_dies(
    tmp_path: Path, bucket: FakeReplica, dial: Dial
) -> None:
    """Two daemons of one home on one machine, each on its own port. A's machine dies behind a proxy that resets every
    connection: what B forwarded there answers 503, and B takes the term, restores A's home and serves it."""
    a_port, b_port = free_port(), free_port()
    with Proxy(('127.0.0.1', a_port)) as proxy:
        cfg_a = home(
            tmp_path,
            'a',
            FORWARDING + f'[daemon]\nforward_refresh = 0.05\nadvertise_url = "http://127.0.0.1:{proxy.port}"\n',
        )
        cfg_b = home(tmp_path, 'b', FORWARDING + '[daemon]\nforward_refresh = 0.05\n')
        a = Daemon(cfg_a, listen=Address(host='127.0.0.1', port=a_port))
        b: list[Daemon] = []
        stack = ExitStack()
        try:
            with a.tracer.span('test') as span:
                a._listen(span)
            starting = threading.Thread(
                target=lambda: b.append(Daemon(cfg_b, listen=Address(host='127.0.0.1', port=b_port))), daemon=True
            )
            starting.start()
            standby = f'http://127.0.0.1:{b_port}'
            wait_until(
                lambda: readiness(standby) == {'serves': True, 'role': 'standby', 'epoch': 1}, 'B forwards to term 1'
            )
            with httpx.Client(transport=httpx.HTTPTransport(uds=str(cfg_b.socket_file)), base_url='http://p') as local:
                refused = local.get('/v1/status')
            assert (refused.status_code, refused.json()['error']) == (421, 'standby'), "B's socket names the holder"

            assert a._cas.put(b'piloop worker cas probe\n') == PROBE
            http = stack.enter_context(dial(standby, secret(a.db, 'w1')))
            worker = Remote(http, threading.Event(), Tracer(None))
            info = WorkerInfo(version='0.2.0', pool='remote', runners=['process'], sample=HOST, cas_probe=PROBE)
            worker.register(info)
            assert isinstance(worker.acquire('r1', HOST), NoLease), 'a worker of the standby: the holder answers'
            admin = secret(a.db, 'agent', Access.ADMIN)
            with Client.connect(dial(standby, admin)) as agent:
                assert agent.status(Selection()).daemon.epoch == 1
                agent.create_token(TokenBody(name='made-through-b', access=Access.READ))
            with a.db.session() as s:
                assert s.scalar(text("SELECT count(*) FROM tokens WHERE name = 'made-through-b'")) == 1

            wait_until(lambda: stored().source == Source(epoch=1), "term 1's replica holds the whole home")
            bucket.copy(f's3://{BUCKET}/cell/terms/1', cfg_a.database)  # what Litestream shipped by the death
            after = httpx.get(f'{standby}/v1/events', headers={'authorization': f'Bearer {admin}'}).json()['next']
            polled: list[httpx.Response] = []
            polling = threading.Thread(
                target=lambda: polled.append(
                    httpx.get(
                        f'{standby}/v1/events',
                        params={'after': after, 'wait': 20},
                        headers={'authorization': f'Bearer {admin}'},
                        timeout=WAIT * 3,
                    )
                )
            )
            polling.start()
            wait_until(lambda: any(b'/v1/events?after=' in line for line in proxy.requests), 'A holds the long-poll')
            assert a._holding is not None
            a._holding.stop()  # A's machine dies: no renewal, no release, and every connection to it resets
            proxy.toxic = Reset()
            polling.join(WAIT * 3)
            [cut] = polled
            assert (cut.status_code, cut.json()['error']) == (503, 'unavailable'), 'the long-poll in flight: 503'
            assert cut.headers['retry-after'] == '1'

            starting.join(WAIT * 3)
            assert len(b) == 1, 'B took term 2 once term 1 ended, and restored the home'
            with b[0].tracer.span('test') as span:
                b[0]._listen(span)
            assert readiness(standby) == {'serves': True, 'role': 'holder', 'epoch': 2}
            assert isinstance(worker.acquire('r2', HOST), NoLease), 'the worker goes on: its token came back'
            with Client.connect(dial(standby, admin)) as agent:
                assert agent.status(Selection()).daemon.epoch == 2, "the agent's token came back with the home"
                assert 'made-through-b' in {t.name for t in agent.list_tokens().tokens}
        finally:
            stack.close()
            a.shutdown(timeout=5)  # its release finds term 2: it says so
            for each in b:
                each.shutdown(timeout=5)
