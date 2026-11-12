"""The paths of ``tests/models/term.tla``, replayed against the real ``bucket`` leader on moto's S3: two hosts that take,
renew and release one term, with clocks that differ by up to ``SKEW``, slow requests, crashes and restarts.

Each host's process is a ``BucketTerms`` with its own wall clock (real time plus the model's offset) and its own
monotonic one. Its writes run in a thread of their own, held at their send and at their answer, as the model's
``Take``/``Renew``/``Release``, ``Land`` and ``Answer`` split them. A held write of a dead process may still land, as
in the model. After each step, the object in the bucket and each holder's end (``piloop.daemon.holding.deadline``)
are the model's.

The traces cover every kind of transition of ``term_graph.cfg`` that the replay tells apart (``--by`` below);
``scripts/graph_traces.py`` wrote them from TLC's ``-dump dot,actionlabels``.
"""

import json
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import boto3
import pytest
from moto import mock_aws
from mypy_boto3_s3 import S3Client

from piloop.daemon.holding import deadline
from piloop.db.session import Database
from piloop.plugins.leader.bucket import BucketLeader, BucketTerms
from piloop.plugins.leader.contract import LostTerm, Term
from tests.conftest import leader_host

TRACES = Path(__file__).parent / 'traces' / 'term'
BUCKET = 'piloop-term-model'
KEY = 'cell/term.json'
TTL, MAX_SKEW = 2, 1
"""``term_graph.cfg``, in seconds: a tick is one second."""
CONFIG = BucketLeader(
    url=f's3://{BUCKET}/cell',
    ttl=timedelta(seconds=TTL),
    renew=timedelta(seconds=0.5),
    deadline=timedelta(seconds=1),
    max_skew=timedelta(seconds=MAX_SKEW),
)
BASE = datetime(2026, 11, 11, tzinfo=UTC)
WAIT = 10
"""How long a step waits for a thread: only a bug makes it wait that long."""

type Value = int | str | bool | list[Value] | dict[str, Value]


def ints(value: Value) -> list[int]:
    assert isinstance(value, list)
    return [v for v in value if isinstance(v, int) and not isinstance(v, bool)]


def _value(itf: object) -> Value:
    match itf:
        case {'#bigint': str(n)}:
            return int(n)
        case dict():
            return {str(k): _value(v) for k, v in itf.items()}
        case list():
            return [_value(v) for v in itf]
        case bool() | str():
            return itf
    raise AssertionError(f'not an ITF value: {itf!r}')


@dataclass
class Write:
    """One write of a host, in a thread held at its send and at its answer."""

    call: Callable[[], Term | None]
    sent: threading.Event = field(default_factory=threading.Event)
    land: threading.Event = field(default_factory=threading.Event)
    landed: threading.Event = field(default_factory=threading.Event)
    answer: threading.Event = field(default_factory=threading.Event)
    ok: bool | None = None
    result: Term | None = None
    error: BaseException | None = None
    thread: threading.Thread | None = None

    def start(self) -> None:
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        for _ in range(WAIT * 1000):
            if self.sent.wait(0.001) or not self.thread.is_alive():
                break
        assert self.sent.is_set(), f'it sent no write: {self.error!r}'

    def _run(self) -> None:
        try:
            self.result = self.call()
        except LostTerm as e:
            self.error = e

    def sending(self, **_: Any) -> None:
        """In the thread, before the PUT goes: wait until the replay lets it land."""
        self.sent.set()
        assert self.land.wait(WAIT), 'never landed'

    def answered(self, http_response: Any, **_: Any) -> None:
        """In the thread, once S3 answered: wait until the replay lets the process take the answer."""
        self.ok = http_response.status_code < 300
        self.landed.set()
        assert self.answer.wait(WAIT), 'never answered'

    def to_land(self) -> None:
        self.land.set()
        assert self.landed.wait(WAIT), 'it did not land'

    def to_answer(self) -> None:
        self.answer.set()
        assert self.thread is not None
        self.thread.join(WAIT)
        assert not self.thread.is_alive(), 'it did not end'


@dataclass
class Process:
    """One ``piloop serve`` on host ``h``: its record of the terms, what it holds, and when that ends."""

    terms: BucketTerms
    name: str
    write: Write | None = None
    """Its write under way, which the client's hooks hold."""
    term: Term | None = None
    ends: float | None = None


