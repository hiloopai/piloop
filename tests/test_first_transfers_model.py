"""Replay the traces of ``tests/models/first_transfers.tla`` against the real first transfers.

The traces come from TLC's state graph of the model's fixed configuration (``scripts/graph_traces.py``, ``--by``
the variables that it keeps): together they take every kind of transition that the replay can tell apart. Each
request, the daemon's ``run`` and each build thread is a real thread that runs to its next boundary (a write
transaction, ``run``'s lock, the build itself), and waits there for the trace's next step. Only the build, the clock and
a restart of the daemon are stand-ins. After each step the real row, the real
threads and the consumer's memory must be the model's.
"""

import json
import queue
import sqlite3
import threading
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace, TracebackType

import pytest
from covalence import Cid
from git import Repo
from sqlalchemy import delete
from sqlalchemy.orm import Session

from piloop.daemon.loop import Loop, Turn
from piloop.db.feed import newest
from piloop.db.models import Campaign, utcnow
from piloop.db.session import Database
from piloop.ids import Revision
from piloop.otel import Tracer
from piloop.plugins.cas.contract import Cas
from piloop.plugins.executor.contract import RepoId
from piloop.plugins.executor.kit import transfers as transfers_module
from piloop.plugins.executor.kit.clones import NotSent
from piloop.plugins.executor.kit.tables import FirstTransfer, FirstTransferState
from piloop.plugins.executor.kit.transfers import BUILDS, FirstTransferBuilds, FirstTransfers
from piloop.plugins.vcs.git import PILOOP, GitRepository
from tests.conftest import disk

TRACES = Path(__file__).parent / 'traces' / 'first_transfers'
DEADLINE = 60.0
"""One tick of the model."""
STEP_SECONDS = 10.0
"""How long a thread of the real code may take to reach its next boundary."""


@dataclass(frozen=True)
class State:
    """A state of the model, as its trace keeps it."""

    row: str
    age: int
    kid: list[str]
    th: list[str]
    crashes: int
    unsettled: bool
    due: bool
    building: bool

    @classmethod
    def of(cls, itf: dict[str, object]) -> State:
        def number(value: object) -> int:
            assert isinstance(value, dict)
            return int(value['#bigint'])

        def strings(value: object) -> list[str]:
            assert isinstance(value, list)
            return [str(item) for item in value]

        return cls(
            row=str(itf['row']),
            age=number(itf['age']),
            kid=strings(itf['kid']),
            th=strings(itf['th']),
            crashes=number(itf['crashes']),
            unsettled=itf['unsettled'] is True,
            due=itf['due'] is True,
            building=itf['building'] is True,
        )


class Killed(BaseException):
    """Ends a thread of a daemon that the trace restarts: no ``except Exception`` of the code catches it."""


_actor = threading.local()


def _current() -> Actor | None:
    return getattr(_actor, 'actor', None)


@dataclass
class Actor:
    """A thread of the real code: it runs to its next boundary, then waits there for the trace's next step."""

    name: str
    events: queue.Queue[tuple[str, object]] = field(default_factory=queue.Queue)
    go: threading.Semaphore = field(default_factory=lambda: threading.Semaphore(0))
    raises: BaseException | None = None
    at: str | None = None
    """The boundary that it waits at, or None once it ended."""
    locked: bool = False

    def start(self, call: Callable[[], object]) -> tuple[str, object]:
        def body() -> None:
            _actor.actor = self
            try:
                self.events.put(('done', call()))
            except BaseException as e:  # the step's outcome, which the driver checks
                self.events.put(('raised', e))

        threading.Thread(target=body, name=self.name, daemon=True).start()
        return self.next()

    def pause(self, where: str) -> None:
        self.events.put(('at', where))
        self.go.acquire()
        if (raises := self.raises) is not None:
            self.raises = None
            raise raises

    def resume(self, raises: BaseException | None = None) -> tuple[str, object]:
        self.raises = raises
        self.go.release()
        return self.next()

    def next(self) -> tuple[str, object]:
        kind, value = self.events.get(timeout=STEP_SECONDS)
        self.at = value if kind == 'at' and isinstance(value, str) else None
        return kind, value


