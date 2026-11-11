"""Replay the state graph of the wake model (``tests/models/wake.tla``) against the real ``Leases``.

TLC wrote the graph of ``wake_fixed.cfg`` (``-dump dot,actionlabels``), and ``scripts/graph_traces.py`` turned it into
traces that cover every transition (``tests/traces/wake``). Each step runs the call that the model's action cites, on a
real database: a queue ``Leases.submit``, a wake the woken worker's ``messages`` then its ``acquire``, a turn an
``acquire``, a finish a ``report``, a silence a ``seen_at`` older than the silence. After each step the leases' rows,
and the worker that each ``LeaseQueued`` wakes, are the model's.
"""

from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from sqlalchemy import delete, select

from piloop.db import feed
from piloop.db.events import in_order
from piloop.db.models import Campaign, Lease, LeaseState, utcnow
from piloop.db.session import Database
from piloop.plugins.campaign_type.kit.evaluation.jobs import EVALUATION, EvaluationJob
from piloop.plugins.executor.kit.capacity import Pool
from piloop.plugins.executor.kit.leases import PAGE, Grant, LeaseQueued, Leases, cancel, topics
from piloop.plugins.executor.kit.tables import LeaseWorker, NoLease
from tests.conftest import home_cas
from tests.test_lease import HOST, MEASURED, TIMING, Recorder, evaluation_job_for_another_candidate, join
from tests.test_lease_model import State, _at, _steps

TRACES = Path(__file__).parent / 'traces' / 'wake'
POOLS = {'test': Pool(slots={EVALUATION: 1})}
"""One slot a worker, as the model's."""
STATES = {
    LeaseState.QUEUED: 'queued',
    LeaseState.ACQUIRED: 'live',
    LeaseState.RUNNING: 'live',
    LeaseState.REPORTED: 'done',
}


@dataclass
class Replay:
    """One trace: its two workers and its leases. The traces share a database; each ends its leases and workers."""

    db: Database
    leases: Leases
    jobs: dict[int, EvaluationJob]
    """The job of each lease of the model: a trace's lease of it is its newest, as the last trace's ended."""
    seq: int
    ids: dict[int, int] = field(default_factory=dict)
    """The real lease of each lease of the model."""
    starts: dict[str, int] = field(default_factory=dict)
    """Each worker's cursor at its registration: a read after it gets every message that it ever had."""

    def name(self, worker: str) -> str:
        return f'{worker}-{self.seq}'

    def start(self) -> None:
        for worker in ('w1', 'w2'):
            self.starts[worker] = join(self.leases, self.name(worker), ['process']).cursor

    def step(self, before: State, state: State) -> None:
        match state['arg']:
            case (str(worker), int(lease)):
                pass
            case other:
                raise AssertionError(f'not a step of the model: {other}')
        match state['op']:
            case 'queue':
                self.leases.submit(self.jobs[lease])
                with self.db.session() as s:
                    self.ids[lease] = s.scalars(select(Lease.id).order_by(Lease.id.desc()).limit(1)).one()
                target = _at(state, 'target', lease)
                assert self._woken(lease) == (None if target == '-' else self.name(str(target)))
                for other in {'w1', 'w2'} - {target}:
                    assert self._work(other, lease) is False, f'{other} is not woken'
            case 'wake':
                assert self._work(worker, lease), 'the woken worker reads its Work'
                self._acquire(worker, before, state)
            case 'turn':
                self._acquire(worker, before, state)
            case 'finish':
                self.leases.report(self.ids[lease], self.name(worker), uuid4().hex, MEASURED)
            case 'silence':
                with self.db.session() as s:
                    row = s.get_one(LeaseWorker, self.name(worker))
                    row.seen_at = utcnow() - self.leases.timing.silence - timedelta(seconds=1)
                    s.commit()
            case 'crash':
                pass  # the worker's process ends: nothing reaches the server
            case other:
                raise AssertionError(f'not a step of the model: {other}')
        self.check(state)

    def _queued(self, lease: int) -> tuple[int, LeaseQueued]:
        """The ``LeaseQueued`` of the model's ``lease``, and its event id."""
        with self.db.session() as s:
            [(event, queued)] = [(e.id, q) for e, q in in_order(s, LeaseQueued) if q.lease == self.ids[lease]]
        return event, queued

    def _woken(self, lease: int) -> str | None:
        return self._queued(lease)[1].worker

    def _work(self, worker: str, lease: int) -> bool:
        """Whether the messages of ``worker`` hold the ``Work`` of the model's ``lease``: the read of ``messages``."""
        event = self._queued(lease)[0]
        with self.db.session() as s:
            page = feed.after(s, self.starts[worker], topics(self.name(worker)), limit=PAGE)
        return event in page.ids

    def _acquire(self, worker: str, before: State, after: State) -> None:
        """An acquire: the grant of the lease that the model gave ``worker`` in this step, or no lease."""
        got = self.leases.acquire(self.name(worker), uuid4().hex, HOST)
        given = [m for m in self.ids if _at(before, 'st', m) == 'queued' and _at(after, 'st', m) == 'live']
        if given:
            assert isinstance(got, Grant) and got.lease == self.ids[given[0]], (got, given)
        else:
            assert isinstance(got, NoLease), got

    def check(self, state: State) -> None:
        """The leases' rows are the model's: state and holder."""
        with self.db.session() as s:
            for model, lease_id in self.ids.items():
                row = s.get_one(Lease, lease_id)
                assert STATES[row.state] == _at(state, 'st', model), (model, row.state)
                holder = _at(state, 'holder', model)
                assert row.worker == (None if holder == '-' else self.name(str(holder))), (model, row.worker, holder)

    def end(self) -> None:
        """End the trace's leases and forget its workers: a later trace must not find them."""
        with self.db.session() as s:
            for lease_id in self.ids.values():
                if (row := s.get_one(Lease, lease_id)).state in (LeaseState.QUEUED, LeaseState.ACQUIRED):
                    cancel(s, home_cas(self.db), row, 'the replay ended')
            s.execute(delete(LeaseWorker).where(LeaseWorker.id.in_([self.name('w1'), self.name('w2')])))
            s.commit()


def test_a_queued_lease_wakes_and_is_taken_as_the_model_on_every_transition_of_its_graph(
    db: Database, campaign: Campaign, tmp_path: Path
) -> None:
    leases = Leases(db, Recorder(db), home_cas(db), TIMING, POOLS)
    jobs = {
        lease: evaluation_job_for_another_candidate(db, campaign, tmp_path, tree=f'wake-{lease}') for lease in (1, 2)
    }
    paths = sorted(TRACES.glob('*.itf.json'))
    assert paths
    for seq, path in enumerate(paths):
        replay = Replay(db, leases, jobs, seq)
        replay.start()
        steps = _steps(path)
        for (_, before), (action, state) in zip(steps, steps[1:], strict=False):
            if action != 'Done':
                try:
                    replay.step(before, state)
                except AssertionError as e:
                    raise AssertionError(f'{path.name}, at {state["op"]}{state["arg"]}: {e}') from e
        replay.end()
