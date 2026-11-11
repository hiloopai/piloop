"""The lease operations: acquire, heartbeat, report, the fence, and the two stages of a silent worker."""

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import partial
from pathlib import Path
from uuid import uuid4

import pytest
from covalence import Cid
from pytest_mock import MockerFixture
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from piloop.actor import System
from piloop.agents.results import PromptOutcome
from piloop.db import feed
from piloop.db.events import emit, has, in_order
from piloop.db.machine import Refused, transition
from piloop.db.models import (
    LIVE_LEASES,
    PROCESS,
    AgentSession,
    Campaign,
    Comment,
    Event,
    EventModel,
    Lease,
    LeaseState,
    utcnow,
)
from piloop.db.session import Database, write_transaction
from piloop.ids import CommentId, TargetKind, TargetRef
from piloop.plugins.campaign_type.kit.candidates.ids import CANDIDATE
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.evaluation.evaluate import Score
from piloop.plugins.campaign_type.kit.evaluation.jobs import EVALUATION, EvaluationJob, EvaluationReport, Sample
from piloop.plugins.campaign_type.kit.evaluation.tables import Evaluation, Evaluator
from piloop.plugins.campaign_type.kit.runs.ids import IMPLEMENTER
from piloop.plugins.executor.contract import (
    JOB,
    AgentFinished,
    AgentJob,
    AgentKey,
    AgentNotStarted,
    Capacity,
    Guard,
    HarnessSession,
    Job,
    JobKey,
    Key,
    Prompt,
    RepoId,
    Report,
    Settled,
    Source,
    key_of,
    kind_of,
)
from piloop.plugins.executor.kit import leases as leases_module
from piloop.plugins.executor.kit.capacity import GB, HostSample, Pool
from piloop.plugins.executor.kit.client import Local, Registration, WorkerInfo
from piloop.plugins.executor.kit.leases import (
    LEASE_STATES,
    NOT_TAKEN,
    SETTLED,
    AgentHandle,
    Answer,
    Grant,
    LeaseAcquired,
    LeaseCancelled,
    LeaseExpired,
    LeasePrompted,
    LeaseQueued,
    LeaseReported,
    LeaseRevoked,
    Leases,
    LeaseStarted,
    LeaseUnreachable,
    LeaseWorkerJoined,
    LeaseWorkerRelieved,
    PromptMessage,
    StaleLease,
    StaleReport,
    Timing,
    Work,
    cancel,
    enqueue,
    read_job,
    revoke,
)
from piloop.plugins.executor.kit.tables import LeaseWorker, NoLease
from piloop.plugins.executor.workers import Workers
from piloop.plugins.metric import Direction
from piloop.plugins.runtime.contract import AgentExit, AgentStart, ExitStatus
from piloop.plugins.vcs.git import Git
from tests.conftest import baseline, home_cas, wait_until

HANDLE = AgentHandle(runtime='subprocess', data={})
TIMING = Timing(renew=10, grace=50, toleration=300)
POOLS = {'test': Pool(slots={EVALUATION: 99})}
"""A pool whose slots no test fills, unless it says so."""
HOST = HostSample(at=utcnow(), cpus=4, load1=0.5, memory_available=8 * GB, disk_free=100 * GB)
PROBE = Cid.of(b'piloop worker cas probe\n')
"""A worker's ``cas_probe``: the CAS of a server that checks it holds these bytes."""


def join(leases: Leases, worker: str, runners: list[str], pool: str = 'test') -> Registration:
    """Register ``worker`` in ``pool`` with ``runners``, as its ``LeaseClient`` does."""
    return Local(leases, worker).register(
        WorkerInfo(version='0.2.0', pool=pool, runners=runners, sample=HOST, cas_probe=PROBE)
    )


def take(leases: Leases, worker: str, runners: list[str], pool: str = 'test') -> Grant | None:
    """Register ``worker`` in ``pool`` with ``runners``, then acquire: the grant, or None when it gets no lease."""
    join(leases, worker, runners, pool)
    grant = Local(leases, worker).acquire(uuid4().hex, HOST)
    return grant if isinstance(grant, Grant) else None


@dataclass
class Recorder:
    """A ``Results`` that records what the leases asked of core, after their guard, in a transaction as core's."""

    db: Database
    calls: list[tuple[str, int, object]] = field(default_factory=list)

    def _apply(self, guard: Guard | None, call: tuple[str, int, object], change: Callable[[Session], None]) -> None:
        with self.db.session() as s:
            write_transaction(s)
            if guard is not None:
                guard(s)
            change(s)
            s.commit()
        self.calls.append(call)

    def started(
        self,
        key: AgentKey,
        handle: dict[str, object],
        session_id: str | None,
        *,
        machine: str | None = None,
        guard: Guard | None = None,
    ) -> None:
        self._apply(guard, ('started', key.agent_session, handle), lambda s: None)

    def session(self, key: AgentKey, session: HarnessSession, *, guard: Guard | None = None) -> None:
        self._apply(guard, ('session', key.agent_session, session.id), lambda s: None)

    def finished(self, job: Job, report: Report, *, guard: Guard | None = None) -> None:
        target = job.agent_session if isinstance(job, AgentJob) else kind_of(job).id(job)
        self._apply(guard, ('finished', target, report), lambda s: None)

    def lost(self, key: Key, reason: str, *, guard: Guard | None = None) -> None:
        def change(s: Session) -> None:
            if isinstance(key, JobKey):  # as core does: the evaluation waits for a slot again
                s.get_one(Evaluation, key.id).started_at = None

        target = key.id if isinstance(key, JobKey) else key.agent_session
        self._apply(guard, ('lost', target, reason), change)

    def queued(self, key: AgentKey, prompt: CommentId, error: str | None) -> None:
        """Record the outcome once, as core does: an event that the leases read."""
        with self.db.session() as s:
            if s.scalar(select(Event.id).where(has(PromptOutcome, comment=prompt))) is None:
                settled = PromptOutcome(kind='comment.queued', comment=prompt)
                emit(s, settled, TargetRef(TargetKind.AGENT_SESSION, key.agent_session), System.DAEMON)
                s.commit()
                self.calls.append(('queued', key.agent_session, (prompt, error)))


@pytest.fixture
def recorder(db: Database) -> Recorder:
    return Recorder(db)


@pytest.fixture
def leases(db: Database, recorder: Recorder) -> Leases:
    return Leases(db, recorder, home_cas(db), TIMING, POOLS)


BRIEF = Cid.of(b'# brief')
"""The brief of a job that the test never starts, or starts with a spec of its own."""
START = AgentStart(executable='true', args=[])
"""The start of a job that the test never starts, or starts with a spec of its own."""