class GatedLock:
    """``FirstTransferBuilds._lock``: ``run`` waits before it takes it the first time, for the trace's lock step."""

    def __init__(self) -> None:
        self._lock = threading.Lock()

    def __enter__(self) -> None:
        if (actor := _current()) is not None and actor.name == 'run' and not actor.locked:
            actor.locked = True
            actor.pause('lock')
        self._lock.acquire()

    def __exit__(self, *_: type[BaseException] | BaseException | TracebackType | None) -> None:
        self._lock.release()


class Record(GitRepository):
    """The repo of record: the build of its first transfer waits for the trace's step, then ends as it says."""

    def __init__(self, path: Path, cas: Cas) -> None:
        super().__init__(Repo(path), PILOOP)
        self._cas = cas
        self._built = 0

    def send(self, cas: Cas, revisions: Sequence[Revision], have: Sequence[Revision]) -> Cid | None:
        if have:  # the versions beyond the first transfer: none
            return None
        if (actor := _current()) is not None:
            actor.pause('build')
        self._built += 1
        return cas.put(f'first transfer {self._built}'.encode())


@dataclass
class Driver:
    """The real first transfers of one repo, driven by the model's steps."""

    db: Database
    monkeypatch: pytest.MonkeyPatch
    repo: RepoId
    record: Record
    cas: Cas
    now: datetime
    lost: set[Cid] = field(default_factory=set)
    kids: dict[int, Actor] = field(default_factory=dict)
    answers: dict[int, str] = field(default_factory=dict)
    run: Actor | None = None
    builders: dict[int, Actor] = field(default_factory=dict)
    started: list[Actor] = field(default_factory=list)
    unasked: list[str] = field(default_factory=list)
    due: datetime | None = None
    crashes: int = 0
    builds: FirstTransferBuilds = field(init=False)
    sends: list[FirstTransfers] = field(init=False)

    def __post_init__(self) -> None:
        self.sends = [FirstTransfers(self.db, self.cas, lambda repo: self.record, DEADLINE) for _ in range(2)]
        self._daemon()

    def time(self) -> datetime:
        """The time that the code reads: a microsecond later on each read, as no two reads of a clock are the same."""
        self.now += timedelta(microseconds=1)
        return self.now

    def _daemon(self) -> None:
        self.builds = FirstTransferBuilds(
            self.db, self.cas, lambda repo: self.record, Tracer(None), DEADLINE, self.wake
        )
        self.monkeypatch.setattr(self.builds, '_lock', GatedLock())  # run's lock step

    def thread(
        self, *, target: Callable[..., None], args: tuple[object, ...], name: str, daemon: bool
    ) -> SimpleNamespace:
        """``threading.Thread`` in ``transfers``: a build thread that the trace drives."""
        with self.db.session() as s:
            row = s.get(FirstTransfer, self.repo)
            if row is None or row.state is not FirstTransferState.REQUESTED:
                self.unasked.append(f'a build started on a row that is {row and row.state}')
        actor = Actor(name)
        self.started.append(actor)
        return SimpleNamespace(start=lambda: self._first_event(actor, actor.start(lambda: target(*args))))

    def _first_event(self, actor: Actor, event: tuple[str, object]) -> None:
        assert event == ('at', 'build'), event

    def wake(self) -> None:
        """The consumer runs in the next turn of the daemon's loop: a due of now."""
        self.due = self.now

    def restart(self) -> None:
        """The daemon ends: its threads go, and the next one starts with no memory."""
        for actor in [*([self.run] if self.run is not None and self.run.at else []), *self.builders.values()]:
            if actor.at is not None:
                kind, _ = actor.resume(Killed())
                assert kind == 'raised'
        self.run, self.builders, self.due = None, {}, None
        self.crashes += 1
        self._daemon()

    def step(self, action: str, before: State, after: State) -> None:
        changed = {k for k in vars(after) if getattr(after, k) != getattr(before, k)}
        STEPS[action](self, before, after, changed)

    def read(self, before: State, after: State, changed: set[str]) -> None:
        c = _kid(before, after)
        kid = self.kids.setdefault(c, Actor(f'send-{c}'))
        self._answer(c, kid.start(lambda: self.sends[c - 1].send(self.repo, [self.record.head().revision], [])))

    def write(self, before: State, after: State, changed: set[str]) -> None:
        c = _kid(before, after)
        self._answer(c, self.kids[c].resume())

    def run_read(self, before: State, after: State, changed: set[str]) -> None:
        self.run = Actor('run')
        assert self.run.start(lambda: self.builds.run(Turn(self.now))) == ('at', 'lock')

    def run_lock(self, before: State, after: State, changed: set[str]) -> None:
        assert self.run is not None
        event = self.run.resume()
        if 'crashes' in changed or after.row != before.row:  # the late end, written or not
            assert event == ('at', 'write'), event
            self._ran(self.run.resume(_locked() if 'crashes' in changed else None))
        elif after.building and not before.building:
            assert event == ('at', 'write'), event  # the update of the new one's start
        else:
            self._ran(event)

    def run_start(self, before: State, after: State, changed: set[str]) -> None:
        assert self.run is not None
        self.started.clear()
        self._ran(self.run.resume(_locked() if 'crashes' in changed else None))
        for i in (1, 2):
            if after.th[i - 1] == 'building' and before.th[i - 1] == 'idle':
                [self.builders[i]] = self.started
        assert not self.started or self.builders, f'a build that the model has not: {self.unasked}'

    def build_end(self, before: State, after: State, changed: set[str]) -> None:
        i = _thread(before, after)
        failed = None if after.th[i - 1] == 'ok' else RuntimeError('the send failed')
        assert self.builders[i].resume(failed) == ('at', 'write')

    def settle(self, before: State, after: State, changed: set[str]) -> None:
        kind, _ = self.builders.pop(_thread(before, after)).resume(_locked() if 'unsettled' in changed else None)
        assert kind == 'done'

    def lose(self, before: State, after: State, changed: set[str]) -> None:
        with self.db.session() as s:
            blob = s.get_one(FirstTransfer, self.repo).blob
        assert blob is not None
        self.lost.add(blob)

    def tick(self, before: State, after: State, changed: set[str]) -> None:
        self.now += timedelta(seconds=DEADLINE, milliseconds=1)  # past it, not at it

    def crash(self, before: State, after: State, changed: set[str]) -> None:
        self.restart()

    def loop_only(self, before: State, after: State, changed: set[str]) -> None:
        """The loop's due, or the end: the next RunRead runs the consumer."""

    def _answer(self, c: int, event: tuple[str, object]) -> None:
        match event:
            case ('at', 'write'):
                self.answers[c] = 'write'
            case ('done', None):
                self.answers[c] = 'wait'
            case ('done', list()):
                self.answers[c] = 'ok'
            case ('raised', NotSent()):
                self.answers[c] = 'failed'
            case _:
                raise AssertionError(f'request {c}: {event}')

    def _ran(self, event: tuple[str, object]) -> None:
        match event:
            case ('done', due):
                assert due is None or isinstance(due, datetime)
                self.due = due
                self.run = None
            case ('raised', sqlite3.OperationalError()):  # a write of the consumer raised: the daemon ends
                self.run = None
                self.restart()
            case _:
                raise AssertionError(f'run: {event}')

    def check(self, action: str, s: State) -> None:
        """The real state is the model's ``s``."""
        with self.db.session() as session:
            row = session.get(FirstTransfer, self.repo)
        match row:
            case None:
                real = 'none'
            case FirstTransfer(state=FirstTransferState.BUILT, blob=blob):
                real = 'lost' if blob in self.lost else 'built'
            case _:
                real = str(row.state)
        assert real == s.row, f'the row is {real}, the model has {s.row}'
        if row is not None and row.state is FirstTransferState.REQUESTED:
            past = int(self.now - row.started_at > timedelta(seconds=DEADLINE))
            assert past == s.age, f'the row is {"past" if past else "within"} its deadline, the model has {s.age}'
        assert [self.answers.get(c, 'ask') for c in range(1, len(s.kid) + 1)] == s.kid
        held = ['idle', 'idle']
        for i, actor in self.builders.items():
            held[i - 1] = 'building' if actor.at == 'build' else 'written' if actor.at == 'write' else 'ended'
        model = ['written' if t in ('ok', 'fail') else t for t in s.th]
        assert held == model, f'the build threads are {held}, the model has {s.th}: {self.unasked}'
        assert not self.unasked, self.unasked
        assert bool(self.builds._building) == s.building, '_building'
        assert bool(self.builds._unsettled) == s.unsettled, '_unsettled'
        assert self.crashes == s.crashes
        if action in ('RunLock', 'RunStart') and self.run is None:
            assert (self.due is not None) == s.due, f'run returned the due {self.due}, the model has {s.due}'

    def end(self) -> None:
        """Let every thread that waits go, as a stop of the test's daemon."""
        for actor in [*self.kids.values(), *self.builders.values(), *([self.run] if self.run else [])]:
            if actor.at is not None:
                actor.resume(Killed())


