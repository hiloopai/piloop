"""Replay the state graph of the lease model (``tests/models/lease.tla``) against the real ``Leases``.

TLC wrote the graphs of ``lease_replay_*.cfg`` (``-dump dot,actionlabels``), and ``scripts/graph_traces.py`` turned
each into traces that cover every transition (``tests/traces/lease_*``). Each step runs the call of the code that the
model's action cites, on a real database, with the time moved by hand (a model tick is one renew, 10 s). The workers'
own state (what runs, the spool, the open request id) is the model's: only the calls that reach the server are real.
After each step the leases' rows must be the model's: their state, holder and time left. So TLC's verdicts on the
model hold for the code on these paths, the two bugs that it found included (the xfails below).
"""

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from piloop.db.models import Campaign, Lease, LeaseState
from piloop.db.session import Database
from piloop.plugins.campaign_type.kit.evaluation.jobs import EvaluationJob
from piloop.plugins.executor.contract import HarnessSession
from piloop.plugins.executor.kit.leases import Answer, Grant, Leases, ProcessHandle, StaleLease, Timing, cancel
from piloop.plugins.executor.kit.tables import LeaseWorker, NoLease
from tests.conftest import home_cas
from tests.test_lease import HOST, MEASURED, POOLS, Clock, Recorder, evaluation_job_for_another_candidate, join, queue

RENEW = 10
TOL, SLACK = 3, 1
"""``lease_replay_*.cfg``: toleration 30 s, slack 10 s, in renew periods."""
TIMING = Timing(renew=RENEW, grace=20, toleration=TOL * RENEW, slack=SLACK * RENEW)
TRACES = Path(__file__).parent / 'traces'
LIVE = (LeaseState.ACQUIRED, LeaseState.RUNNING)
STATES = {
    LeaseState.QUEUED: 'queued',
    LeaseState.ACQUIRED: 'live',
    LeaseState.RUNNING: 'live',
    LeaseState.EXPIRED: 'expired',
    LeaseState.REPORTED: 'reported',
}

type Value = int | str | bool | frozenset[Value] | tuple[Value, ...] | dict[str, Value]
type State = dict[str, Value]


def _value(itf: object) -> Value:
    """An ITF value as Python: ints, strings, booleans, sets, tuples and records."""
    match itf:
        case {'#bigint': str(number)}:
            return int(number)
        case {'#set': list(items)}:
            return frozenset(_value(item) for item in items)
        case dict():
            return {str(key): _value(item) for key, item in itf.items()}
        case list():
            return tuple(_value(item) for item in itf)
        case bool() | str():
            return itf
    raise TypeError(f'not an ITF value: {itf!r}')


def _steps(path: Path) -> list[tuple[str, State]]:
    """Each state of a trace with the action that led to it (``Init`` first). ``Done`` changes nothing."""
    trace = json.loads(path.read_text())
    steps = []
    for action, state in zip(['Init', *trace['#meta']['actions']], trace['states'], strict=True):
        record = _value(state['s'])
        assert isinstance(record, dict)
        steps.append((action, record))
    return steps


def _at(state: State, name: str, key: str | int) -> Value:
    """``state.name[key]``: a record's field (by worker) or a sequence's item (by lease, from 1)."""
    value = state[name]
    if isinstance(value, dict):
        return value[str(key)]
    assert isinstance(value, tuple) and isinstance(key, int)
    return value[key - 1]


@dataclass
class Replay:
    """The real leases of one trace, and what the model's workers know: the lease ids and the open request ids.

    The traces of a graph share a database: each has a job and workers of its own (``w1-<n>``), and ends its leases.
    """

    db: Database
    leases: Leases
    clock: Clock
    job: EvaluationJob
    seq: int
    ids: dict[int, int] = field(default_factory=dict)
    """The real lease of each lease of the model."""
    requests: dict[str, str] = field(default_factory=dict)
    """The request id of each worker's acquire that got no answer yet (``Worker._acquiring``)."""

    def step(self, before: State, after: State) -> None:
        match after['op'], after['arg']:
            case str(op), (str(worker), int(arg)):
                pass
            case other:
                raise AssertionError(f'not a step of the model: {other}')
        STEPS[op.removesuffix('_lost')](self, before, after, worker, arg)
        if worker != '-' and not _at(after, 'asking', worker):  # answered: the next acquire takes a new id
            self.requests.pop(worker, None)
        self.check(after)

    def check(self, state: State) -> None:
        """The rows of the leases are the model's: state, holder, and the renew periods left before expiry."""
        with self.db.session() as s:
            for model, lease_id in self.ids.items():
                row = s.get_one(Lease, lease_id)
                assert STATES[row.state] == _at(state, 'srv', model), (model, row.state)
                holder = _at(state, 'holder', model)
                assert row.worker == (None if holder == '-' else self.name(str(holder))), (model, row.worker, holder)
                if row.state in LIVE:
                    assert row.expires_at is not None
                    left = (row.expires_at - self.clock.now).total_seconds() / RENEW
                    assert max(left, 0) == _at(state, 'left', model), (model, left)

    def end(self) -> None:
        """End every lease of the trace that has not ended: the next trace's acquires must not find them."""
        with self.db.session() as s:
            for lease_id in self.ids.values():
                if (row := s.get_one(Lease, lease_id)).state in (LeaseState.QUEUED, *LIVE):
                    cancel(s, home_cas(self.db), row, 'the replay ended')
            s.commit()

    def name(self, worker: str) -> str:
        return f'{worker}-{self.seq}'

    def request(self, worker: str) -> str:
        return self.requests.setdefault(worker, uuid4().hex)

    def acquire(self, worker: str, expected: int) -> None:
        """Acquire with the open request id: the grant of ``expected`` (a lease of the model), or no lease (0)."""
        got = self.leases.acquire(self.name(worker), self.request(worker), HOST)
        if expected == 0:
            assert isinstance(got, NoLease), got
        else:
            assert isinstance(got, Grant) and got.lease == self.ids[expected], (got, expected)