def agent_job(db: Database, campaign: Campaign, tmp_path: Path) -> AgentJob:
    with db.session() as s:
        w = AgentSession(kind=IMPLEMENTER, campaign_id=campaign.id)
        s.add(w)
        s.commit()
        agent_session_id = w.id
    return AgentJob(
        agent_session=agent_session_id,
        runtime='subprocess',
        artifact=Source(vcs=Git(), repo=RepoId(campaign.id), revision=baseline(db, campaign.id).revision),
        place='logs',
        timeout=60,
        brief_cid=BRIEF,
        spec=START,
    )


def evaluation_job(db: Database, campaign: Campaign, tmp_path: Path, runner: str = 'process') -> EvaluationJob:
    """An evaluation of the baseline, under the campaign's evaluator, or under one of its own for another runner."""
    base = baseline(db, campaign.id)
    with db.session() as s:
        evaluator_id = s.get_one(Campaign, campaign.id).evaluator_id
        if runner != PROCESS:
            evaluator = Evaluator(campaign_id=campaign.id, key=runner, definition={}, runner=runner)
            s.add(evaluator)
            s.flush()
            evaluator_id = evaluator.id
        evaluation = Evaluation(candidate_id=base.id, evaluator_id=evaluator_id)
        s.add(evaluation)
        s.commit()
        evaluation_id = evaluation.id
    return EvaluationJob(
        evaluation=evaluation_id,
        candidate=base.id,
        runner=runner,
        artifact=Source(vcs=Git(), repo=RepoId(campaign.id), revision=base.revision),
        place='eval',
        command=['true'],
        timeout=60,
        score=Score(name='score', direction=Direction.HIGHER),
        required=['score'],
        known=['score'],
        max_evidence_bytes=0,
    )


def queue(db: Database, job: Job, wake: str | None = None) -> int:
    """Queue ``job`` on a new lease whose message wakes ``wake``: a test that needs no wake lets the turns take it."""
    with db.session() as s:
        lease = enqueue(s, home_cas(db), job, wake)
        s.commit()
        return lease.id


def granted(db: Database, grant: Grant) -> Job:
    """The job of ``grant``, as its worker reads it from the home's CAS."""
    return read_job(home_cas(db), grant.lease, grant.job_cid)


def worker_row(db: Database, worker: str) -> LeaseWorker:
    with db.session() as s:
        return s.get_one(LeaseWorker, worker)


def lease_row(db: Database, lease_id: int) -> Lease:
    with db.session() as s:
        return s.get_one(Lease, lease_id)


def events[M: EventModel](db: Database, model: type[M]) -> list[tuple[TargetKind, int, M]]:
    with db.session() as s:
        return [(e.target_kind, e.target_id, payload) for e, payload in in_order(s, model)]


DONE = AgentFinished(process=AgentExit(ExitStatus.EXITED, 1.0, 0))
RECEIPT = {'reported': 'agent_finished'}
MEASURED = EvaluationReport(samples=[Sample(metrics={'score': 2.0}, seconds=1.0)])


def test_the_timing_is_kubernetes_two_stages_and_the_parameters_name_it_and_refuse_a_disorder() -> None:
    """Renewed every 10 s (the kubelet's node Lease), unreachable after 50 s (``--node-monitor-grace-period`` since
    Kubernetes 1.32), expired after 300 s (the unreachable taint's ``tolerationSeconds``). The worker stops two
    heartbeats before."""
    defaults = Workers()
    assert (defaults.renew_seconds, defaults.grace_seconds, defaults.toleration_seconds) == (10, 50, 300)
    assert Timing() == Timing(renew=10, grace=50, toleration=300) and Timing().self_stop == 280
    with pytest.raises(ValueError, match='renew_seconds < grace_seconds'):
        Workers(grace_seconds=400)


def test_a_lease_names_its_job_by_address_and_a_job_queued_again_is_one_blob(
    db: Database, campaign: Campaign, tmp_path: Path
) -> None:
    """The row stays small: the job is a blob of the CAS, stored once however often it is queued."""
    job = evaluation_job(db, campaign, tmp_path)
    first, again = lease_row(db, queue(db, job)), lease_row(db, queue(db, job))
    assert first.job_cid == again.job_cid and JOB.validate_json(home_cas(db).get(first.job_cid) or b'') == job


def test_a_lease_is_acquired_started_renewed_and_reported_once_with_its_receipt(
    db: Database, campaign: Campaign, tmp_path: Path, leases: Leases, recorder: Recorder
) -> None:
    job = agent_job(db, campaign, tmp_path)
    lease_id = queue(db, job)
    grant = take(leases, 'w1', ['process'])
    assert grant is not None and (grant.lease, granted(db, grant)) == (lease_id, job)
    assert take(leases, 'w2', ['process']) is None, 'a job goes to one worker'
    handle = AgentHandle(runtime='subprocess', data={'pid': 7})
    Local(leases, 'w1').started(lease_id, handle, None)
    assert Local(leases, 'w1').heartbeat({lease_id: HarnessSession(id='sid')}, HOST) == {lease_id: Answer.RENEWED}
    with db.session() as s:  # as core's ``learn`` does
        s.get_one(AgentSession, job.agent_session).session_id = 'sid'
        s.commit()
    assert Local(leases, 'w1').heartbeat({lease_id: HarnessSession(id='sid')}, HOST) == {lease_id: Answer.RENEWED}
    assert Local(leases, 'w1').report(lease_id, 'r1', DONE) == RECEIPT
    assert Local(leases, 'w1').report(lease_id, 'r1', DONE) == RECEIPT, 'a resent report gets its receipt'
    assert recorder.calls == [
        ('started', job.agent_session, {'pid': 7}),
        ('session', job.agent_session, 'sid'),
        ('finished', job.agent_session, DONE),
    ], "it applied once, with the runtime's handle, and a session that the row has goes to core no more"
    row = lease_row(db, lease_id)
    assert (row.state, row.worker, row.handle, row.report_request) == (
        LeaseState.REPORTED,
        'w1',
        handle.model_dump(),
        'r1',
    )
    with pytest.raises(StaleLease, match=f'lease#{lease_id} is reported'):
        Local(leases, 'w1').report(lease_id, 'r2', DONE)


def test_a_report_with_an_old_fencing_token_is_refused_and_kept_as_a_stale_report(
    db: Database, campaign: Campaign, tmp_path: Path, leases: Leases, recorder: Recorder
) -> None:
    """A worker presumed dead comes back after its lease expired and its job went to another: its report is refused."""
    job = evaluation_job(db, campaign, tmp_path)
    first = queue(db, job)
    assert take(leases, 'w1', ['process']) is not None
    leases.expire(utcnow() + timedelta(seconds=301))
    assert lease_row(db, first).state is LeaseState.EXPIRED
    queue(db, job)  # core submits the lost evaluation again
    second = take(leases, 'w2', ['process'])
    assert second is not None and second.lease > first, 'the fencing token grows with each lease of the job'
    with pytest.raises(StaleLease, match=f'stale_lease: lease#{first} is expired'):
        Local(leases, 'w1').report(first, 'late', MEASURED)
    assert Local(leases, 'w1').heartbeat({first: HarnessSession()}, HOST) == {first: Answer.STALE}
    assert [c[0] for c in recorder.calls] == ['lost'], 'nothing of the late report applied'
    [(kind, target, payload)] = events(db, StaleReport)
    assert (kind, target, payload.lease, payload.worker) == (CANDIDATE, job.candidate, first, 'w1')
    assert payload.report == MEASURED
    assert Local(leases, 'w2').report(second.lease, 'r', MEASURED) == {'reported': 'evaluation'}