class Replay:
    def __init__(self, db: Database, client: S3Client, off: list[int]) -> None:
        self.db, self.client, self.off = db, client, off
        self.real = 0
        self.starts = [0, 0]
        self.procs: list[Process | None] = [None, None]
        self.writes: list[Write | None] = [None, None]
        """Each host's write that has not answered yet: a dead process's may still land."""
        for h in (1, 2):
            self.restart(h)

    def wall(self, h: int) -> Callable[[], datetime]:
        return lambda: BASE + timedelta(seconds=self.real + self.off[h - 1])

    def proc(self, h: int) -> Process:
        proc = self.procs[h - 1]
        assert proc is not None, f'host {h} runs no daemon'
        return proc

    def restart(self, h: int) -> None:
        self.starts[h - 1] += 1
        client = boto3.client('s3')
        proc = Process(BucketTerms(CONFIG, leader_host(self.db), client, self.wall(h)), f'h{h}/{self.starts[h - 1]}')
        client.meta.events.register('before-call.s3.PutObject', lambda **kw: self.write_of(proc).sending(**kw))
        client.meta.events.register('after-call.s3.PutObject', lambda **kw: self.write_of(proc).answered(**kw))
        self.procs[h - 1] = proc

    @staticmethod
    def write_of(proc: Process) -> Write:
        assert proc.write is not None, 'a write that the replay did not start'
        return proc.write

    def send(self, h: int, call: Callable[[Process], Term | None]) -> None:
        proc = self.proc(h)
        write = Write(lambda: call(proc))
        proc.write = self.writes[h - 1] = write
        write.start()

    def step(self, op: str, h: int, state: dict[str, Value]) -> None:
        match op:
            case 'read':
                self.read(h, state)
            case 'slow':  # it just waits to land
                pass
            case 'take':
                self.send(h, lambda p: p.terms.attempt(p.name, None))
            case 'renew':
                self.send(h, lambda p: p.terms.renew(self.held(p)))
            case 'release':  # after the daemon's last write
                self.send(h, lambda p: (p.terms.release(self.held(p)), None)[1])
                self.proc(h).ends = None
            case 'land':
                write = self.writes[h - 1]
                assert write is not None
                write.to_land()
                req = state['req']
                assert isinstance(req, list)
                if req[h - 1] != 'none':  # a dead process's write: the record tells
                    assert write.ok == (req[h - 1] in ('took', 'renewed', 'released')), 'S3 took it as the model did'
                if self.procs[h - 1] is None:  # its process is gone: nobody takes the answer
                    write.to_answer()
                    self.writes[h - 1] = None
            case 'answer':
                self.answer(h)
            case 'self_stop':
                proc = self.proc(h)
                assert proc.ends is not None and proc.ends <= self.real, 'its end came'
                self.procs[h - 1] = None
            case 'crash':
                self.procs[h - 1] = None
                if (write := self.writes[h - 1]) is not None and write.landed.is_set():
                    write.to_answer()  # an answer that nobody takes
                    self.writes[h - 1] = None
            case 'restart':
                self.restart(h)
            case 'tick':
                self.real += 1
            case _:
                raise AssertionError(f'no step for {op}')

    def read(self, h: int, state: dict[str, Value]) -> None:
        """While the model sees the term held, a take by the code reads it and writes nothing: no early take."""
        rec = state['rec']
        assert isinstance(rec, dict)
        age, ttl, off = (ints([rec[k]])[0] for k in ('age', 'ttl', 'off'))
        if rec['none'] is True or age >= ttl + MAX_SKEW + off - self.off[h - 1]:  # the model's `Expired`
            return
        proc = self.proc(h)
        proc.write = None  # a write now fails the step (`write_of`)
        assert proc.terms.attempt(proc.name, None) is None, 'it takes no held term'

    def held(self, proc: Process) -> Term:
        assert proc.term is not None, 'it holds a term'
        return proc.term

    def answer(self, h: int) -> None:
        proc, write = self.proc(h), self.writes[h - 1]
        assert write is not None
        write.to_answer()
        self.writes[h - 1] = None
        if write.result is not None:  # took or renewed
            proc.term = write.result
            proc.ends = deadline(write.result, float(self.real), self.wall(h)())
        elif proc.term is not None:  # a release, or a refused renewal: the daemon exits
            self.procs[h - 1] = None

    def check(self, state: dict[str, Value]) -> None:
        rec, left, phase = state['rec'], ints(state['left']), state['phase']
        assert isinstance(rec, dict) and isinstance(phase, list)
        try:
            body = self.client.get_object(Bucket=BUCKET, Key=KEY)['Body'].read()
        except self.client.exceptions.NoSuchKey:
            assert rec['none'] is True, 'the record is there'
        else:
            stored = Term.model_validate_json(body)
            assert rec['none'] is False, 'no record yet'
            assert (stored.epoch, stored.holder.split('/')[0]) == (rec['epoch'], f'h{rec["holder"]}')
        for h in (1, 2):
            proc = self.procs[h - 1]
            writing = proc is not None and proc.ends is not None and self.real < proc.ends
            assert writing == (phase[h - 1] == 'holding' and left[h - 1] > 0), f'host {h} may write'
            if writing:
                assert proc is not None and proc.ends is not None
                assert proc.ends - self.real == left[h - 1], f'host {h} writes as long as the model says'

    def finish(self) -> None:
        """Let every write that the path left held run to its end: a path may end anywhere in the graph."""
        for write in self.writes:
            if write is not None and write.thread is not None:
                write.land.set()
                write.answer.set()
                write.thread.join(WAIT)  # before the next path starts: it would land in it


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[S3Client]:
    for name in ('AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY'):
        monkeypatch.setenv(name, 'testing')
    monkeypatch.setenv('AWS_DEFAULT_REGION', 'us-east-1')
    with mock_aws():
        s3 = boto3.client('s3')
        s3.create_bucket(Bucket=BUCKET)
        yield s3


def test_every_transition_of_the_term_model_holds_on_the_real_bucket_leader(db: Database, client: S3Client) -> None:
    """Takes, renewals and releases on S3 do what the model does on every path: the same record, the same epochs, and
    each holder writes exactly as long as the model lets it, with the clocks skewed and the writes slow."""
    paths = sorted(TRACES.glob('*.itf.json'))
    assert paths, 'the traces are there'
    for path in paths:
        trace = json.loads(path.read_text())
        states = [_value(st['s']) for st in trace['states']]
        first = states[0]
        assert isinstance(first, dict)
        client.delete_object(Bucket=BUCKET, Key=KEY)
        replay = Replay(db, client, ints(first['off']))
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
