"""The ``bucket`` leader's Litestream, the real one, on ``file://`` replicas: a term's new replica holds every write.

The tests need the pinned Litestream on ``PATH``: ``uv run -m scripts.litestream`` puts it in ``.venv/bin``, as
``make install`` and CI do. Without it they fail, and say so.
"""

import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from piloop.plugins.leader.bucket import Litestream
from piloop.plugins.leader.contract import Unreachable
from scripts.litestream import VERSION
from tests.conftest import wait_until

FIRST = 'ltx/0/0000000000000001-0000000000000001.ltx'
"""A ``file://`` replica's first file, which holds the whole database: ``bucket.FIRST`` on S3."""


@pytest.fixture(scope='module')
def litestream() -> Litestream:
    found = shutil.which('litestream')
    assert found is not None, (
        f'Litestream {VERSION} is not on PATH: `uv run -m scripts.litestream` installs it into .venv/bin'
    )
    return Litestream(found, None)


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