def test_only_the_newest_lease_of_a_job_is_live(
    db: Database, campaign: Campaign, tmp_path: Path, leases: Leases
) -> None:
    """The fence checks the token, not only the state: two live leases of one job cannot both report."""
    job = agent_job(db, campaign, tmp_path)
    old = queue(db, job)
    take(leases, 'w1', [])
    new = queue(db, job)
    take(leases, 'w2', [])
    with pytest.raises(StaleLease, match=f'lease#{old} was replaced by lease#{new}'):
        Local(leases, 'w1').started(old, HANDLE, None)
    with pytest.raises(StaleLease, match='held by w2'):
        Local(leases, 'w1').started(new, HANDLE, None)


def test_a_silent_worker_is_unreachable_after_the_grace_period_and_its_leases_expire_after_the_toleration(
    db: Database, campaign: Campaign, tmp_path: Path, leases: Leases, recorder: Recorder, mocker: MockerFixture
) -> None:
    job = agent_job(db, campaign, tmp_path)
    lease_id = queue(db, job)
    before = utcnow()
    take(leases, 'w1', [])
    now = utcnow()
    expiry = leases.expire(now + timedelta(seconds=49))
    grace = timedelta(seconds=50)
    assert expiry is not None and before + grace <= expiry <= now + grace, 'the next deadline: when it goes silent'
    assert events(db, LeaseUnreachable) == []
    leases.expire(now + timedelta(seconds=51))
    assert leases.expire(now + timedelta(seconds=52)) == lease_row(db, lease_id).expires_at, (
        'the next deadline of an unreachable worker: its lease expires'
    )
    assert events(db, LeaseUnreachable) == [
        (TargetKind.AGENT_SESSION, job.agent_session, LeaseUnreachable(lease=lease_id, worker='w1'))
    ], 'once per silence'
    assert worker_row(db, 'w1').no_lease == NoLease.UNREACHABLE
    Local(leases, 'w1').heartbeat({lease_id: HarnessSession()}, HOST)
    assert worker_row(db, 'w1').no_lease is None, 'heard again'
    heard = utcnow()
    leases.expire(heard + timedelta(seconds=51))  # unreachable again: a due of its own, not an absence
    assert leases.expire(heard + timedelta(seconds=301)) is None, 'expired: nothing left to wait for'
    row = lease_row(db, lease_id)
    assert (row.state, row.receipt) == (LeaseState.EXPIRED, {'lost': 'worker lost: w1 was silent for 301 s'})
    [(_, _, payload)] = events(db, LeaseExpired)
    assert payload.lease == lease_id and recorder.calls[-1][:2] == ('lost', job.agent_session)
    locked = mocker.spy(leases_module, 'write_transaction')
    leases.expire(heard + timedelta(seconds=302))
    assert locked.call_count == 0, "an ended lease's past expiry is not due: no write lock"


def test_a_restarted_daemon_gives_every_live_lease_a_full_period(
    db: Database, campaign: Campaign, tmp_path: Path, leases: Leases
) -> None:
    lease_id = queue(db, agent_job(db, campaign, tmp_path))
    take(leases, 'w1', [])
    join(leases, 'idle', [])
    with db.session() as s:
        s.get_one(Lease, lease_id).expires_at = utcnow() - timedelta(seconds=1)
        s.commit()
    now = utcnow()
    leases.renew_all(now)
    row = lease_row(db, lease_id)
    assert (row.renewed_at, row.expires_at) == (now, now + timedelta(seconds=300))
    assert (worker_row(db, 'w1').seen_at, worker_row(db, 'idle').seen_at != now) == (now, True), 'only a holder'
    leases.expire()
    assert lease_row(db, lease_id).state is LeaseState.ACQUIRED, "nothing expires for the daemon's own absence"


def test_a_revoked_lease_is_answered_revoked_and_still_reports(
    db: Database, campaign: Campaign, tmp_path: Path, leases: Leases, recorder: Recorder
) -> None:
    lease_id = queue(db, agent_job(db, campaign, tmp_path))
    take(leases, 'w1', [])
    with db.session() as s:
        revoke(s, home_cas(db), s.get_one(Lease, lease_id))
        s.commit()
    assert Local(leases, 'w1').heartbeat({lease_id: HarnessSession()}, HOST) == {lease_id: Answer.REVOKED}
    assert Local(leases, 'w1').report(lease_id, 'r', DONE) == RECEIPT


def test_an_evaluation_goes_to_a_worker_with_its_runner_in_the_order_in_which_it_was_submitted(
    db: Database, campaign: Campaign, tmp_path: Path, leases: Leases
) -> None:
    """Core submits an evaluation when it has a slot, in the queue's order: the leases keep that order."""
    other = evaluation_job(db, campaign, tmp_path, runner='gpu-runner')
    queue(db, other)
    assert take(leases, 'w1', ['process']) is None, 'no worker has the runner'
    job = evaluation_job_for_another_candidate(db, campaign, tmp_path)
    queue(db, job)
    grant = take(leases, 'w1', ['process'])
    assert grant is not None and key_of(granted(db, grant)) == key_of(job)
    second = take(leases, 'w2', ['process', 'gpu-runner'])
    assert second is not None and key_of(granted(db, second)) == key_of(other)
    leases.revoke(key_of(job))
    assert [lease_row(db, g.lease).revoked_at is not None for g in (grant, second)] == [True, False], (
        "a revoke finds its own evaluation's lease, not another's"
    )


def evaluation_job_for_another_candidate(
    db: Database, campaign: Campaign, tmp_path: Path, tree: str = 't2'
) -> EvaluationJob:
    """An evaluation of another candidate, of ``tree``: one candidate and evaluator have one evaluation."""
    base = baseline(db, campaign.id)
    with db.session() as s:
        candidate = Candidate(campaign_id=campaign.id, tree_sha=tree, revision=base.revision, parent_id=base.id)
        s.add(candidate)
        s.flush()
        evaluation = Evaluation(candidate_id=candidate.id, evaluator_id=s.get_one(Campaign, campaign.id).evaluator_id)
        s.add(evaluation)
        s.commit()
        evaluation_id, candidate_id = evaluation.id, candidate.id
    return EvaluationJob(
        evaluation=evaluation_id,
        candidate=candidate_id,
        runner='process',
        artifact=Source(vcs=Git(), repo=RepoId(campaign.id), revision=base.revision),
        place='eval2',
        command=['true'],
        timeout=60,
        score=Score(name='score', direction=Direction.HIGHER),
        required=['score'],
        known=['score'],
        max_evidence_bytes=0,
    )


