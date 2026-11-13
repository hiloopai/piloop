"""The ``bucket`` leader's Litestream, the real one, on ``file://`` replicas: a term's new replica holds every write.

The tests need the pinned Litestream on ``PATH``: ``uv run -m scripts.litestream`` puts it in ``.venv/bin``, as
``make install`` and CI do. Without it they fail, and say so.
"""

import shutil
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import closing
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from piloop.plugins.leader.bucket import Litestream
from piloop.plugins.leader.contract import Unreachable
from scripts.litestream import VERSION
from tests.conftest import wait_until

FIRST = 'ltx/0/0000000000000001-0000000000000001.ltx'
"""A ``file://`` replica's first file, which holds the whole database: ``bucket.FIRST`` on S3."""


@pytest.fixture(scope='module')
def executable() -> str:
    found = shutil.which('litestream')
    assert found is not None, (
        f'Litestream {VERSION} is not on PATH: `uv run -m scripts.litestream` installs it into .venv/bin'
    )
    return found


@pytest.fixture
def litestream(executable: str) -> Litestream:
    return Litestream(executable, None)


def add(database: Path, body: str) -> None:
    with closing(sqlite3.connect(database)) as con:
        con.execute('INSERT INTO notes (body) VALUES (?)', (body,))
        con.commit()


def notes(database: Path) -> list[str]:
    with closing(sqlite3.connect(database)) as con:
        return [r[0] for r in con.execute('SELECT body FROM notes ORDER BY id')]


def restored(litestream: Litestream, url: str, out: Path) -> list[str]:
    """The notes that a restore of the replica at ``url`` holds now: none while it has no snapshot."""
    out.unlink(missing_ok=True)
    try:
        litestream.restore(url, None, out)
    except Unreachable:
        return []
    return notes(out)


def written(at: Path) -> Path:
    database = at / 'piloop.db'
    with closing(sqlite3.connect(database)) as con:
        con.execute('PRAGMA journal_mode=WAL')
        con.execute('CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT)')
        con.commit()
    return database


def test_a_replication_over_old_local_state_without_txid_1_ships_every_later_write_to_its_new_replica(
    litestream: Litestream, tmp_path: Path
) -> None:
    """An earlier term's Litestream, or the ``local`` chart's sidecar, left its state beside the database.

    Litestream's L0 retention dropped that state's first files. A replica of the state would get one snapshot, then
    fail every upload: the term's writes would never ship, and the provider would never see its first file.
    """
    database = written(tmp_path)
    state = tmp_path / '.piloop.db-litestream' / 'ltx' / '0'
    old = litestream.replicate(database, f'file://{tmp_path / "old"}')
    try:
        for i in range(1, 4):  # a sync each: Litestream makes an L0 file per sync, not per write
            wait_until(lambda n=i: len(list(state.glob('*.ltx'))) >= n, f'L0 file {i} of the old replication')
            add(database, f'old {i}')
    finally:
        old.stop()
    *dropped, kept = sorted(state.glob('*.ltx'))
    for each in dropped:  # what L0 retention leaves after a while
        each.unlink()
    assert not kept.name.startswith('0000000000000001-')

    new = tmp_path / 'new'
    replicating = litestream.replicate(database, f'file://{new}')
    try:
        wait_until(lambda: (new / FIRST).is_file(), "the new replica's first file")
        add(database, 'after the start')
        want = notes(database)
        assert len(want) == 4
        wait_until(
            lambda: restored(litestream, f'file://{new}', tmp_path / 'r.db') == want, 'the write after the start'
        )
    finally:
        replicating.stop()
    assert restored(litestream, f'file://{new}', tmp_path / 'r.db') == want, 'every row, from the new replica alone'


class Refusing(BaseHTTPRequestHandler):
    """S3 for a role without ``s3:GetBucketLocation``, as hosted staging's: it refuses every request, and records it."""

    seen: list[str]

    def _refuse(self) -> None:
        self.seen.append(self.path)
        body = b'<Error><Code>AccessDenied</Code><Message>Access Denied</Message></Error>'
        self.send_response(HTTPStatus.FORBIDDEN)
        self.send_header('Content-Type', 'application/xml')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_PUT = do_HEAD = do_POST = do_DELETE = _refuse

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def s3(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """The paths that Litestream asks a refusing S3 for. Litestream reaches it through ``AWS_ENDPOINT_URL_S3``, so it
    still looks up a bucket's region when its URL names none, as it does against AWS."""
    seen: list[str] = []
    handler = type('Handler', (Refusing,), {'seen': seen})
    server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    monkeypatch.setenv('AWS_ENDPOINT_URL_S3', f'http://127.0.0.1:{server.server_address[1]}')
    for name in ('AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY'):
        monkeypatch.setenv(name, 'testing')
    try:
        yield seen
    finally:
        server.shutdown()
        server.server_close()


def test_litestream_named_no_region_asks_s3_for_it_and_exits_at_once_when_refused(
    litestream: Litestream, s3: list[str], tmp_path: Path
) -> None:
    """What the ``bucket`` leader did before it named the region: the term's Litestream died at its start."""
    replicating = litestream.replicate(written(tmp_path), 's3://piloop-b/terms/1')
    try:
        wait_until(lambda: replicating.exited() is not None, 'Litestream to exit')
    finally:
        replicating.stop()
    why = replicating.exited()
    assert why is not None and why.startswith('Litestream exited with status 1: ') and 'GetBucketLocation' in why
    assert any('location' in path for path in s3)


def test_litestream_given_the_region_asks_s3_for_no_location(executable: str, s3: list[str], tmp_path: Path) -> None:
    located = Litestream(executable, 'us-east-1')
    replicating = located.replicate(written(tmp_path), 's3://piloop-b/terms/1')
    try:
        wait_until(lambda: len(s3) >= 2, 'Litestream to ask S3 for its replica')
        assert replicating.exited() is None, 'refused, it tries again: it runs'
    finally:
        replicating.stop()
    assert not any('location' in path for path in s3), s3
    with pytest.raises(Unreachable, match=r'litestream restore s3://piloop-b/terms/1\?region=us-east-1'):
        located.restore('s3://piloop-b/terms/1', None, tmp_path / 'r.db')
    assert not any('location' in path for path in s3), 'nor does its restore'