def _init(replay: Replay, _before: State, _after: State, _worker: str, _arg: int) -> None:
    replay.ids[1] = queue(replay.db, replay.job)
    for worker in ('w1', 'w2'):
        join(replay.leases, replay.name(worker), ['process'])


def _acq_read(replay: Replay, before: State, after: State, worker: str, _arg: int) -> None:
    """The read answers (``Leases.acquire``'s read, or its write without a slack: the same answer), or goes on to
    the write, which ``_acq_write`` calls."""
    replay.request(worker)
    if _at(after, 'snap', worker) == 0:
        took = _at(before, 'took', worker)
        assert isinstance(took, int)
        replay.acquire(worker, took)


def _acq_write(replay: Replay, _before: State, _after: State, worker: str, lease: int) -> None:
    replay.acquire(worker, lease)


def _start(replay: Replay, _before: State, after: State, worker: str, lease: int) -> None:
    try:
        replay.leases.started(replay.ids[lease], replay.name(worker), ProcessHandle(pgid=1, start_time=None))
    except StaleLease:
        assert not _at(after, 'started', worker)
    else:
        assert _at(after, 'started', worker)


def _report(replay: Replay, _before: State, after: State, worker: str, lease: int) -> None:
    try:
        replay.leases.report(replay.ids[lease], replay.name(worker), uuid4().hex, MEASURED)
    except StaleLease:
        assert _at(after, 'srv', lease) != 'reported'
    else:
        assert _at(after, 'srv', lease) == 'reported'


def _held(state: State, worker: str) -> set[int]:
    spool, run = _at(state, 'spool', worker), _at(state, 'run', worker)
    assert isinstance(spool, frozenset) and isinstance(run, int)
    return {lease for lease in spool if isinstance(lease, int)} | ({run} - {0})


def _heartbeat(replay: Replay, before: State, _after: State, worker: str, due: int) -> None:
    """The model's ``forced`` is a seen_at older than the slack: the row gets the one that the model chose."""
    with replay.db.session() as s:
        row = s.get_one(LeaseWorker, replay.name(worker))
        row.seen_at = replay.clock.now - timedelta(seconds=SLACK * RENEW + 1 if due else 0)
        row.no_lease = None if row.no_lease == NoLease.UNREACHABLE else row.no_lease
        s.commit()
    held = _held(before, worker)
    sessions = {replay.ids[lease]: HarnessSession() for lease in held}
    answers = replay.leases.heartbeat(replay.name(worker), sessions, HOST)
    for lease in held:
        valid = _at(before, 'srv', lease) == 'live' and _at(before, 'holder', lease) == worker
        valid = valid and (lease == 2 or _at(before, 'srv', 2) == 'none')
        assert (answers[replay.ids[lease]] is Answer.RENEWED) == valid, (lease, answers)


def _expire(replay: Replay, _before: State, _after: State, _worker: str, _lease: int) -> None:
    replay.leases.expire(replay.clock.now)


def _requeue(replay: Replay, _before: State, _after: State, _worker: str, _lease: int) -> None:
    replay.ids[2] = queue(replay.db, replay.job)  # as core submits a lost job again


def _tick(replay: Replay, _before: State, _after: State, _worker: str, _arg: int) -> None:
    replay.clock.after(RENEW)


def _crash(replay: Replay, _before: State, _after: State, worker: str, _arg: int) -> None:
    replay.requests.pop(worker, None)  # its open request id was in its memory


def _restart(replay: Replay, before: State, _after: State, worker: str, _arg: int) -> None:
    held = {grant.lease for grant in join(replay.leases, replay.name(worker), ['process']).held}
    live = {lease for lease in (1, 2) if lease in replay.ids and _at(before, 'srv', lease) == 'live'}
    assert held == {replay.ids[lease] for lease in live if _at(before, 'holder', lease) == worker}