def test_a_worker_finds_its_live_leases_again_with_their_handles(
    db: Database, campaign: Campaign, tmp_path: Path, leases: Leases
) -> None:
    job = agent_job(db, campaign, tmp_path)
    lease_id = queue(db, job)
    take(leases, 'w1', [])
    Local(leases, 'w1').started(lease_id, AgentHandle(runtime='subprocess', data={'pid': 1}), None)
    [grant] = join(leases, 'w1', []).held
    assert (grant.lease, granted(db, grant), grant.handle) == (
        lease_id,
        job,
        AgentHandle(runtime='subprocess', data={'pid': 1}),
    )
    assert grant.started_at is not None and join(leases, 'w2', []).held == []
    Local(leases, 'w1').lost(lease_id, 'gone')
    assert join(leases, 'w1', []).held == [] and lease_row(db, lease_id).receipt == {'lost': 'gone'}


def test_a_revoke_or_a_cancel_changes_only_a_lease_it_applies_to(
    db: Database, campaign: Campaign, tmp_path: Path, leases: Leases
) -> None:
    """A queued lease has no worker to revoke; a reported one is over, and a cancel leaves it as it is."""
    queued = queue(db, agent_job(db, campaign, tmp_path))
    with db.session() as s:
        revoke(s, home_cas(db), s.get_one(Lease, queued))
        s.commit()
    assert lease_row(db, queued).revoked_at is None
    take(leases, 'w1', [])
    Local(leases, 'w1').report(queued, 'r', DONE)
    with db.session() as s:
        cancel(s, home_cas(db), s.get_one(Lease, queued), 'daemon stopped')
        s.commit()
    assert lease_row(db, queued).state is LeaseState.REPORTED


def test_each_move_of_a_lease_is_one_event_on_its_jobs_row_and_an_ended_lease_never_moves_again(
    db: Database, campaign: Campaign, tmp_path: Path, leases: Leases
) -> None:
    """``LEASE_STATES`` is the one writer of ``leases.state``: its guarded UPDATE refuses a move from any other state."""
    lease_id = queue(db, agent_job(db, campaign, tmp_path))
    take(leases, 'w1', [])
    Local(leases, 'w1').started(lease_id, HANDLE, None)
    Local(leases, 'w1').report(lease_id, 'r', DONE)
    on = (TargetKind.AGENT_SESSION, 1)
    assert [
        (e[:2], e[2].kind) for model in (LeaseAcquired, LeaseStarted, LeaseReported) for e in events(db, model)
    ] == [
        (on, 'lease.acquired'),
        (on, 'lease.started'),
        (on, 'lease.reported'),
    ]
    with db.session() as s, pytest.raises(Refused, match=f'lease#{lease_id} is reported: it cannot become running'):
        transition(
            s, LEASE_STATES, lease_id, LeaseState.RUNNING, LeaseStarted(lease=lease_id, worker='w1'), System.DAEMON
        )
    queued = queue(db, evaluation_job(db, campaign, tmp_path))
    with db.session() as s:
        cancel(s, home_cas(db), s.get_one(Lease, queued), NOT_TAKEN)
        s.commit()
    [(_, _, cancelled)] = events(db, LeaseCancelled)
    assert (cancelled.lease, cancelled.reason, lease_row(db, queued).state) == (queued, NOT_TAKEN, LeaseState.CANCELLED)


def test_a_report_on_a_lease_that_never_was_is_refused_and_kept_nowhere(db: Database, leases: Leases) -> None:
    with pytest.raises(StaleLease, match='there is no lease#9'):
        Local(leases, 'w1').report(9, 'r', DONE)
    assert events(db, StaleReport) == []


def test_the_fence_and_the_write_are_one_transaction_so_an_expiry_cannot_slip_between_them(
    db: Database, campaign: Campaign, tmp_path: Path, recorder: Recorder
) -> None:
    """An expiry that comes while a report applies waits for it, then finds the lease reported: one of them applies."""
    other = Recorder(db)
    expiring = Leases(db, other, home_cas(db), TIMING, POOLS)
    expiries: list[threading.Thread] = []

    class Slow(Recorder):
        def finished(self, job: Job, report: Report, *, guard: Guard | None = None) -> None:
            def slow(s: Session) -> None:
                assert guard is not None
                guard(s)
                thread = threading.Thread(target=expiring.expire, args=(utcnow() + timedelta(seconds=301),))
                thread.start()
                expiries.append(thread)
                time.sleep(0.5)  # the expiry runs now, between the fence and the commit

            super().finished(job, report, guard=slow)

    leases = Leases(db, Slow(db), home_cas(db), TIMING, POOLS)
    job = evaluation_job(db, campaign, tmp_path)
    lease_id = queue(db, job)
    take(leases, 'w1', ['process'])
    Local(leases, 'w1').report(lease_id, 'r', MEASURED)
    expiries[0].join(30)
    with db.session() as s:
        assert [lease.state for lease in s.scalars(select(Lease))] == [LeaseState.REPORTED], 'not queued again'
    assert other.calls == [] and events(db, LeaseExpired) == []


def test_a_daemon_that_was_away_longer_than_the_grace_period_renews_the_leases_before_it_expires_any(
    db: Database, campaign: Campaign, tmp_path: Path, leases: Leases, recorder: Recorder
) -> None:
    """A laptop asleep for an hour: the loop woke an hour past its plan, and its own absence is no worker's silence."""
    lease_id = queue(db, agent_job(db, campaign, tmp_path))
    take(leases, 'w1', [])
    now = utcnow()
    leases.expire(now + timedelta(hours=1), absent=timedelta(hours=1))  # the first turn after the resume
    assert (lease_row(db, lease_id).state, recorder.calls) == (LeaseState.ACQUIRED, [])
    assert events(db, LeaseUnreachable) == [], 'and not unreachable either'


def test_an_unreachable_worker_counts_no_slot_until_it_is_heard_again(
    db: Database, campaign: Campaign, tmp_path: Path, leases: Leases
) -> None:
    held = queue(db, agent_job(db, campaign, tmp_path))
    take(leases, 'w1', [])
    assert leases.capacity() == Capacity(None, {EVALUATION: 99})
    with db.session() as s:
        s.get_one(LeaseWorker, 'w1').seen_at = utcnow() - timedelta(seconds=51)
        s.commit()
    assert leases.capacity() == Capacity(0, {EVALUATION: 0}), 'unreachable'
    Local(leases, 'w1').heartbeat({held: HarnessSession()}, HOST)
    assert leases.capacity() == Capacity(None, {EVALUATION: 99})


