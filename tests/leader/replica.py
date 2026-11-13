"""A fake of Litestream for the ``bucket`` leader's tests: a replica per URL, in memory, and its first file on moto's S3.

A replica is a list of segments, as Litestream's LTX files are: the first one a copy of the whole database (a SQLite
backup), each later one either another whole copy or one event row. A transaction id is the number of segments that a
restore applies. The first segment also puts the key that the provider looks for (``FIRST``) in the bucket, so the
provider sees the replica hold the whole database.
"""

import shutil
import sqlite3
import tempfile
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from mypy_boto3_s3 import S3Client

from piloop.plugins.leader.bucket import FIRST
from piloop.plugins.leader.contract import Unreachable

type Row = tuple[object, ...]
"""An event's row: ``id, at, actor, target_kind, target_id, payload``."""


@dataclass
class Replicating:
    replica: FakeReplica
    database: Path
    url: str
    stopped: bool = False
    ended: str | None = None
    """Why the replication ended by itself: a test sets it."""

    def exited(self) -> str | None:
        return self.ended

    def stop(self) -> None:
        """Ship the whole database once more, as Litestream ships its last frames."""
        if self.replica.automatic:
            self.replica.copy(self.url, self.database)
        self.stopped = True


@dataclass
class FakeReplica:
    client: S3Client
    automatic: bool = True
    """Whether ``replicate`` copies the database at its start and at its stop. A replay ships by hand instead."""
    segments: dict[str, list[Path | Row]] = field(default_factory=dict)
    scratch: Path = field(default_factory=lambda: Path(tempfile.mkdtemp()))
    restores: list[tuple[str, str | None]] = field(default_factory=list)

    def restore(self, url: str, txid: str | None, out: Path) -> str:
        self.restores.append((url, txid))
        segments = self.segments.get(url, [])
        n = len(segments) if txid is None else int(txid, 16)
        if not segments or n < 1 or not isinstance(segments[0], Path):
            raise Unreachable(f'no replica at {url}')
        start = max(i for i in range(n) if isinstance(segments[i], Path))  # the newest whole copy within the n
        whole = segments[start]
        assert isinstance(whole, Path)
        shutil.copy(whole, out)
        with closing(sqlite3.connect(out, isolation_level=None)) as con:
            for row in segments[start + 1 : n]:
                assert not isinstance(row, Path)
                con.execute(
                    'INSERT INTO events (id, at, actor, target_kind, target_id, payload) VALUES (?, ?, ?, ?, ?, ?)', row
                )
        return f'{n:016x}'

    def replicate(self, database: Path, url: str) -> Replicating:
        if self.automatic:
            self.copy(url, database)
        return Replicating(self, database, url)

    def copy(self, url: str, database: Path) -> None:
        """A segment that holds the whole database: the first one is what the provider looks for."""
        target = self.scratch / f'{len(list(self.scratch.iterdir()))}.db'
        with closing(sqlite3.connect(database)) as src, closing(sqlite3.connect(target)) as dst:
            src.backup(dst)
        segments = self.segments.setdefault(url, [])
        segments.append(target)
        if len(segments) == 1:
            parsed = urlparse(url)
            key = f'{parsed.path.strip("/")}/{FIRST}{1:016x}.ltx'
            self.client.put_object(Bucket=parsed.netloc, Key=key, Body=b'')

    def ship(self, url: str, row: Row) -> None:
        self.segments.setdefault(url, []).append(row)
