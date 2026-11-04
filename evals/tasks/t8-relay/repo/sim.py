"""The deterministic simulator: it runs a relay's processes against the store and the sink, under seeded faults.

Time is in ticks. Each relay process (``workers`` workers, then one compactor) is a coroutine; each call it awaits
completes after a latency, and is applied then, atomically. Calls that complete on the same tick go in an order the
seed draws. Everything else the seed draws too, from streams of its own, so a relay that makes the same calls sees the
same schedule:

- the producers' transactions: when each starts, its keys, when it commits or aborts (the workload);
- each process's crashes, the relay-wide outages, and each process's pauses (the faults, by tick);
- the sink's answer to the n-th attempt that reaches it for an event.

A crash ends the process: its memory is gone, and the call in flight is applied or not, on a coin. The process
restarts after ``RESTART`` ticks, as a fresh copy of the relay's modules, with nothing but the store to restore from.
A pause freezes the process: the calls it has in flight complete when the pause is over. Every call takes a tick or
more, so time always moves. The run ends when the
producers are done and the outbox is empty (every committed event compacted), or at ``cap`` ticks. ``CONTRACT.md``
states the rates; ``Split`` holds one split's sizes and rates.
"""

import builtins
import contextlib
import heapq
import random
import types
from collections.abc import Callable, Coroutine
from typing import NamedTuple

from store import (
    Attempt,
    Call,
    Client,
    Commit,
    Compaction,
    Fenced,
    Lease,
    Save,
    Sink,
    SinkFailure,
    SinkTimeout,
    Store,
    encode,
)

STORE_LATENCY = (1, 2)
"""Ticks from issuing a store or clock call to its completion: drawn in this range."""
SINK_LATENCY = (2, 6)
TIMEOUT_AFTER = 12
"""Ticks after it reaches the sink that a timed-out delivery raises."""
BURST = 20
"""Ticks over which the transactions of one burst start: the producers write in 3 to 6 bursts over the span."""
RESTART = (5, 30)
"""Ticks from a crash to the restart of the process."""
WRITES = frozenset({'save', 'deliver', 'compact', 'acquire', 'renew', 'release'})
"""The calls that a crash may still apply."""

type Entry = Callable[[Client], Coroutine[Call[object], object, object]]


class Rates(NamedTuple):
    """The fault rates of a run: ``CONTRACT.md`` gives the highest that a relay must survive."""

    crash: float
    """Crashes of each process, per 1000 ticks."""
    outage: float
    """Crashes of every process at once, per 1000 ticks."""
    pause: float
    """Pauses of each process, per 1000 ticks."""
    pause_ticks: int
    """The longest pause; each is drawn from 20 to this."""
    failure: float
    """The chance that an attempt fails."""
    timeout: float
    """The chance that an attempt times out; half of these apply the effect."""
    poison: float
    """The share of events whose every attempt fails."""
    abort: float
    """The share of transactions that abort."""
    commit_delay: int
    """The longest delay from a transaction's last insert to its commit or abort."""


SCENARIOS = {
    'crash': ('crash', 'outage', 'failure', 'poison'),
    'pause': ('pause',),
    'sink': ('failure', 'timeout', 'poison'),
    'mixed': ('crash', 'outage', 'pause', 'failure', 'timeout', 'poison'),
}
"""The faults each scenario turns on, at the split's rates. Aborts and out-of-order commits are always on."""


class Split(NamedTuple):
    """A set of schedules: sizes, rates, seeds and scenarios."""

    workers: int
    keys: int
    events: int
    span: int
    """Ticks over which the transactions start."""
    cap: int
    rates: Rates
    seeds: list[int]
    scenarios: list[str]

    def rates_of(self, scenario: str) -> Rates:
        """The rates of ``scenario``: the split's, with the faults it does not turn on at 0."""
        on = SCENARIOS[scenario]
        kept = {k: v for k, v in self.rates._asdict().items() if k in on or k in ('abort', 'commit_delay')}
        return self.rates._replace(**{k: 0 for k in self.rates._fields if k not in kept})


class History(NamedTuple):
    """What the store and the sink recorded in one run."""

    commits: list[Commit]
    attempts: list[Attempt]
    saves: list[Save]
    compactions: list[Compaction]
    poison: frozenset[int]
    keys: int


class Result(NamedTuple):
    """One run: its history, the tick it drained at (``cap`` if it did not), and its first relay error."""

    history: History
    drain: int
    drained: bool
    calls: int
    error: str


