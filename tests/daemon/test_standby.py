"""Two daemons of one home on two hosts, under the ``bucket`` leader: the second waits as a standby, and takes the
next term once the first stops."""

import os
import signal
import threading
from collections.abc import Iterator
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from piloop.daemon.main import Daemon
from piloop.db import terms
from piloop.plugins.leader.contract import Term, Unreachable
from piloop.settings import Settings

BUCKET = 'piloop-standby'
CONFIG = f"""[plugins.leader]
provider = "bucket"
url = "s3://{BUCKET}/cell"
ttl = 2
renew = 0.05
deadline = 1
max_skew = 0
"""
WAIT = 30


@pytest.fixture
def bucket(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in ('AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY'):
        monkeypatch.setenv(name, 'testing')
    monkeypatch.setenv('AWS_DEFAULT_REGION', 'us-east-1')
    with mock_aws():
        boto3.client('s3').create_bucket(Bucket=BUCKET)
        yield


def home(tmp_path: Path, name: str) -> Settings:
    path = tmp_path / name
    path.mkdir()
    (path / 'config.toml').write_text(CONFIG)
    return Settings(home=path)


def epochs(daemon: Daemon) -> int:
    with daemon.db.session() as s:
        return terms.newest(s)


def test_a_second_daemon_waits_as_a_standby_and_takes_the_next_term_when_the_first_stops(
    tmp_path: Path, bucket: None
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
    finally:
        second.shutdown(timeout=5)


def test_a_standby_stops_on_sigterm_and_a_holder_that_cannot_release_says_so(
    tmp_path: Path, bucket: None, monkeypatch: pytest.MonkeyPatch
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