STEPS: dict[str, Callable[[Driver, State, State, set[str]], None]] = {
    'Read': Driver.read,
    'Write': Driver.write,
    'RunRead': Driver.run_read,
    'RunLock': Driver.run_lock,
    'RunStart': Driver.run_start,
    'BuildEnd': Driver.build_end,
    'Settle': Driver.settle,
    'Lose': Driver.lose,
    'Tick': Driver.tick,
    'Restart': Driver.crash,
    'DueFire': Driver.loop_only,
    'Done': Driver.loop_only,
}
"""Each step of the model, by its action."""


def _locked() -> sqlite3.OperationalError:
    return sqlite3.OperationalError('database is locked')


def _kid(before: State, after: State) -> int:
    """The request of the step: the one that changed, or else one that waits and reads the row again."""
    pairs = list(enumerate(zip(before.kid, after.kid, strict=True), start=1))
    moved = [c for c, (b, a) in pairs if b != a]
    return moved[0] if moved else next(c for c, (b, _) in pairs if b == 'wait')


def _thread(before: State, after: State) -> int:
    return next(i for i, (b, a) in enumerate(zip(before.th, after.th, strict=True), start=1) if b != a)


def traces(directory: Path) -> Iterator[tuple[str, list[str], list[State]]]:
    for path in sorted(directory.glob('*.itf.json'), key=lambda p: (len(p.name), p.name)):
        itf = json.loads(path.read_text())
        yield path.name, itf['#meta']['actions'], [State.of(state['s']) for state in itf['states']]