class Program:
    """A relay's modules, compiled once; each process gets fresh copies, so no memory survives a crash."""

    def __init__(self, sources: dict[str, str]) -> None:
        self.code = {name: compile(text, f'{name}.py', 'exec') for name, text in sources.items()}

    def load(self) -> types.ModuleType:
        """A fresh copy of module ``relay``, whose imports of the other modules are fresh copies too."""
        modules: dict[str, types.ModuleType] = {}
        real = builtins.__import__

        def importer(name: str, globs: object = None, locs: object = None, fromlist: object = (), level: int = 0):
            if level == 0 and name in self.code:
                return module(name)
            return real(name, globs, locs, fromlist, level)  # ty: ignore[invalid-argument-type]

        env = {**vars(builtins), '__import__': importer}

        def module(name: str) -> types.ModuleType:
            if name not in modules:
                m = types.ModuleType(name)
                m.__dict__['__builtins__'] = env
                modules[name] = m
                exec(self.code[name], m.__dict__)
            return modules[name]

        return module('relay')


class Proc:
    """One incarnation of a process."""

    __slots__ = ('coro', 'id', 'pending', 'rng', 'since', 'slot', 'token')

    def __init__(self, pid: int, slot: int, coro: Coroutine[Call[object], object, object], rng: random.Random) -> None:
        self.id = pid
        self.slot = slot
        self.coro = coro
        self.rng = rng
        self.pending: Call[object] | None = None
        self.token = 0
        self.since: int | None = None


