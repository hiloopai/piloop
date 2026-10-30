"""What the ``optimization`` campaign type does in the daemon's campaigns' pass (``Planner``).

Its run queue, its runs' ends and its measurements.
"""

from collections.abc import Collection
from concurrent.futures import Future
from datetime import datetime
from functools import partial
from typing import ClassVar, Literal

from sqlalchemy import Boolean, ColumnElement, func, select
from sqlalchemy.orm import Session

from piloop.actor import System
from piloop.campaign.launches import choose_harness, full_harnesses, remove_checkouts
from piloop.db.events import emit, latest
from piloop.db.models import LIVE, AgentSession, Campaign, Event, EventModel, ReadBy, count
from piloop.ids import AgentSessionId, RoleName, TargetKind, TargetRef
from piloop.plugins.campaign_type.contract import CannotLaunch, Steering, Take, TypeHost
from piloop.plugins.campaign_type.kit.backlog.ideas import claimable, held
from piloop.plugins.campaign_type.kit.backlog.ids import IDEA, IdeaStatus
from piloop.plugins.campaign_type.kit.backlog.tables import Idea
from piloop.plugins.campaign_type.kit.runs.controller import RUNS, close_run, handed_off
from piloop.plugins.campaign_type.kit.runs.ids import OPEN_RUNS, RUN, RunEnd, RunStatus
from piloop.plugins.campaign_type.kit.runs.ops import RunEndRequested, end_requested
from piloop.plugins.campaign_type.kit.runs.stop import release
from piloop.plugins.campaign_type.kit.runs.tables import Run, campaign_runs, run_of
from piloop.plugins.campaign_type.kit.verdicts.judge import due, head, head_unmeasured, rejudges
from piloop.plugins.fact_source import facts
from piloop.plugins.fact_source.facts import Facts
from piloop.plugins.role.contract import ImplementerRole
from piloop.plugins.trigger.contract import Backlog

NO_RUN_ERROR = 'an implementer needs a run; the scheduler requests them for free slots'


class RunQueued(EventModel):
    """The daemon queued a fresh run, on the next harness of the rotation. Its worker claims its idea."""

    READ_BY: ClassVar[ReadBy] = ReadBy.READERS
    kind: Literal['run.queued'] = 'run.queued'
    harness: str


EventModel.retire('run.rerouted')  # 0.5 moved a queued run to another harness; a run keeps the one it was queued on


def done_by(role: RoleName) -> ColumnElement[bool]:
    """Select the runs of ``role``.

    ``likely()`` tells SQLite that most runs match, as most runs are the implementer's. Without it, SQLite drives a join
    with ``ideas`` from ``runs`` and scans every run of every campaign.
    """
    return func.likely(Run.role == role, type_=Boolean)


def free_slots(s: Session, campaign: Campaign, roles: dict[RoleName, ImplementerRole], runs_left: int | None) -> int:
    """Return the runs that ``roles`` can start now.

    For each role, this is its ``max_concurrent`` minus its open runs. The sum is never more than the runs that the
    stop rule leaves (``runs_left``).
    """
    open_runs = campaign_runs(campaign.id).where(Run.status.in_(OPEN_RUNS))
    free = sum(max(role.max_concurrent - count(s, open_runs.where(done_by(name))), 0) for name, role in roles.items())
    return max(free if runs_left is None else min(free, runs_left), 0)


