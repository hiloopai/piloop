"""The paths of ``tests/models/replica.tla``, replayed against the real ``bucket`` leader on moto's S3: takes, restores
from the pinned source, the pin, the naming of a term's own replica, the lift, and real writes.

Each host's process has its own home, whose database is a real file, and its own ``BucketTerms``. A restore runs in a
thread held at its pin (the PUT of the record), as the model's ``Restore`` and ``Pin`` split it. Litestream is faked
(``tests.leader.replica``): the replay ships each commit by hand, as the model's ``Ship``. A commit is an event that
the daemon writes (``emit``); its id is what a worker holds. After each step: no id was handed out twice, each host's
database holds exactly the model's commits, and the record's source is the model's.

The traces cover every kind of transition of ``replica_graph.cfg`` that the replay tells apart (``--by`` below);
``scripts/graph_traces.py`` wrote them from TLC's ``-dump dot,actionlabels``.
"""

import json
import shutil
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import boto3
import pytest
from moto import mock_aws
from mypy_boto3_s3 import S3Client

from piloop.actor import System
from piloop.db import terms
from piloop.db.events import emit
from piloop.db.session import Database
from piloop.ids import HOME
from piloop.plugins.executor.kit.leases import LeaseWorkerJoined
from piloop.plugins.leader.bucket import BucketLeader, BucketTerms, Record, Source
from piloop.plugins.leader.contract import LeaderHost, LostTerm, Term
from tests.leader.replica import FakeReplica
from tests.leader.test_bucket import SKEW, TTL, Clock

TRACES = Path(__file__).parent / 'traces' / 'replica'
BUCKET = 'piloop-replica-model'
URL = f's3://{BUCKET}/cell'
ALL = 99
"""The model's position past every segment: the whole prefix."""
WAIT = 10

type Value = int | str | bool | list[Value] | dict[str, Value]
type Commit = tuple[int, int]
"""A model commit: its writer's epoch, and its id in the model."""


def _value(itf: object) -> Value:
    match itf:
        case {'#bigint': str(n)}:
            return int(n)
        case {'#set': list(items)}:
            return [_value(v) for v in items]
        case dict():
            return {str(k): _value(v) for k, v in itf.items()}
        case list():
            return [_value(v) for v in itf]
        case bool() | str():
            return itf
    raise AssertionError(f'not an ITF value: {itf!r}')


def ints(value: Value) -> list[int]:
    assert isinstance(value, list)
    return [v for v in value if isinstance(v, int) and not isinstance(v, bool)]


def of(state: dict[str, Value], name: str, h: int) -> set[Commit]:
    """Host ``h``'s set of commits in the model's variable ``name``."""
    per_host = state[name]
    assert isinstance(per_host, list)
    mine = per_host[h - 1]
    assert isinstance(mine, list)
    found: set[Commit] = set()
    for c in mine:
        epoch, n = ints(c)
        found.add((epoch, n))
    return found


@dataclass
class Pin:
    """A restore in a thread of its own, held before its pin, the PUT of the record."""

    call: Any
    held: threading.Event = field(default_factory=threading.Event)
    go: threading.Event = field(default_factory=threading.Event)
    drop: bool = False
    result: bool | None = None
    error: BaseException | None = None
    thread: threading.Thread | None = None

    def start(self) -> None:
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        for _ in range(WAIT * 1000):
            if self.held.wait(0.001) or not self.thread.is_alive():
                return
        raise AssertionError('neither held at its pin nor ended')

    def _run(self) -> None:
        try:
            self.result = self.call()
        except (LostTerm, RuntimeError) as e:
            self.error = e

    def sending(self, **_: Any) -> None:
        self.held.set()
        assert self.go.wait(WAIT), 'never let go of'
        if self.drop:  # its process died: the PUT never goes
            raise RuntimeError('the process is gone')

    def finish(self) -> None:
        self.go.set()
        assert self.thread is not None
        self.thread.join(WAIT)
        assert not self.thread.is_alive()