def test_capacity_counts_a_lease_that_a_worker_takes_while_it_reads_once(
    db: Database, campaign: Campaign, tmp_path: Path, recorder: Recorder
) -> None:
    """Its reads are one snapshot: a lease that moves from queued to live between two of them is counted once."""
    leases = Leases(db, recorder, home_cas(db), TIMING, {'test': Pool(agents=2)})
    join(leases, 'w1', [])
    queue(db, agent_job(db, campaign, tmp_path))
    assert leases.capacity().agents == 1, 'two slots, one taken by the queued job'

    def acquire_before_the_queued_count(conn: object, cursor: object, statement: str, *_: object) -> None:
        if statement.startswith('SELECT leases.kind') and 'leases.state = ?' in statement and not acquiring:
            acquiring.append(True)  # its own reads come through here too
            taking = threading.Thread(target=lambda: acquired.append(Local(leases, 'w1').acquire(uuid4().hex, HOST)))
            taking.start()  # another worker's thread: this one holds the read's connection
            taking.join(10)

    acquiring: list[bool] = []
    acquired: list[object] = []
    event.listen(db.engine, 'before_cursor_execute', acquire_before_the_queued_count)
    try:
        assert leases.capacity().agents == 1
    finally:
        event.remove(db.engine, 'before_cursor_execute', acquire_before_the_queued_count)
    assert isinstance(acquired[0], Grant), 'the lease moved from queued to live during the read'


class Settles(Recorder):
    """Core that finds every job's row settled already: closed by a stop, measured, or gone."""

    def _apply(self, guard: Guard | None, call: tuple[str, int, object], change: Callable[[Session], None]) -> None:
        raise Settled('agent_session#1 is closed')


@pytest.mark.parametrize('refused', ['started', 'report'])
def test_a_job_whose_row_is_settled_is_refused_as_stale_and_its_lease_ends_at_once(
    db: Database, campaign: Campaign, tmp_path: Path, refused: str
) -> None:
    """Core refuses a start or a result for a closed row (``Settled``): the worker stops the job and forgets it.

    The lease used to stay live, with nothing to renew or report it: its slot was held until it expired as lost.
    """
    leases = Leases(db, Settles(db), home_cas(db), TIMING, POOLS)
    lease_id = queue(db, agent_job(db, campaign, tmp_path))
    take(leases, 'w1', [])
    with pytest.raises(StaleLease, match='stale_lease: agent_session#1 is closed'):
        if refused == 'started':
            Local(leases, 'w1').started(lease_id, AgentHandle(runtime='subprocess', data={'pid': 1}), None)
        else:
            Local(leases, 'w1').report(lease_id, 'r', DONE)
    assert (lease_row(db, lease_id).state, lease_row(db, lease_id).receipt) == (
        LeaseState.CANCELLED,
        {'cancelled': SETTLED, 'settled': 'agent_session#1 is closed'},
    )
    assert [p.reason for _, _, p in events(db, StaleReport)] == (
        ['agent_session#1 is closed'] if refused == 'report' else []
    )
    assert Local(leases, 'w1').heartbeat({lease_id: HarnessSession()}, HOST) == {lease_id: Answer.STALE}


def test_a_lost_job_whose_row_is_settled_ends_its_lease_and_its_receipt_says_so(
    db: Database, campaign: Campaign, tmp_path: Path
) -> None:
    """A worker that restarted cannot find its job, whose row a stop closed meanwhile: nothing is left to lose."""
    leases = Leases(db, Settles(db), home_cas(db), TIMING, POOLS)
    lease_id = queue(db, agent_job(db, campaign, tmp_path))
    take(leases, 'w1', [])
    leases.lost(lease_id, 'w1', 'its worktree is gone')
    assert (lease_row(db, lease_id).state, lease_row(db, lease_id).receipt) == (
        LeaseState.EXPIRED,
        {'lost': 'its worktree is gone', 'settled': 'agent_session#1 is closed'},
    )


def test_an_adoption_ends_the_evaluation_leases_of_the_previous_daemon_taken_or_not(
    db: Database, campaign: Campaign, tmp_path: Path, leases: Leases
) -> None:
    """The next daemon asks again for the measurements that it needs: core dropped the rows of the previous one's.

    A taken one used to run to its end, and its refused report then left the lease live (B36): now it is revoked.
    """
    untaken = queue(db, evaluation_job(db, campaign, tmp_path))
    taken = queue(db, evaluation_job(db, campaign, tmp_path, runner='other'))
    join(leases, 'w1', ['other'])
    Local(leases, 'w1').acquire(uuid4().hex, HOST)
    assert leases.adopt([], 'local') == []
    assert (lease_row(db, untaken).state, lease_row(db, untaken).receipt) == (
        LeaseState.CANCELLED,
        {'cancelled': NOT_TAKEN},
    )
    assert Local(leases, 'w1').heartbeat({taken: HarnessSession()}, HOST) == {taken: Answer.REVOKED}, 'it is killed'


@pytest.mark.parametrize('marked', [False, True], ids=['silent', 'marked unreachable'])
def test_a_daemon_back_from_an_absence_announces_the_workers_that_it_counts_again(
    db: Database, campaign: Campaign, tmp_path: Path, marked: bool
) -> None:
    """``renew_all`` gives a silent worker's slots back: it says so, as a heartbeat would (B37)."""
    leases = Leases(db, Recorder(db), home_cas(db), TIMING, {'test': Pool(slots={EVALUATION: 2})})
    queue(db, evaluation_job(db, campaign, tmp_path))
    take(leases, 'w1', ['process'])
    with db.session() as s:
        s.get_one(LeaseWorker, 'w1').seen_at = utcnow() - timedelta(seconds=TIMING.grace + 1)
        s.commit()
    if marked:
        leases.expire()
    joined = len(events(db, LeaseWorkerJoined))
    assert leases.capacity().free(EVALUATION) == 0, 'silent'
    leases.expire(absent=timedelta(seconds=TIMING.grace + 1))
    assert (leases.capacity().free(EVALUATION), len(events(db, LeaseWorkerJoined)) - joined) == (1, 1)
    with db.session() as s:
        assert s.get_one(LeaseWorker, 'w1').no_lease is None


def test_a_settled_refusal_of_a_lease_that_ended_meanwhile_leaves_its_end(
    db: Database, campaign: Campaign, tmp_path: Path
) -> None:
    """A refusal races the lease's end, by a report or an expiry: the end that came first stays."""
    leases = Leases(db, Recorder(db), home_cas(db), TIMING, POOLS)
    lease_id = queue(db, agent_job(db, campaign, tmp_path))
    take(leases, 'w1', [])
    Local(leases, 'w1').report(lease_id, 'r', DONE)
    receipt = lease_row(db, lease_id).receipt
    leases._end_settled(lease_id, 'w1', Settled('agent_session#1 is closed'))
    assert (lease_row(db, lease_id).state, lease_row(db, lease_id).receipt) == (LeaseState.REPORTED, receipt)


