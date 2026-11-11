"""The paths of ``tests/models/replica.tla``, replayed against real databases: the migrated schema, the lift of the
``bucket`` leader's first transaction (``BucketTerms.begin``) and real writes, with the replica faked.

Each host's database is a real file. A commit is an event that the daemon writes (``emit``): its id is what a worker
holds. The replica is a fake of Litestream's, per prefix: its snapshot is a copy of the file (SQLite's backup), and each
shipped commit is that row, applied in order on a restore. A restore copies the snapshot of the model's source and
applies the shipped rows up to the pinned position. After each step, each live host's commits are the model's, and no
id was handed out twice.

The traces cover every kind of transition of ``replica_graph.cfg`` that the replay tells apart (``--by`` below);
``scripts/graph_traces.py`` wrote them from TLC's ``-dump dot,actionlabels``.
"""

import json
import shutil
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

import boto3
import pytest

from piloop.actor import System
from piloop.db import terms
from piloop.db.events import emit
from piloop.db.models import Event
from piloop.db.session import Database
from piloop.ids import HOME
from piloop.plugins.executor.kit.leases import LeaseWorkerJoined
from piloop.plugins.leader.bucket import BucketLeader, BucketTerms
from piloop.plugins.leader.contract import Term

TRACES = Path(__file__).parent / 'traces' / 'replica'
ALL = 99
"""The model's position past every segment: the whole prefix."""

type Value = int | str | bool | list[Value] | dict[str, Value]
type Commit = tuple[int, int]
"""A model commit: its writer's epoch, and its id in the model."""


def _value(itf: object) -> Value:
    match itf:
        case {'#bigint': str(n)}:
            return int(n)
        case {'#set': list(items)} | {'#tup': list(items)}:
            return [_value(v) for v in items]
        case dict():
            return {str(k): _value(v) for k, v in itf.items()}
        case list():
            return [_value(v) for v in itf]
        case bool() | str():
            return itf
    raise AssertionError(f'not an ITF value: {itf!r}')


def of(state: dict[str, Value], name: str, h: int) -> set[Commit]:
    """Host ``h``'s set of commits in the model's variable ``name``."""
    per_host = state[name]
    assert isinstance(per_host, list)
    return commits(per_host[h - 1])


def commits(value: Value) -> set[Commit]:
    assert isinstance(value, list)
    found: set[Commit] = set()
    for c in value:
        assert isinstance(c, list) and len(c) == 2 and isinstance(c[0], int) and isinstance(c[1], int)
        found.add((c[0], c[1]))
    return found


@dataclass
class Prefix:
    """What Litestream holds under ``terms/<epoch>/``: a snapshot file, then the shipped rows in order."""

    snapshot: Path | None = None
    rows: list[tuple[Commit, tuple[object, ...]]] = field(default_factory=list)


@dataclass
class Host:
    epoch: int = 0
    db: Database | None = None
    path: Path | None = None
    real: dict[Commit, int] = field(default_factory=dict)
    """Each model commit that this host wrote, by its real event id."""