class World:
    """One run of a relay on one schedule."""

    def __init__(self, program: Program, split: Split, seed: int, scenario: str) -> None:
        self.program = program
        self.split = split
        self.rates = split.rates_of(scenario)
        self.name = f'{seed}/{scenario}'
        self.order = random.Random(f'{self.name}/order')
        self.faults = random.Random(f'{self.name}/faults')
        self.store = Store()
        self.sink = Sink()
        self.tick = 0
        self.seq = 0
        self.heap: list[tuple] = []
        self.pushed = 0
        self.procs: dict[int, Proc | None] = {}
        self.incarnations: dict[int, int] = {}
        self.paused: dict[int, int] = {}
        self.waiting: list[Proc] = []
        self.arrivals: dict[int, int] = {}
        self.calls = 0
        self.error = ''
        self.txns = 0
        self.ended = 0
        self.poison: set[int] = set()
        self.committed: dict[int, int] = {}
        """Every event that committed, and its key."""
        self.pids = 0

    def push(self, tick: int, kind: str, a: object = None, b: object = None) -> None:
        self.pushed += 1
        heapq.heappush(self.heap, (tick, self.order.random(), self.pushed, kind, a, b))

    def workload(self) -> None:
        """Schedule the producers' transactions."""
        rng = random.Random(f'{self.name}/workload')
        bursts = [rng.randrange(self.split.span) for _ in range(rng.randint(3, 6))]
        left, txn = self.split.events, 0
        while left > 0:
            size = min(left, rng.choice((1, 1, 2, 3)))
            left -= size
            start = rng.choice(bursts) + rng.randrange(BURST)
            for i in range(size):
                self.push(start + i, 'insert', txn, rng.randrange(self.split.keys))
            end = start + size + rng.randint(1, self.rates.commit_delay)
            self.push(end, 'abort' if rng.random() < self.rates.abort else 'commit', txn)
            txn += 1
        self.txns = txn

    def schedule_faults(self) -> None:
        """Schedule every crash, outage and pause up to the cap."""
        slots = self.split.workers + 1
        for kind, rate in (('crash', self.rates.crash), ('pause', self.rates.pause)):
            for slot in range(slots):
                for tick in self.poisson(rate):
                    self.push(tick, kind, slot, self.faults.randint(20, max(20, self.rates.pause_ticks)))
        for tick in self.poisson(self.rates.outage):
            for slot in range(slots):
                self.push(tick, 'crash', slot)

    def poisson(self, per_1000: float) -> list[int]:
        out, tick = [], 0.0
        while per_1000 > 0:
            tick += self.faults.expovariate(per_1000 / 1000)
            if tick >= self.split.cap:
                return out
            out.append(int(tick))
        return out

    def start(self, slot: int) -> None:
        """Start a new incarnation of ``slot``."""
        n = self.incarnations.get(slot, 0)
        self.incarnations[slot] = n + 1
        relay = self.program.load()
        ctx = Client(slot, self.split.workers, range(self.split.keys))
        entry: Entry = relay.compactor if slot == self.split.workers else relay.worker
        self.pids += 1
        proc = Proc(self.pids, slot, entry(ctx), random.Random(f'{self.name}/latency/{slot}/{n}'))
        self.procs[slot] = proc
        self.resume(proc, None, None)

    def resume(self, proc: Proc, value: object, error: BaseException | None) -> None:
        """Run ``proc`` up to its next call, and schedule that call's completion."""
        proc.token += 1
        proc.pending = None
        self.calls += 1
        try:
            call = proc.coro.throw(error) if error is not None else proc.coro.send(value)
            if not isinstance(call, Call):
                raise TypeError(f'a relay process awaited {call!r}, not a call of its Client')
        except StopIteration:
            self.exit(proc, f'process {proc.slot} returned')
            return
        except Exception as e:  # the relay's own error ends the process, as an exception would end a real one
            self.exit(proc, f'process {proc.slot}: {type(e).__name__}: {e}')
            return
        proc.pending = call
        if call.kind == 'deliver':
            latency = proc.rng.randint(*SINK_LATENCY)
        elif call.kind == 'sleep':
            latency = max(1, call.args[0]) if isinstance(call.args[0], int) else 1
        else:
            latency = proc.rng.randint(*STORE_LATENCY)
        self.push(self.tick + latency, 'step', proc, proc.token)

    def exit(self, proc: Proc, why: str) -> None:
        if not self.error:
            self.error = f'tick {self.tick}: {why}'
        self.crash(proc.slot, apply_pending=False)

    def crash(self, slot: int, apply_pending: bool = True) -> None:
        proc = self.procs.get(slot)
        if proc is None:
            return
        self.procs[slot] = None
        if proc in self.waiting:
            self.waiting.remove(proc)
        call = proc.pending
        if apply_pending and call is not None and call.kind in WRITES and self.faults.random() < 0.5:
            with contextlib.suppress(TypeError, ValueError):  # a call the relay got wrong: refused, as if it lived
                self.apply(proc, call)
        with contextlib.suppress(RuntimeError):  # the relay awaited in a `finally`: a crashed process runs no more
            proc.coro.close()
        self.push(self.tick + self.faults.randint(*RESTART), 'restart', slot)

    def step(self, proc: Proc, token: int, kind: str) -> None:
        """Complete the call of ``proc``, wake it from a wait, or raise its delivery's timeout."""
        if self.procs.get(proc.slot) is not proc or proc.token != token:
            return
        if self.paused.get(proc.slot, 0) > self.tick:
            self.push(self.paused[proc.slot], kind, proc, token)
            return
        call = proc.pending
        assert call is not None
        if kind == 'raise':  # a timed-out delivery, raising now
            self.resume(proc, None, call.args[0])  # ty: ignore[invalid-argument-type]
            return
        if kind == 'wake':
            self.waiting.remove(proc)
            self.resume(proc, self.store.version, None)
            return
        if call.kind == 'wait':
            self.wait(proc, call)
            return
        try:
            value, error, later = self.apply(proc, call)
        except (TypeError, ValueError) as e:
            value, error, later = None, ValueError(f'{call.kind}: {e}'), 0
        if later:
            proc.pending = Call('timeout', error)
            self.push(self.tick + later, 'raise', proc, proc.token)
            return
        self.resume(proc, value, error)

    def wait(self, proc: Proc, call: Call[object]) -> None:
        since, timeout = call.args
        since = self.store.version if since is None else since
        if not isinstance(since, int) or not isinstance(timeout, int):
            self.resume(proc, None, ValueError('wait: since and timeout are ints'))
        elif self.store.version > since:
            self.resume(proc, self.store.version, None)
        else:
            proc.since = since
            self.waiting.append(proc)
            self.push(self.tick + max(1, timeout), 'wake', proc, proc.token)

    def apply(self, proc: Proc, call: Call[object]) -> tuple[object, BaseException | None, int]:
        """Apply ``call`` for ``proc`` now: its value, the exception it raises, and the ticks before it raises."""
        self.seq += 1
        store, tick, args = self.store, self.tick, call.args
        match call.kind:
            case 'now' | 'sleep':
                return (tick if call.kind == 'now' else None), None, 0
            case 'changes':
                return store.version, None, 0
            case 'horizon':
                return store.horizon(), None, 0
            case 'read':
                key, after, limit = (self.key(args[0]), self.as_int(args[1]), self.as_int(args[2]))
                return store.read(key, after, limit), None, 0
            case 'compact':
                store.compact(self.as_int(args[0]), self.seq, tick)
                return None, None, 0
            case 'acquire':
                return store.acquire(proc.id, self.name_of(args[0]), self.as_int(args[1]), tick), None, 0
            case 'renew':
                return store.renew(proc.id, self.lease(args[0]), self.as_int(args[1]), tick), None, 0
            case 'release':
                store.release(proc.id, self.lease(args[0]), tick)
                return None, None, 0
            case 'load':
                return store.load(self.key(args[0])), None, 0
            case 'save':
                return self.save(proc, *args)
            case 'deliver':
                return self.deliver(proc, *args)
        raise TypeError(f'no such call: {call.kind}')

    def save(self, proc: Proc, key: object, cursor: object, state: object, dead: object, fence: object) -> tuple:
        key, cursor, text = self.key(key), self.as_int(cursor), encode(state)
        ids = [self.as_int(e) for e in dead]  # ty: ignore[not-iterable]
        if any(self.committed.get(e) != key for e in ids):
            raise ValueError(f'dead letters must be committed events of key {key}: {ids}')
        if fence is not None and not self.store.holds(proc.id, key, self.tick, self.lease(fence)):
            return None, Fenced(f'save of key {key}'), 0
        self.store.save(proc.id, key, cursor, text, tuple(ids), self.seq, self.tick)
        return None, None, 0

    def deliver(self, proc: Proc, event: object, number: object, idem: object, fence: object) -> tuple:
        eid, number = self.as_int(event), self.as_int(number)
        if eid not in self.committed:
            raise ValueError(f'event {eid} was never committed')
        if idem is not None and not isinstance(idem, str):
            raise TypeError('idem is a str or None')
        key = self.committed[eid]
        if fence is not None and not self.store.holds(proc.id, key, self.tick, self.lease(fence)):
            return None, Fenced(f'delivery of event {eid}'), 0
        n = self.arrivals.get(eid, 0)
        self.arrivals[eid] = n + 1
        draw = random.Random(f'{self.name}/sink/{eid}/{n}')
        u, applied = draw.random(), draw.random() < 0.5
        r = self.rates
        outcome = 'failure' if eid in self.poison or u < r.failure else 'timeout' if u < r.failure + r.timeout else 'ok'
        holder = self.store.holds(proc.id, key, self.tick)
        answer = self.sink.deliver(eid, number, idem, holder, outcome, applied, self.seq, self.tick)
        if answer == 'failure':
            return None, SinkFailure(f'event {eid}'), 0
        if answer == 'timeout':
            return None, SinkTimeout(f'event {eid}'), TIMEOUT_AFTER
        return answer, None, 0

    def key(self, value: object) -> int:
        if not isinstance(value, int) or not 0 <= value < self.split.keys:
            raise ValueError(f'no such key: {value!r}')
        return value

    def as_int(self, value: object) -> int:
        if not isinstance(value, int):
            raise TypeError(f'expected an int, not {value!r}')
        return value

    def name_of(self, value: object) -> int | str:
        if not isinstance(value, int | str):
            raise TypeError(f'a lease name is an int or a str, not {value!r}')
        return value

    def lease(self, value: object) -> Lease:
        if not isinstance(value, Lease):
            raise TypeError(f'expected a Lease, not {value!r}')
        return value

    def produce(self, kind: str, txn: int, key: int | None) -> None:
        if kind == 'insert':
            assert key is not None
            eid = self.store.insert(txn, key)
            if random.Random(f'{self.name}/poison/{eid}').random() < self.rates.poison:
                self.poison.add(eid)
            return
        self.seq += 1
        if kind == 'commit':
            for c in self.store.commit(txn, self.seq, self.tick):
                self.committed[c.id] = c.key
        else:
            self.store.abort(txn)
        self.ended += 1

    def drained(self) -> bool:
        return self.ended == self.txns and not any(self.store.outbox.values())

    def run(self) -> Result:
        """Run to the drain or the cap."""
        self.workload()
        self.schedule_faults()
        for slot in range(self.split.workers + 1):
            self.push(0, 'restart', slot)
        version = 0
        drained = False
        while self.heap:
            tick, _, _, kind, a, b = heapq.heappop(self.heap)
            if tick >= self.split.cap:
                break
            self.tick = tick
            match kind:
                case 'insert' | 'commit' | 'abort':
                    self.produce(kind, a, b)
                case 'step' | 'wake' | 'raise':
                    self.step(a, b, kind)
                case 'crash':
                    self.crash(a)
                case 'pause':
                    self.paused[a] = max(self.paused.get(a, 0), tick + b)
                case 'restart':
                    if self.procs.get(a) is None:
                        self.start(a)
            if self.store.version != version:
                version = self.store.version
                for proc in [p for p in self.waiting if p.since is not None and p.since < version]:
                    self.push(tick + 1, 'wake', proc, proc.token)
                    proc.since = None
            if kind in ('step', 'commit', 'abort') and self.drained():
                drained = True
                break
        s = self.store
        history = History(
            s.commits, self.sink.attempts, s.saves, s.compactions, frozenset(self.poison), self.split.keys
        )
        return Result(history, self.tick if drained else self.split.cap, drained, self.calls, self.error)
