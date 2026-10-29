"""The schema of the ``workers`` executor, ``executor/workers``: its workers, their instances and first transfers.

The lease kit (``leases``, ``scaling``, ``transfers``) reads and writes them. The ``leases`` table stays core's.
"""

from datetime import datetime
from enum import StrEnum
from pathlib import Path

from covalence import Cid
from sqlalchemy import CheckConstraint, ForeignKey, Index, text
from sqlalchemy.orm import Mapped, mapped_column

from piloop.db.models import EMPTY_JSON, NOW, Base, CidText, by_value, text_checks, utcnow
from piloop.db.schema import Schema
from piloop.db.triggers import on
from piloop.ids import CampaignId, Revision, TargetKind

INSTANCE = TargetKind('instance')
"""The kind of a worker instance, which events of its lifecycle point at (``piloop.target_kinds``)."""


class NoLease(StrEnum):
    """Why a worker got no lease (``piloop.plugins.executor.kit.capacity.admits``). ``piloop worker list`` shows it.

    Declared in order of precedence: of the reasons for each kind of job, the first one declared is the worker's.
    """

    DRAINING = 'draining'
    """The server drained it: it takes no more jobs, and exits once it holds none."""
    MEMORY = 'memory'
    """Its memory available is below ``min_memory_gb``."""
    LOAD = 'load'
    """Its load per CPU is above ``max_load``."""
    SLOTS = 'slots'
    """Its slots of the waiting job's kind are full."""
    NOTHING = 'nothing'
    """Nothing is queued that it can run."""
    UNREACHABLE = 'unreachable'
    """It was silent past ``grace_seconds``."""


class WorkerInstanceState(StrEnum):
    """The lifecycle of a worker that a provisioner starts (``worker_instances``).

    ``starting`` until its worker registers, then ``running``. ``draining`` takes no more jobs: it ends once it holds
    none. ``ended`` says why in ``end``.
    """

    STARTING = 'starting'
    RUNNING = 'running'
    DRAINING = 'draining'
    ENDED = 'ended'


class InstanceEnd(StrEnum):
    """Why a worker instance ended."""

    DRAINED = 'drained'
    """It held no lease when it ended: the pool drained it, or its worker lost its server (``orphan_exit_seconds``)."""
    REGISTER_TIMEOUT = 'register_timeout'
    """Its worker did not register within ``register_seconds``."""
    START_REFUSED = 'start_refused'
    """The provisioner refused to start it: a quota, no capacity, a bad image."""
    LOST = 'lost'
    """Its worker was silent past ``toleration_seconds``."""
    GONE = 'gone'
    """It ended with a lease, or before its worker registered: it crashed, was preempted, or was deleted by hand."""


# the types of its columns, beside core's in Base.type_annotation_map
Base.registry.update_type_annotation_map(
    {NoLease: by_value(NoLease), WorkerInstanceState: by_value(WorkerInstanceState), InstanceEnd: by_value(InstanceEnd)}
)


class LeaseWorker(Base):
    """A worker that takes leases (``piloop.plugins.executor.kit.leases``): its pool, and what it last sent.

    A worker's row is written when it starts, and on each
    heartbeat. A worker whose ``seen_at`` is older than ``grace_seconds`` is unreachable: its slots count zero.
    """

    __tablename__ = 'lease_workers'
    __table_args__ = (*text_checks('lease_workers', 'id', 'pool', 'version'),)

    id: Mapped[str] = mapped_column(primary_key=True)
    """The worker's name. ``serve`` is the worker inside ``piloop serve``."""
    pool: Mapped[str]
    """Its pool: ``[plugins.executor.pools.<pool>]`` gives its slots and thresholds."""
    version: Mapped[str]
    """The piloop version it runs."""
    runners: Mapped[list[str]] = mapped_column(default=list, server_default=text("'[]'"))
    """The evaluator runners it has. It takes only the evaluations of these."""
    sample: Mapped[dict[str, object]] = mapped_column(default=dict, server_default=EMPTY_JSON)
    """Its last host sample (``HostSample``): load, CPUs, memory available and disk free."""
    seen_at: Mapped[datetime]
    """When a call of it last came in."""
    no_lease: Mapped[NoLease | None]
    """Why its last request got no lease, or that it went silent. NULL after a grant."""
    cursor: Mapped[int] = mapped_column(default=0, server_default=text('0'))
    """The event after which it reads its messages: its ack floor. A new worker starts at the newest event."""
    drained_at: Mapped[datetime | None]
    """When the server drained it (``lease.drained``): it takes no more leases. NULL while it takes them."""


class WorkerInstance(Base):
    """A worker that a provisioner starts for a pool (``piloop.plugins.executor.kit.scaling``).

    The row commits before the provisioner's ``start``, so a daemon that restarts finds every instance that it asked
    for. Its name is the worker's everywhere: its credential, its ``lease_workers`` row, the provider's tag.
    """

    __tablename__ = 'worker_instances'
    __table_args__ = (
        CheckConstraint('(state = \'ended\') = ("end" IS NOT NULL)', name='worker_instances.an ended one says why'),
        Index('ix_worker_instances_state', 'state'),
        *text_checks('worker_instances', 'name', 'pool'),
        {'sqlite_autoincrement': True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(unique=True)
    """The worker's name, which core picks: ``<pool>-<8 hex digits>``."""
    pool: Mapped[str]
    state: Mapped[WorkerInstanceState] = mapped_column(
        default=WorkerInstanceState.STARTING, server_default=WorkerInstanceState.STARTING.value
    )
    handle: Mapped[dict[str, object]] = mapped_column(default=dict, server_default=EMPTY_JSON)
    """The provider's own id of it, as its ``list`` last showed it: a sandbox id, a pod uid."""
    requested_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=NOW)
    registered_at: Mapped[datetime | None]
    """When its worker first registered."""
    ended_at: Mapped[datetime | None]
    end: Mapped[InstanceEnd | None]


class FirstTransfer(Base):
    """What a worker's empty clone of a repo of record starts from: its versions up to ``revision``, as one blob.

    The server builds it once per repo (``piloop.plugins.executor.kit.transfers``).
    """

    __tablename__ = 'first_transfers'
    __table_args__ = (*text_checks('first_transfers', 'revision', 'blob'),)

    campaign_id: Mapped[CampaignId] = mapped_column(ForeignKey('campaigns.id', ondelete='CASCADE'), primary_key=True)
    """The repo of record (``RepoId``)."""
    revision: Mapped[Revision]
    """The head of the repo of record when it was built."""
    blob: Mapped[Cid] = mapped_column(CidText)


TABLES = (LeaseWorker, WorkerInstance, FirstTransfer)
SCHEMA = Schema(tables=TABLES, versions=Path(__file__).parent / 'migrations', triggers=on(TABLES), internal=TABLES)