class Replay:
    def __init__(self, tmp: Path, template: Path) -> None:
        self.tmp, self.template = tmp, template
        self.hosts = {1: Host(), 2: Host()}
        self.prefixes: dict[int, Prefix] = {}
        self.issued: list[int] = []
        """Every real id handed out, in order."""
        self.ids: dict[Commit, int] = {}
        """The real event id of each model commit."""
        self.files = 0

    def file(self) -> Path:
        self.files += 1
        return self.tmp / f'db-{self.files}.db'

    def step(self, op: str, h: int, state: dict[str, Value]) -> None:
        host = self.hosts[h]
        match op:
            case 'take':
                ep = state['ep']
                assert isinstance(ep, list)
                epoch = ep[h - 1]
                assert isinstance(epoch, int)
                host.epoch = epoch
            case 'restore':
                self.restore(host, state)
            case 'snapshot':
                self.snapshot(host)
            case 'commit':
                assert host.db is not None
                with host.db.session() as s:
                    row = emit(s, LeaseWorkerJoined(worker=f'w{len(self.issued)}', pool='p'), HOME, System.DAEMON)
                    s.commit()
                    real = row.id
                [made] = of(state, 'mine', h) - set(host.real)
                host.real[made] = real
                self.ids[made] = real
                self.issued.append(real)
            case 'ship':
                shipped = of(state, 'shipped', h)
                prefix = self.prefixes.setdefault(host.epoch, Prefix())
                already = {c for c, _ in prefix.rows}
                for c in sorted(shipped & set(host.real) - already, key=lambda c: c[1]):
                    prefix.rows.append((c, self.row(host, host.real[c])))
            case 'crash' | 'exit' | 'abandon':
                self.close(host)
            case 'pin' | 'serve' | 'lose' | 'restart':
                pass  # the record's source is the model's; serving and noticing a lost term write nothing here
            case _:
                raise AssertionError(f'no step for {op}')

    def row(self, host: Host, real: int) -> tuple[object, ...]:
        assert host.path is not None
        with closing(sqlite3.connect(host.path)) as con:
            return con.execute(
                'SELECT id, at, actor, target_kind, target_id, payload FROM events WHERE id = ?', (real,)
            ).fetchone()

    def restore(self, host: Host, state: dict[str, Value]) -> None:
        source = state['from']
        assert isinstance(source, list)
        pair = source[host_index(self, host)]
        assert isinstance(pair, list)
        epoch, position = pair
        assert isinstance(epoch, int) and isinstance(position, int)
        path = self.file()
        prefix = self.prefixes.get(epoch)
        if position == 0 or prefix is None or prefix.snapshot is None:  # nothing to restore: an empty home
            shutil.copy(self.template, path)
        else:
            shutil.copy(prefix.snapshot, path)
            with closing(sqlite3.connect(path, isolation_level=None)) as con:
                for _, values in prefix.rows[: max(0, position - 1)]:  # segment 1 is the snapshot
                    con.execute(
                        'INSERT INTO events (id, at, actor, target_kind, target_id, payload) VALUES (?, ?, ?, ?, ?, ?)',
                        values,
                    )
        host.path, host.db = path, Database(f'sqlite:///{path}', actor=System.DAEMON)
        host.real = {c: real for c, real in self.ids.items() if self.has(host, real)}
        leader = BucketTerms(
            BucketLeader(url='s3://piloop-replica-model'), host.db, boto3.client('s3', region_name='us-east-1')
        )
        with host.db.session() as s:
            leader.begin(s, Term(epoch=host.epoch, holder=f'h{host_index(self, host) + 1}'))
            terms.begin(s, host.epoch, 1, None)
            s.commit()

    def has(self, host: Host, real: int) -> bool:
        assert host.db is not None
        with host.db.session() as s:
            return s.get(Event, real) is not None

    def snapshot(self, host: Host) -> None:
        assert host.path is not None
        target = self.file()
        with closing(sqlite3.connect(host.path)) as src, closing(sqlite3.connect(target)) as dst:
            src.backup(dst)
        self.prefixes[host.epoch] = Prefix(snapshot=target)

    def close(self, host: Host) -> None:
        if host.db is not None:
            host.db.close()
        host.db = host.path = None
        host.real = {}

    def check(self, state: dict[str, Value]) -> None:
        assert len(self.issued) == len(set(self.issued)), 'an id handed out twice'
        phase, db = state['phase'], state['db']
        assert isinstance(phase, list) and isinstance(db, list)
        for h, host in self.hosts.items():
            if phase[h - 1] in ('restored', 'holding', 'stale') and host.db is not None:
                expected = {self.ids[c] for c in commits(db[h - 1])}
                with host.db.session() as s:
                    real = {i for i in self.issued if s.get(Event, i) is not None}
                assert real == expected, f'host {h} holds the commits that the model says'

    def finish(self) -> None:
        for host in self.hosts.values():
            self.close(host)


def host_index(replay: Replay, host: Host) -> int:
    return next(h for h, found in replay.hosts.items() if found is host) - 1


@pytest.fixture
def template(tmp_path: Path, db: Database) -> Path:
    """An empty home's database at head, which a term restores when there is no source."""
    path = tmp_path / 'template.db'
    with closing(sqlite3.connect(db.engine.url.database or '')) as src, closing(sqlite3.connect(path)) as dst:
        src.backup(dst)
    return path


def test_every_transition_of_the_replica_model_holds_on_real_databases(tmp_path: Path, template: Path) -> None:
    """Restores from the pinned source, the lift and real writes: on every path no id is handed out twice, and each
    term holds exactly the commits that the model gives it."""
    paths = sorted(TRACES.glob('*.itf.json'))
    assert paths, 'the traces are there'
    for path in paths:
        trace = json.loads(path.read_text())
        states = [_value(st['s']) for st in trace['states']]
        replay = Replay(tmp_path / path.stem, template)
        replay.tmp.mkdir()
        op, h = 'init', 0
        try:
            for state in states[1:]:
                assert isinstance(state, dict)
                op, h = state['op'], state['arg']
                assert isinstance(op, str) and isinstance(h, int)
                replay.step(op, h, state)
                replay.check(state)
        except AssertionError as e:
            raise AssertionError(f'{path.name}: after {op}({h}): {e}') from e
        finally:
            replay.finish()