def test_a_crash_report_names_no_blob_and_says_why() -> None:
    """What a worker sends in place of a report that the control plane rejected ``report_tries`` times."""
    blob = Cid.of(b'x')
    finished = AgentFinished(process=AgentExit(ExitStatus.EXITED, 1.0, 0), files={'a': blob}, workspace={'b': blob})
    crashed = finished.crashed('rejected')
    assert (crashed.blobs(), crashed.process.error, crashed.process.status) == ([], 'rejected', ExitStatus.EXITED)
    unstarted = AgentNotStarted(reason='a stop came', files={'a': blob})
    assert unstarted.crashed('rejected') == unstarted.model_copy(update={'files': {}})


def reported(leases: Leases, lease_id: int) -> None:
    leases.report(lease_id, 'w1', 'r', DONE)


def renewed(leases: Leases, lease_id: int) -> None:
    leases.heartbeat('w1', {lease_id: HarnessSession()}, HOST)


@pytest.mark.parametrize(
    ('heard', 'state', 'applied'),
    [(reported, LeaseState.REPORTED, ['finished']), (renewed, LeaseState.ACQUIRED, [])],
    ids=['report', 'heartbeat'],
)
def test_an_expiry_that_a_report_or_a_heartbeat_overtakes_expires_nothing(
    db: Database,
    campaign: Campaign,
    tmp_path: Path,
    leases: Leases,
    recorder: Recorder,
    mocker: MockerFixture,
    heard: Callable[[Leases, int], None],
    state: LeaseState,
    applied: list[str],
) -> None:
    """It comes between the expiry's look for silent leases and its write: the lease is no longer silent then."""
    lease_id = queue(db, agent_job(db, campaign, tmp_path))
    take(leases, 'w1', [])
    with db.session() as s:
        s.get_one(Lease, lease_id).expires_at = utcnow() - timedelta(seconds=1)
        s.commit()
    lose = Leases._lose

    def heard_first(self: Leases, lease_id: int, reason: str, fence: Callable[[Session], Lease]) -> None:
        heard(self, lease_id)
        lose(self, lease_id, reason, fence)

    mocker.patch.object(Leases, '_lose', heard_first)
    leases.expire(utcnow())
    assert (lease_row(db, lease_id).state, [c[0] for c in recorder.calls]) == (state, applied)


def test_a_stop_leaves_an_agent_lease_that_a_worker_took_meanwhile_to_its_report(
    db: Database, campaign: Campaign, tmp_path: Path
) -> None:
    class TakenFirst(Recorder):
        def finished(self, job: Job, report: Report, *, guard: Guard | None = None) -> None:
            take(leases, 'w1', [])  # between the stop's look for untaken leases and its write
            super().finished(job, report, guard=guard)

    recorder = TakenFirst(db)
    leases = Leases(db, recorder, home_cas(db), TIMING, POOLS)
    lease_id = queue(db, agent_job(db, campaign, tmp_path))
    leases.stop_untaken()
    assert (lease_row(db, lease_id).state, recorder.calls) == (LeaseState.ACQUIRED, [])


def test_a_prompt_handed_over_again_is_not_written_again_and_survives_the_process(
    db: Database, campaign: Campaign, tmp_path: Path, recorder: Recorder
) -> None:
    """Core hands an unanswered prompt over again after a daemon restart: the lease has it already."""
    job = agent_job(db, campaign, tmp_path)
    first = Leases(db, recorder, home_cas(db), TIMING, POOLS)
    start = join(first, 'w1', []).cursor
    queue(db, job, 'w1')
    grant = Local(first, 'w1').acquire(uuid4().hex, HOST)
    assert isinstance(grant, Grant)
    first.prompt(AgentKey(job.agent_session), Prompt(CommentId(7), 'try lzma'))
    second = Leases(db, recorder, home_cas(db), TIMING, POOLS)  # the next daemon process
    second.prompt(AgentKey(job.agent_session), Prompt(CommentId(7), 'try lzma'))
    assert [p.comment for _, _, p in events(db, LeasePrompted)] == [7]
    [_, prompt] = Local(second, 'w1').messages(start, 5)
    assert isinstance(prompt, PromptMessage) and prompt.prompt == Prompt(CommentId(7), 'try lzma')


def test_a_worker_that_restarts_resumes_from_its_cursor(
    db: Database, campaign: Campaign, tmp_path: Path, leases: Leases
) -> None:
    """Its last read stored the cursor that it gave: its ack floor. It gets again only what lies after it."""
    job = agent_job(db, campaign, tmp_path)
    start = join(leases, 'w1', []).cursor
    queue(db, job, 'w1')
    w1 = Local(leases, 'w1')
    grant = w1.acquire(uuid4().hex, HOST)
    assert isinstance(grant, Grant)
    leases.prompt(AgentKey(job.agent_session), Prompt(CommentId(1), 'one'))
    leases.prompt(AgentKey(job.agent_session), Prompt(CommentId(2), 'two'))
    [_, one, two] = w1.messages(start, 5)
    w1.queued(one.id, None)
    assert w1.messages(one.id, 0.01) == [two], 'the first is answered: the worker reads after it, then dies'
    again = join(leases, 'w1', [])
    assert (again.cursor, [g.lease for g in again.held]) == (one.id, [grant.lease])
    assert w1.messages(again.cursor, 0.01) == [two]
    with db.session() as s:
        s.get_one(LeaseWorker, 'w1').cursor = two.id + 100  # a database restored from an older backup
        s.commit()
        end = feed.newest(s)
    assert join(leases, 'w1', []).cursor == end, 'a cursor past the feed starts again at its newest event'


def test_a_change_that_rolls_back_leaves_no_message(
    db: Database, campaign: Campaign, tmp_path: Path, leases: Leases
) -> None:
    """The outbox: a message is written in the transaction of the change that it announces, or not at all."""
    lease_id = queue(db, agent_job(db, campaign, tmp_path))
    take(leases, 'w1', [])
    with db.session() as s:
        enqueue(s, home_cas(db), agent_job(db, campaign, tmp_path), None)
        revoke(s, home_cas(db), s.get_one(Lease, lease_id))
        s.rollback()
    assert lease_row(db, lease_id).revoked_at is None
    assert [p.lease for _, _, p in events(db, LeaseQueued)] == [lease_id]
    assert events(db, LeaseRevoked) == []


def test_a_waiting_worker_wakes_on_a_commit_of_this_process(
    db: Database, campaign: Campaign, tmp_path: Path, leases: Leases
) -> None:
    start = join(leases, 'w1', []).cursor
    threading.Timer(0.2, leases.submit, [agent_job(db, campaign, tmp_path)]).start()
    began = time.monotonic()
    assert [type(m) for m in Local(leases, 'w1').messages(start, 30)] == [Work]
    assert time.monotonic() - began < 10


