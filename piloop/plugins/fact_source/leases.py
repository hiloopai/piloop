"""``leases``: the built-in fact source of what the campaign's jobs cost on workers, from piloop's own leases.

Workers are shared: one runs the jobs of several campaigns, so a provider's invoice cannot be split by campaign. The
leases can. Each second of a worker is split evenly among the jobs that it held then, and priced at its pool's
``usd_per_hour`` (``[plugins.executor.pools.<pool>]`` in ``config.toml``). The time that a worker held no job is no
campaign's.
"""

from collections import defaultdict
from collections.abc import Mapping
from datetime import datetime
from itertools import pairwise

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session

from piloop.db.events import has
from piloop.db.models import AgentSession, Campaign, Event, Lease, LeaseWorker, WorkerInstance
from piloop.plugins.campaign_type.kit.candidates.ids import CANDIDATE
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.executor.contract import EXECUTORS
from piloop.plugins.executor.kit.capacity import Pool
from piloop.plugins.executor.kit.leases import LeaseQueued
from piloop.plugins.executor.workers import Workers
from piloop.plugins.fact_source.contract import FactSource
from piloop.plugins.fact_source.facts import COMPUTE_USD, Fact, FactValue
from piloop.settings import settings

type Holding = tuple[datetime, datetime, bool]
"""One job on a worker: when it took the worker, when it let go (or now), and whether it is of the campaign."""


class LeaseFacts(FactSource):
    """``leases``: ``compute_usd``, the seconds that the campaign's jobs held a worker, priced by its pool."""

    def supplies(self) -> tuple[Fact[FactValue], ...]:
        """``COMPUTE_USD``."""
        return (COMPUTE_USD,)

    def gather(
        self, s: Session, campaign: Campaign, wanted: frozenset[Fact[FactValue]], now: datetime
    ) -> dict[Fact[FactValue], FactValue]:
        """``COMPUTE_USD``: None when a worker that ran a job of the campaign is of a pool without a price."""
        return {COMPUTE_USD: compute_usd(s, campaign, _pools(), now)}


def compute_usd(s: Session, campaign: Campaign, pools: Mapping[str, Pool], now: datetime) -> float | None:
    """What the jobs of ``campaign`` cost on its workers as of ``now``, at the prices of ``pools``. None: unknown."""
    mine = _leases_of(campaign)
    ours = set(s.scalars(mine))
    # every lease on a worker was acquired: the coalesces only tell the type checker so
    rows = s.execute(
        select(Lease.id, func.coalesce(Lease.worker, ''), func.coalesce(Lease.acquired_at, Lease.created_at))
        .add_columns(Lease.ended_at)
        .where(Lease.worker.in_(select(Lease.worker).where(Lease.id.in_(mine))))
    )
    held: dict[str, list[Holding]] = defaultdict(list)
    for lease, worker, start, end in rows:
        held[worker].append((start, end or now, lease in ours))
    of = _pool_of(s, set(held))
    usd = 0.0
    for worker, jobs in held.items():
        if (pool := pools.get(of.get(worker, ''))) is None or pool.usd_per_hour is None:
            return None
        usd += _share(jobs) / 3600 * pool.usd_per_hour
    return usd


def _leases_of(campaign: Campaign) -> Select[int]:
    """Select the ids of the campaign's leases: of its agents, and of the evaluations of its candidates."""
    agents = select(AgentSession.id).where(AgentSession.campaign_id == campaign.id)
    candidates = select(Candidate.id).where(Candidate.campaign_id == campaign.id)
    evaluations = select(func.json_extract(Event.payload, '$.lease')).where(
        has(LeaseQueued), Event.target_kind == CANDIDATE, Event.target_id.in_(candidates)
    )
    return select(Lease.id).where(or_(Lease.agent_session_id.in_(agents), Lease.id.in_(evaluations)))


def _pool_of(s: Session, workers: set[str]) -> dict[str, str]:
    """The pool of each worker: that of its instance, which outlives it, else that of its row."""
    joined = s.execute(select(LeaseWorker.id, LeaseWorker.pool).where(LeaseWorker.id.in_(workers)))
    provisioned = s.execute(select(WorkerInstance.name, WorkerInstance.pool).where(WorkerInstance.name.in_(workers)))
    return {**dict(joined.all()), **dict(provisioned.all())}


def _share(jobs: list[Holding]) -> float:
    """The seconds of one worker that the campaign's jobs held: each second split evenly among the jobs then."""
    edges = sorted((at, step, mine) for start, end, mine in jobs for at, step in ((start, 1), (end, -1)))
    seconds, running, ours = 0.0, 0, 0
    for (at, step, mine), (later, _, _) in pairwise(edges):
        running, ours = running + step, ours + step * mine
        if running:
            seconds += (later - at).total_seconds() * ours / running
    return seconds


def _pools() -> Mapping[str, Pool]:
    """The pools of the home's executor, with their prices: none for an executor without pools."""
    match EXECUTORS.selected(settings.providers):
        case Workers(pools=pools):
            return pools
        case _:
            return {}