@dataclass
class Process:
    """One ``piloop serve`` on a host: its home, its record of the terms, its term, and its database once open."""

    home: Path
    terms: BucketTerms
    name: str
    term: Term | None = None
    db: Database | None = None
    pin: Pin | None = None
    real: dict[Commit, int] = field(default_factory=dict)


class Replay:
    def __init__(self, tmp: Path, template: Path, client: S3Client) -> None:
        self.tmp, self.template, self.client = tmp, template, client
        self.clock = Clock()
        self.replica = FakeReplica(client, automatic=False)
        self.procs: dict[int, Process | None] = {1: None, 2: None}
        self.started = 0
        self.issued: list[int] = []
        self.ids: dict[Commit, int] = {}
        for h in (1, 2):
            self.restart(h)

    def proc(self, h: int) -> Process:
        proc = self.procs[h]
        assert proc is not None, f'host {h} runs no daemon'
        return proc

    def restart(self, h: int) -> None:
        self.started += 1
        home = self.tmp / f'home-{self.started}'
        home.mkdir()
        shutil.copy(self.template, home / 'piloop.db')
        client = boto3.client('s3')
        config = BucketLeader(url=URL, ttl=TTL, max_skew=SKEW)
        leader = BucketTerms(config, LeaderHost(home, home / 'piloop.db'), client, self.clock, self.replica)
        proc = Process(home, leader, f'h{h}/{self.started}')
        client.meta.events.register(
            'before-call.s3.PutObject', lambda **kw: proc.pin.sending(**kw) if proc.pin else None
        )
        self.procs[h] = proc

    def step(self, op: str, h: int, state: dict[str, Value]) -> None:
        match op:
            case 'take':
                proc = self.proc(h)
                self.clock.now += TTL + SKEW  # whoever held the term was paused past it: the model's fault
                proc.term = proc.terms.attempt(proc.name, None)
                assert proc.term is not None and proc.term.epoch == ints(state['ep'])[h - 1]
            case 'restore':
                proc = self.proc(h)
                term = proc.term
                assert term is not None
                proc.pin = Pin(lambda: proc.terms.restore(term))
                proc.pin.start()
            case 'pin':
                self.pinned(self.proc(h))
            case 'abandon':
                proc = self.proc(h)
                if proc.pin is None:  # it never began its restore
                    assert proc.term is not None
                    with pytest.raises(LostTerm):
                        proc.terms.restore(proc.term)
                else:
                    proc.pin.finish()
                    assert isinstance(proc.pin.error, LostTerm), 'its pin is refused'
                self.gone(h)
            case 'snapshot':
                proc = self.proc(h)
                assert proc.term is not None
                self.replica.copy(self.url(proc), proc.home / 'piloop.db')
                try:
                    assert proc.terms.named(proc.term), 'its replica holds the whole database'
                except LostTerm:
                    assert proc.term.epoch < max(ints(state['ep'])), 'a stale term cannot name its replica'
            case 'commit':
                proc = self.proc(h)
                assert proc.db is not None
                with proc.db.session() as s:
                    row = emit(s, LeaseWorkerJoined(worker=f'w{len(self.issued)}', pool='p'), HOME, System.DAEMON)
                    s.commit()
                    real = row.id
                [made] = of(state, 'mine', h) - set(proc.real)
                proc.real[made] = self.ids[made] = real
                self.issued.append(real)
            case 'ship':
                proc = self.proc(h)
                shipped = {c for c, _ in self.shipped(proc)}
                for c in sorted(of(state, 'shipped', h) - shipped, key=lambda c: c[1]):
                    self.replica.ship(self.url(proc), self.row(proc, proc.real[c]))
            case 'crash' | 'exit':
                self.gone(h)
            case 'serve' | 'lose':
                pass  # the daemon serves, or learns that it lost its term: it writes nothing here
            case 'restart':
                self.restart(h)
            case _:
                raise AssertionError(f'no step for {op}')

    def url(self, proc: Process) -> str:
        assert proc.term is not None
        return f'{URL}/terms/{proc.term.epoch}'

    def shipped(self, proc: Process) -> list[tuple[Commit, object]]:
        rows = [r for r in self.replica.segments.get(self.url(proc), []) if not isinstance(r, Path)]
        by_id = {real: c for c, real in self.ids.items()}
        return [(by_id[real], r) for r in rows if isinstance(real := r[0], int)]

    def row(self, proc: Process, real: int) -> tuple[object, ...]:
        with closing(sqlite3.connect(proc.home / 'piloop.db')) as con:
            return con.execute(
                'SELECT id, at, actor, target_kind, target_id, payload FROM events WHERE id = ?', (real,)
            ).fetchone()

    def pinned(self, proc: Process) -> None:
        """Let the restore's pin go, then begin the term as the daemon does: the lift, and its term row."""
        assert proc.pin is not None and proc.term is not None
        proc.pin.finish()
        assert proc.pin.error is None, f'the pin goes: {proc.pin.error}'
        proc.pin = None
        proc.db = Database(f'sqlite:///{proc.home / "piloop.db"}', actor=System.DAEMON)
        with proc.db.session() as s:
            proc.terms.begin(s, proc.term)
            terms.begin(s, proc.term.epoch, 1, None)
            s.commit()

    def gone(self, h: int) -> None:
        proc = self.procs[h]
        if proc is None:
            return
        if proc.pin is not None and proc.pin.thread is not None and proc.pin.thread.is_alive():
            proc.pin.drop = True
            proc.pin.finish()
        if proc.db is not None:
            proc.db.close()
        self.procs[h] = None

    def check(self, state: dict[str, Value]) -> None:
        assert len(self.issued) == len(set(self.issued)), 'an id handed out twice'
        phase, src = state['phase'], ints(state['src'])
        assert isinstance(phase, list)
        for h, proc in self.procs.items():
            if proc is not None and phase[h - 1] in ('restored', 'holding', 'stale'):
                with closing(sqlite3.connect(proc.home / 'piloop.db')) as con:
                    held = {r[0] for r in con.execute('SELECT id FROM events')}
                assert held & set(self.issued) == {self.ids[c] for c in of(state, 'db', h)}, (
                    f"host {h} holds the model's"
                )
        expected = None if src[0] == 0 else Source(epoch=src[0], txid=None if src[1] == ALL else f'{src[1]:016x}')
        found = self.record()
        assert (found.source if found is not None else None) == expected, "the record names the model's source"

    def record(self) -> Record | None:
        try:
            body = self.client.get_object(Bucket=BUCKET, Key='cell/term.json')['Body'].read()
        except self.client.exceptions.NoSuchKey:
            return None
        return Record.model_validate_json(body)

    def finish(self) -> None:
        for h in (1, 2):
            self.gone(h)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[S3Client]:
    for name in ('AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY'):
        monkeypatch.setenv(name, 'testing')
    monkeypatch.setenv('AWS_DEFAULT_REGION', 'us-east-1')
    with mock_aws():
        s3 = boto3.client('s3')
        s3.create_bucket(Bucket=BUCKET)
        yield s3


@pytest.fixture
def template(tmp_path: Path, db: Database) -> Path:
    """An empty home's database at head: what each new process starts from."""
    path = tmp_path / 'template.db'
    with closing(sqlite3.connect(db.engine.url.database or '')) as src, closing(sqlite3.connect(path)) as dst:
        src.backup(dst)
    return path


def test_every_transition_of_the_replica_model_holds_on_the_real_bucket_leader(
    tmp_path: Path, template: Path, client: S3Client
) -> None:
    """Restores from the pinned source, pins refused once another term took over, and the lift: on every path no id is
    handed out twice, each term holds exactly the model's commits, and the record names the model's source."""
    paths = sorted(TRACES.glob('*.itf.json'))
    assert paths, 'the traces are there'
    for path in paths:
        trace = json.loads(path.read_text())
        states = [_value(st['s']) for st in trace['states']]
        for key in client.list_objects_v2(Bucket=BUCKET).get('Contents', []):
            client.delete_object(Bucket=BUCKET, Key=key['Key'])
        replay = Replay(tmp_path / path.stem, template, client) if (tmp_path / path.stem).mkdir() is None else None
        assert replay is not None
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