def test_a_poll_that_waits_for_nothing_reads_again_only_after_a_message_of_some_worker(
    db: Database, campaign: Campaign, tmp_path: Path, leases: Leases
) -> None:
    """A worker polls every heartbeat, and most polls find nothing: those read no row until a message comes."""
    with db.session() as s:  # an event that is no message: a cursor before the newest
        s.add(Comment(target=TargetRef(TargetKind.CAMPAIGN, campaign.id), author='human', kind='note', body='hi'))
        s.commit()
    start = join(leases, 'w1', []).cursor
    w1 = Local(leases, 'w1')
    statements: list[str] = []

    def counted(_conn: object, _cursor: object, statement: str, *_: object) -> None:
        statements.append(statement)

    assert w1.messages(start, 0) == []
    event.listen(db.engine, 'before_cursor_execute', counted)
    try:
        assert w1.messages(start, 0) == []
        assert statements == [], 'nothing came since its last read after the same cursor'
        assert w1.messages(start - 1, 0) == [] and statements, 'another cursor reads'
        statements.clear()
        assert w1.messages(start - 1, 0) == [] and statements == []
        assert leases._messages is not None
        woken = leases._messages.generation
        queue(db, agent_job(db, campaign, tmp_path), 'w2')  # a message of another worker
        wait_until(lambda: leases._messages is not None and leases._messages.generation > woken, 'the wake')
        assert w1.messages(start - 1, 0) == [] and statements, 'it read, and found nothing of its own'
        woken = leases._messages.generation
        queue(db, agent_job(db, campaign, tmp_path), 'w1')  # its own
        wait_until(lambda: leases._messages is not None and leases._messages.generation > woken, 'the wake')
        assert [type(m) for m in w1.messages(start - 1, 0)] == [Work]
        assert statements, 'it read'
    finally:
        event.remove(db.engine, 'before_cursor_execute', counted)


def test_a_prompt_for_an_agent_without_a_live_lease_is_undelivered_at_once(
    db: Database, campaign: Campaign, tmp_path: Path, leases: Leases, recorder: Recorder
) -> None:
    job = agent_job(db, campaign, tmp_path)
    queue(db, job)
    leases.prompt(AgentKey(job.agent_session), Prompt(CommentId(3), 'x'))
    assert recorder.calls == [('queued', job.agent_session, (3, 'the agent has no live lease'))]


@dataclass
class Overtaken(Recorder):
    """A ``Results`` whose first ``finished`` runs ``first`` before it: the same report, applied meanwhile."""

    first: Callable[[], object] | None = None

    def finished(self, job: Job, report: Report, *, guard: Guard | None = None) -> None:
        if (first := self.first) is not None:
            self.first = None
            first()
        super().finished(job, report, guard=guard)


def test_a_report_sent_again_while_the_first_applies_gets_its_receipt_and_is_not_kept_as_stale(
    db: Database, campaign: Campaign, tmp_path: Path
) -> None:
    """A worker that gave up waiting for an answer sends its report again while the server still applies the first."""
    recorder = Overtaken(db)
    leases = Leases(db, recorder, home_cas(db), TIMING, POOLS)
    join(leases, 'w1', [])
    lease = queue(db, agent_job(db, campaign, tmp_path))
    w1 = Local(leases, 'w1')
    assert isinstance(w1.acquire(uuid4().hex, HOST), Grant)
    recorder.first = partial(w1.report, lease, 'r1', DONE)
    assert w1.report(lease, 'r1', DONE) == RECEIPT
    assert events(db, StaleReport) == []
    assert [c[0] for c in recorder.calls] == ['finished'], 'applied once'


def test_a_worker_whose_slots_count_again_says_so_on_the_home(
    db: Database, campaign: Campaign, tmp_path: Path, recorder: Recorder
) -> None:
    """The daemon wakes on these: a worker that joins, one heard again after its silence, one whose host recovers."""
    leases = Leases(db, recorder, home_cas(db), TIMING, {'test': Pool(max_load=1.0, slots={EVALUATION: 99})})
    join(leases, 'w1', [])
    assert events(db, LeaseWorkerJoined) == [(TargetKind.HOME, 1, LeaseWorkerJoined(worker='w1', pool='test'))]
    with db.session() as s:
        s.get_one(LeaseWorker, 'w1').seen_at = utcnow() - timedelta(seconds=TIMING.grace + 1)
        s.commit()
    leases.expire()
    Local(leases, 'w1').heartbeat({}, HOST)
    assert len(events(db, LeaseWorkerJoined)) == 2, 'heard again'
    with db.session() as s:
        s.get_one(LeaseWorker, 'w1').seen_at = utcnow() - timedelta(seconds=TIMING.grace + 1)
        s.commit()
    assert leases.capacity().free(EVALUATION) == 0, 'silent past the grace: its slots count zero'
    Local(leases, 'w1').heartbeat({}, HOST)
    assert len(events(db, LeaseWorkerJoined)) == 3, 'heard again before any expire marked it unreachable'
    assert leases.capacity().free(EVALUATION) == 99
    with db.session() as s:
        s.get_one(LeaseWorker, 'w1').seen_at = utcnow() - timedelta(seconds=TIMING.grace + 1)
        s.commit()
    join(leases, 'w1', [])
    assert len(events(db, LeaseWorkerJoined)) == 4, 'a worker that registers again after its silence: once'
    Local(leases, 'w1').heartbeat({}, HOST)
    assert len(events(db, LeaseWorkerJoined)) == 4, 'heard within the grace: its slots counted all along'
    Local(leases, 'w1').heartbeat({}, HOST.model_copy(update={'load1': 9.0}))
    assert events(db, LeaseWorkerRelieved) == [], 'pressed: nothing counts again'
    Local(leases, 'w1').heartbeat({}, HOST)
    assert events(db, LeaseWorkerRelieved) == [(TargetKind.HOME, 1, LeaseWorkerRelieved(worker='w1', was=NoLease.LOAD))]
    Local(leases, 'w1').heartbeat({}, HOST)
    assert len(events(db, LeaseWorkerRelieved)) == 1, 'once, at the change'