def fixed_only(actions: list[str], states: list[State]) -> bool:
    """Whether the trace takes the fix's step: a start that finds the row no longer requested starts no build."""
    return any(
        action == 'RunStart' and before.th == after.th and before.crashes == after.crashes
        for action, before, after in zip(actions, states[:-1], states[1:], strict=True)
    )


@pytest.fixture
def driven(
    db: Database, campaign: Campaign, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Callable[[str, list[str], list[State]], Driver]]:
    """Replay one trace on a fresh row of the campaign's repo, with the real code's boundaries gated."""
    cas = disk(tmp_path / 'shared')
    record = Record(Path(campaign.path), cas)
    repo = RepoId(campaign.id)
    write = transfers_module.write_transaction
    clock: list[Driver] = []

    def gated(s: Session) -> None:
        if (actor := _current()) is not None:
            actor.pause('write')
        write(s)

    lost: set[Cid] = set()
    has = cas.has
    monkeypatch.setattr(cas, 'has', lambda cid: cid not in lost and has(cid))
    monkeypatch.setattr(transfers_module, 'write_transaction', gated)
    monkeypatch.setattr(transfers_module, 'utcnow', lambda: clock[-1].time())
    drivers: list[Driver] = []

    def replay(name: str, actions: list[str], states: list[State]) -> Driver:
        with db.session() as s:
            s.execute(delete(FirstTransfer))
            s.commit()
        lost.clear()
        driver = Driver(db, monkeypatch, repo, record, cas, utcnow(), lost)
        clock.append(driver)
        drivers.append(driver)
        monkeypatch.setattr(transfers_module, 'threading', SimpleNamespace(Lock=threading.Lock, Thread=driver.thread))
        try:
            driver.check('init', states[0])
            for n, (action, before, after) in enumerate(zip(actions, states[:-1], states[1:], strict=True)):
                try:
                    driver.step(action, before, after)
                    driver.check(action, after)
                except AssertionError as e:
                    raise AssertionError(f'{name}, step {n + 1} ({action}): {e}') from e
        finally:
            driver.end()
        return driver

    yield replay
    for driver in drivers:
        driver.end()