def _worker_only(_replay: Replay, _before: State, _after: State, _worker: str, _arg: int) -> None:
    """A step of the worker alone, a job that ends or a self-stop: nothing reaches the server."""


STEPS: dict[str, Callable[[Replay, State, State, str, int], None]] = {
    'init': _init,
    'acq_read': _acq_read,
    'acq_write': _acq_write,
    'start': _start,
    'finish': _worker_only,
    'self_stop': _worker_only,
    'report': _report,
    'heartbeat': _heartbeat,
    'expire': _expire,
    'requeue': _requeue,
    'tick': _tick,
    'crash': _crash,
    'restart': _restart,
}


GRAPHS = sorted(path.name for path in TRACES.glob('lease_*'))


@pytest.mark.parametrize('graph', GRAPHS)
def test_the_leases_move_as_the_model_on_every_transition_of_its_graph(
    db: Database, campaign: Campaign, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, graph: str
) -> None:
    leases, clock = Leases(db, Recorder(db), home_cas(db), TIMING, POOLS), Clock(monkeypatch)
    for seq, path in enumerate(sorted((TRACES / graph).glob('*.itf.json'))):
        job = evaluation_job_for_another_candidate(db, campaign, tmp_path, tree=f'{graph}-{seq}')
        replay = Replay(db, leases, clock, job, seq)
        steps = _steps(path)
        replay.step(steps[0][1], steps[0][1])
        for (_, before), (action, after) in zip(steps, steps[1:], strict=False):
            if action != 'Done':
                try:
                    replay.step(before, after)
                except AssertionError as e:
                    raise AssertionError(f'{path.name}, at {action}: {e}') from e
        replay.end()


def _running(db: Database, campaign: Campaign, tmp_path: Path, leases: Leases, request: str) -> int:
    """A lease that ``w1`` acquired with ``request`` and started: the id of the lease."""
    lease_id = queue(db, evaluation_job_for_another_candidate(db, campaign, tmp_path, tree='bug'))
    join(leases, 'w1', ['process'])
    grant = leases.acquire('w1', request, HOST)
    assert isinstance(grant, Grant) and grant.lease == lease_id
    return lease_id


def test_a_lease_that_an_acquire_sent_again_gets_does_not_expire_under_its_job(
    db: Database, campaign: Campaign, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TLC on lease_current.cfg: ``acq_write_lost(w1, 1), tick x4, acq_read(w1), expire(1)``. The answer of a grant is
    lost; the worker sends the acquire again with its request id once the lease is due to expire, and the lease of the
    request id comes back as it is. The worker starts the job, and the daemon expires the lease under it."""
    timing = Timing(renew=10, grace=50, toleration=300, slack=60)
    clock, leases = Clock(monkeypatch), Leases(db, Recorder(db), home_cas(db), timing, POOLS)
    request = uuid4().hex
    lease_id = _running(db, campaign, tmp_path, leases, request)  # its answer lost: the worker knows nothing of it
    clock.after(299)
    resent = leases.acquire('w1', request, HOST)
    assert isinstance(resent, Grant) and resent.lease == lease_id
    leases.started(lease_id, 'w1', ProcessHandle(pgid=1, start_time=None))
    leases.expire(clock.after(1))  # before the worker's next heartbeat, a renew later
    with db.session() as s:
        assert s.get_one(Lease, lease_id).state in LIVE, 'expired under the job that its worker just started'


def test_a_lease_that_a_restarted_worker_recovers_does_not_expire_before_the_worker_would_stop_its_job(
    db: Database, campaign: Campaign, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TLC on lease_current.cfg with the resend fixed: ``acq_write(w1, 1), start(w1, 1), tick x2, crash(w1), tick x2,
    restart(w1), expire(1)``. A worker counts its silence from its start (``Worker.start``), and register renews
    nothing. So a worker that restarts after renews were missed, and then hears nothing more, stops its job only
    ``self_stop`` after its start: later than the lease's expiry, which counts from the last renewal."""
    timing = Timing(renew=10, grace=50, toleration=300, slack=60)
    clock, leases = Clock(monkeypatch), Leases(db, Recorder(db), home_cas(db), timing, POOLS)
    lease_id = _running(db, campaign, tmp_path, leases, uuid4().hex)
    leases.started(lease_id, 'w1', ProcessHandle(pgid=1, start_time=None))
    restart = clock.after(120)  # heartbeats missed for two slacks, then the worker process dies and starts again
    held = join(leases, 'w1', ['process']).held
    assert [grant.lease for grant in held] == [lease_id], 'it recovers the job'
    with db.session() as s:
        expires = s.get_one(Lease, lease_id).expires_at
    assert expires is not None
    assert expires > restart + timedelta(seconds=timing.self_stop), 'expires while the worker still runs the job'