class Clock:
    """The time that ``leases`` reads (``utcnow``), moved by hand."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.now = utcnow()
        monkeypatch.setattr(leases_module, 'utcnow', lambda: self.now)

    def after(self, seconds: float) -> datetime:
        self.now += timedelta(seconds=seconds)
        return self.now


def test_a_heartbeat_that_changes_nothing_writes_nothing_and_the_lease_still_expires_on_silence(
    db: Database, campaign: Campaign, tmp_path: Path, recorder: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With a slack, a renewal writes the expiry ``toleration + slack`` ahead, and a worker is silent once its written
    ``seen_at`` is ``grace + slack`` old. So a lease expires ``toleration`` after its last heartbeat at the earliest, a
    worker is silent ``grace`` after it at the earliest, as with every heartbeat written, and each is found at most a
    slack later. A worker that heartbeats every ``renew`` neither expires nor goes silent."""
    timing = Timing(renew=10, grace=50, toleration=300, slack=60)
    clock = Clock(monkeypatch)
    leases = Leases(db, recorder, home_cas(db), timing, POOLS)
    lease_id = queue(db, agent_job(db, campaign, tmp_path))
    grant = take(leases, 'w1', ['process'])
    assert grant is not None and grant.lease == lease_id
    w1 = Local(leases, 'w1')
    held = {lease_id: HarnessSession()}
    statements: list[str] = []

    def counted(_conn: object, _cursor: object, statement: str, *_: object) -> None:
        statements.append(statement)

    event.listen(db.engine, 'before_cursor_execute', counted)
    try:
        for _ in range(40):  # 400 s of heartbeats, every renew
            beat = clock.after(10)
            assert w1.heartbeat(held, HOST) == {lease_id: Answer.RENEWED}
            leases.expire(beat)
            row = lease_row(db, lease_id)
            assert row.state in LIVE_LEASES and row.expires_at is not None
            assert row.expires_at >= beat + timedelta(seconds=300), 'toleration after it at least'
    finally:
        event.remove(db.engine, 'before_cursor_execute', counted)
    writes = sum(s.startswith('BEGIN IMMEDIATE') for s in statements)
    assert writes == 6, f'one write per slack of 60 s, not one per heartbeat: {writes} in 40'
    assert events(db, LeaseUnreachable) == [], 'never silent'
    last = clock.now
    expiry = lease_row(db, lease_id).expires_at
    assert expiry is not None and last + timedelta(seconds=300) <= expiry <= last + timedelta(seconds=360)
    leases.expire(last + timedelta(seconds=49))
    assert events(db, LeaseUnreachable) == [], 'not silent before the grace: its seen_at is at most a slack old'
    leases.expire(last + timedelta(seconds=110))
    assert [e.lease for _, _, e in events(db, LeaseUnreachable)] == [lease_id], 'silent by grace + slack'
    leases.expire(expiry - timedelta(seconds=1))
    assert lease_row(db, lease_id).state in LIVE_LEASES
    leases.expire(expiry)
    assert lease_row(db, lease_id).state == LeaseState.EXPIRED, 'silent: it expires'


def test_an_acquire_that_takes_nothing_new_writes_nothing_and_of_two_that_race_for_a_lease_one_takes_it(
    db: Database, campaign: Campaign, tmp_path: Path, recorder: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each worker acquires on every turn. With a slack, one that gets no lease, or the lease of its request id again,
    reads only. One that finds a queued lease takes it under the write lock, after it read everything again."""
    leases = Leases(db, recorder, home_cas(db), Timing(renew=10, grace=50, toleration=300, slack=60), POOLS)
    w1, w2 = Local(leases, 'w1'), Local(leases, 'w2')
    join(leases, 'w1', ['process'])
    join(leases, 'w2', ['process'])
    assert w1.acquire(uuid4().hex, HOST) is NoLease.NOTHING, 'its first: the reason goes on its row'
    statements: list[str] = []

    def counted(_conn: object, _cursor: object, statement: str, *_: object) -> None:
        statements.append(statement)

    def writes() -> int:
        return sum(s.startswith('BEGIN IMMEDIATE') for s in statements)

    event.listen(db.engine, 'before_cursor_execute', counted)
    try:
        assert w1.acquire(uuid4().hex, HOST) is NoLease.NOTHING and writes() == 0, 'nothing new: no write'
        with db.session() as s:  # its seen_at older than the slack: due a write, whatever the answer
            s.get_one(LeaseWorker, 'w1').seen_at = utcnow() - timedelta(seconds=61)
            s.commit()
        statements.clear()
        assert w1.acquire(uuid4().hex, HOST) is NoLease.NOTHING and writes() == 1, 'it records the call'
        assert worker_row(db, 'w1').seen_at > utcnow() - timedelta(seconds=5)
        lease_id = queue(db, evaluation_job(db, campaign, tmp_path))
        taken: list[Grant | NoLease] = []
        raced = threading.Event()
        write = leases_module.write_transaction

        def racing(s: Session) -> None:  # w1 takes the lease between w2's read and w2's write
            if not raced.is_set():
                raced.set()
                race = threading.Thread(target=lambda: taken.append(w1.acquire(uuid4().hex, HOST)))
                race.start()
                race.join()
            write(s)

        monkeypatch.setattr(leases_module, 'write_transaction', racing)
        statements.clear()
        assert w2.acquire(uuid4().hex, HOST) is NoLease.NOTHING, 'the second read it again under the lock'
        [grant] = taken
        assert isinstance(grant, Grant) and grant.lease == lease_id and lease_row(db, lease_id).worker == 'w1'
        monkeypatch.setattr(leases_module, 'write_transaction', write)
        request = uuid4().hex
        again = w2.acquire(request, HOST)
        assert again is NoLease.NOTHING
        second = queue(db, evaluation_job_for_another_candidate(db, campaign, tmp_path))
        first = w2.acquire(request, HOST)
        assert isinstance(first, Grant) and first.lease == second
        statements.clear()
        assert w2.acquire(request, HOST) == first and writes() == 0, 'its request id again: the same grant, read'
    finally:
        event.remove(db.engine, 'before_cursor_execute', counted)


def test_a_request_id_sent_again_after_its_job_was_queued_again_gets_its_own_ended_lease_not_the_new_one(
    db: Database, campaign: Campaign, tmp_path: Path, recorder: Recorder
) -> None:
    """A lease never goes back to ``queued``: a job queued again is a new lease, a new fence. A request id sent again
    gets the lease that it took, which has ended, and the new one waits for any worker."""
    leases = Leases(db, recorder, home_cas(db), Timing(renew=10, grace=50, toleration=300, slack=60), POOLS)
    w1 = Local(leases, 'w1')
    join(leases, 'w1', ['process'])
    job = evaluation_job(db, campaign, tmp_path)
    first = queue(db, job)
    request = uuid4().hex
    grant = w1.acquire(request, HOST)
    assert isinstance(grant, Grant) and grant.lease == first
    leases.lost(first, 'w1', 'its worker lost the job')
    again = queue(db, job)  # as core submits a lost job again
    assert again != first
    resent = w1.acquire(request, HOST)
    assert isinstance(resent, Grant) and resent.lease == first, 'its own lease, which ended: the next call says stale'
    assert lease_row(db, first).state == LeaseState.EXPIRED
    assert (lease_row(db, again).state, lease_row(db, again).worker) == (LeaseState.QUEUED, None), 'for any worker'
    taken = w1.acquire(uuid4().hex, HOST)
    assert isinstance(taken, Grant) and taken.lease == again