@pytest.mark.parametrize('graph', ['one', 'two', 'faults'])
def test_every_transition_of_the_model_is_one_of_the_first_transfers(
    graph: str, driven: Callable[[str, list[str], list[State]], Driver]
) -> None:
    """One request with each failure of a build and of its end and the deadline; two requests that ask at once; one
    request with a restart of the daemon and a blob that the CAS lost."""
    replayed = 0
    for name, actions, states in traces(TRACES / graph):
        if not fixed_only(actions, states):
            driven(name, actions, states)
            replayed += 1
    assert replayed > 0


def test_a_build_starts_only_on_a_row_that_its_start_found_requested(
    driven: Callable[[str, list[str], list[State]], Driver],
) -> None:
    """TLC's shortest trace: the due at the deadline runs the consumer while the build runs, the build writes its end
    between the consumer's read and its lock, and the consumer starts another build on the ended row. With the fix,
    that start starts nothing; so do the traces of the fixed model in which a start finds the row no longer requested.
    """
    name, actions, states = next(traces(TRACES / 'bugs'))
    assert name == 'unasked_build.itf.json'
    states[-1] = replace(states[-1], th=['idle', 'idle'], building=False)  # the fix: no build
    driven(name, actions, states)
    for graph in ('one', 'two', 'faults'):
        for name, actions, states in traces(TRACES / graph):
            if fixed_only(actions, states):
                driven(name, actions, states)


def test_a_build_whose_end_is_not_written_after_its_deadline_is_failed(
    driven: Callable[[str, list[str], list[State]], Driver], db: Database
) -> None:
    """TLC's trace of the model without the fix: the consumer's run at the deadline finds the build still running, and
    returns no due. The build then ends, and its end is not written. Before the fix no event and no due ran the
    consumer again: the row stayed requested, and every request failed. Now the unwritten end wakes the consumer, and
    the daemon's loop writes the row failed."""
    [(name, actions, states)] = [t for t in traces(TRACES / 'bugs') if t[0] == 'unwritten_end_never_failed.itf.json']
    driver = driven(name, actions, states)
    assert driver.due is not None, 'the unwritten end woke the consumer'
    loop = Loop(db, [driver.builds.consumer()], now=lambda: driver.now)
    with db.session() as s:
        loop._cursor = newest(s)  # every event so far was read: the trace's runs read them
    loop.wake(BUILDS)  # the build's wake reaches the daemon's loop so (`Host.wake`)
    for _ in range(3):
        driver.now += timedelta(seconds=DEADLINE)
        loop.turn()
    with db.session() as s:
        row = s.get_one(FirstTransfer, driver.repo)
    assert row.state is FirstTransferState.FAILED, f'the row is still {row.state}, and nothing builds it'