class OptimizationPlanner:
    """The ``optimization`` type at work in one daemon process: the hooks of the campaigns' pass (``TypeHost``)."""

    def __init__(self, host: TypeHost) -> None:
        self._host = host
        self._tracer = host.tracer
        self._controller = host.controller
        self._scoring: dict[tuple[TargetKind, int], Future[None]] = {}
        """Scoring outside an implementer: a run's resumed scoring by run id, or a campaign's re-measure by campaign id."""

    def drain(self, s: Session, campaign: Campaign) -> None:
        """Fail the campaign's queued runs at its stop: their requested implementers are closed by the pass."""
        queued = (Run.campaign_id == campaign.id, Run.status == RunStatus.QUEUED)
        for run in s.scalars(select(Run).where(*queued)):
            self._fail_run(s, run, RunEnd.CAMPAIGN_STOPPED)

    def ended(self, s: Session, agent_session: AgentSession, end: RunEnd) -> None:
        """A requested agent closed without starting, for ``end``: its queued run fails."""
        if run_of(agent_session) is not None:
            self._fail_run(s, s.get_one(Run, run_of(agent_session)), end)

    def take(self, s: Session, agent_session: AgentSession, full: set[str]) -> Take | CannotLaunch:
        """Whether a requested implementer launches now: one without a run is refused, and one at its harness's cap waits."""
        if run_of(agent_session) is None:
            return CannotLaunch(NO_RUN_ERROR)
        return Take.WAIT if agent_session.harness in full else Take.LAUNCH

    def fail_open(self, s: Session, end: RunEnd, *, reaped: bool, sparing: Collection[AgentSessionId]) -> None:
        """End each run in flight as ``end``, and its claim, but those of the agents of ``sparing``.

        Queued runs are not changed: they are requests. With ``reaped``, their worktrees are removed: a previous daemon
        left them. Otherwise a live run thread removes its own worktrees in its ``finally`` block: removing a worktree
        while an evaluator still runs in it is worse than leaving it a moment longer.
        """
        spared = AgentSession.id.in_(sparing)
        launched = select(AgentSession.subject_id).where(
            AgentSession.state.in_(LIVE), (AgentSession.subject_kind == RUN), ~spared
        )
        kept = select(AgentSession.subject_id).where(spared, (AgentSession.subject_kind == RUN))
        in_flight = (Run.status == RunStatus.RUNNING) & Run.id.not_in(kept)
        for run in s.scalars(select(Run).where(in_flight | Run.id.in_(launched))).all():
            self._fail_run(s, run, end)
            if reaped:
                remove_checkouts(self._host.home / RUNS / str(run.id), run.campaign, self._tracer)

    def steered_by(self, s: Session, agent_session: AgentSession) -> Steering | None:
        """An implementer's run, and its idea once it claimed one, since the run started: it signs as ``run#<id>`` too."""
        run = s.get(Run, run_of(agent_session)) if run_of(agent_session) is not None else None
        if run is None:
            return None
        targets = (TargetRef(RUN, run.id), *(() if run.idea_id is None else (TargetRef(IDEA, run.idea_id),)))
        since = run.started_at or agent_session.started_at
        assert since is not None, 'a running implementer started'
        return Steering(targets, since, (f'run#{run.id}',))

    def ended_at(self, s: Session, agent_session: AgentSession) -> tuple[datetime, RunEnd] | None:
        """When the implementer ended its run (``piloop run end``), and how the run ends, or None while it did not."""
        run_id = run_of(agent_session)
        assert run_id is not None
        found = latest(s, RunEndRequested, Event.target == TargetRef(RUN, run_id))
        return None if found is None else (found[0].at, handed_off(found[1])[0])

    def resume_unscored(self) -> None:
        """Score each closed run whose commits were not all scored (``runs.scoring``): a previous daemon stopped first."""
        with self._controller.launcher.db.session() as s:
            unscored = s.scalars(select(Run.id).where(Run.scoring).order_by(Run.id)).all()
        for run_id in unscored:
            self._tracer.event('run.unscored', run=run_id)
            self._scoring[RUN, run_id] = self._host.submit(partial(self._controller.resume, run_id))

    def in_flight(self) -> dict[str, Future[None]]:
        """The scoring in flight, by what it scores, after it forgets what ended. Scoring never raises."""
        self._scoring = {key: fut for key, fut in self._scoring.items() if not fut.done()}
        return {f'{kind}#{i}': fut for (kind, i), fut in self._scoring.items()}

    def _fail_run(self, s: Session, run: Run, end: RunEnd) -> None:
        """End ``run`` as ``end``, and its claim (``release``).

        A running run whose worker ended it is not failed: it is handed off (``_hand_off``).
        """
        if run.status is RunStatus.RUNNING and (ending := end_requested(s, run)) is not None:
            self._hand_off(s, run, ending)
            return
        release(s, close_run(s, run, end, None), end, None)
        self._tracer.event('run.aborted', run=run.id, end=end)

    def _hand_off(self, s: Session, run: Run, ending: RunEndRequested) -> None:
        """Close a run whose worker ended it as its end says, and leave its commits to be scored on the next start.

        The worker ended its run. A job of its own may still keep the run open, but the run is not cut: its idea stays
        ``claimed``, so no second run redoes the work. The run thread pins the commits
        (``RunController._keep_cut_work``), and ``reap`` scores them and settles the idea (``runs.scoring``). Its status is
        what the run's end would give (``run_outcome``).
        """
        end, error = handed_off(ending)
        close_run(s, run, end, error).scoring = True
        self._tracer.event('run.handed_off', run=run.id, end=end)

    def remeasure(self, s: Session, campaign: Campaign) -> None:
        """Measure what the current evaluator must measure (``ledger.due``), one candidate at a time per campaign.

        After an evaluator change, no candidate has a score from the new evaluator. The head is measured first, and runs
        that are not pinned wait until it has a score (``_queue``). Then the bars that runs need, and the candidates
        that someone asked to measure again. Older keeps are measured again only when someone asks. A candidate that
        someone measured by hand is judged again first (``rejudges``), in the same slot: a rebase can measure its merge.
        """
        key = (TargetKind.CAMPAIGN, campaign.id)
        if (busy := self._scoring.get(key)) is not None and not busy.done():
            return
        if again := rejudges(s, campaign):
            self._tracer.event('candidate.rejudge', campaign=campaign.name, candidate=again[0].id, left=len(again))
            self._scoring[key] = self._host.submit(partial(self._controller.rejudge, again[0].id))
            return
        if not (todo := due(s, campaign)):
            return
        self._tracer.event('candidate.remeasure', campaign=campaign.name, candidate=todo[0].id, left=len(todo))
        self._scoring[key] = self._host.submit(partial(self._controller.remeasure, todo[0].id))

    def backlog(self, s: Session, campaign: Campaign, name: RoleName, left: Facts, buffer: int) -> Backlog:
        """Return the campaign's backlog as role ``name`` fills it: ``buffer`` beyond the run slots, its own sweeps.

        It is never more than the runs that the stop rule ``left``.
        """
        cfg = campaign.config
        target = cfg.run_slots + buffer
        if (runs_left := left.get(facts.RUNS)) is not None:
            target = min(target, runs_left)
        sweeps = count(
            s,
            select(AgentSession).where(
                AgentSession.campaign_id == campaign.id,
                AgentSession.kind == name,
                AgentSession.state.in_(LIVE),
            ),
        )
        mine = select(Idea).where(Idea.campaign_id == campaign.id)
        return Backlog(
            runnable=count(s, mine.where(claimable())),
            waiting=count(s, mine.where((Idea.status == IdeaStatus.OPEN) & held())),
            target=target,
            free=free_slots(s, campaign, cfg.roles.doing_runs(), left.get(facts.RUNS)),
            sweeps=sweeps,
            per_sweep=cfg.roles.sweeping()[name].max_ideas,
            on_hold=head_unmeasured(s, campaign),
        )

    def queue(self, s: Session, campaign: Campaign, left: Facts) -> None:
        """Start a fresh run, and request its worker, for each free slot within the runs ``left``.

        A run starts from the head, with no idea: its worker claims one from the board, or proposes one and claims it.
        Each role that does runs fills its own slots (``max_concurrent``), in the order of ``Roles.doing_runs``, on the
        next harness of its rotation that is not at its cap. Nothing starts while the campaign has no head, or while a
        new evaluator has not measured the head (``remeasure`` measures it first).
        """
        trunk = head(s, campaign)
        if trunk is None or head_unmeasured(s, campaign):
            return
        cfg = campaign.config
        runs_left = left.get(facts.RUNS)
        for name, role in cfg.roles.doing_runs().items():
            for _ in range(free_slots(s, campaign, {name: role}, runs_left)):
                total_runs = count(s, campaign_runs(campaign.id).where(done_by(name)))
                harness = choose_harness(cfg, name, full_harnesses(s, campaign, cfg), total_runs)
                if harness is None:
                    break
                model = cfg.role_models(name)[harness]
                run = Run(campaign=campaign, role=name, base_candidate_id=trunk.id, harness=harness, model=model)
                s.add(run)
                s.flush()
                emit(s, RunQueued(harness=harness), TargetRef(RUN, run.id), System.DAEMON)
                s.add(
                    AgentSession(
                        kind=name,
                        campaign_id=campaign.id,
                        subject=TargetRef(RUN, run.id),
                        harness=harness,
                        model=model,
                        reason='a free slot',
                    )
                )
                runs_left = None if runs_left is None else runs_left - 1
