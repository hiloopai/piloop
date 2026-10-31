"""The campaign type capability's machinery: the pass over the campaigns, and the agents that it launches.

The pass has two phases. First, each campaign's type and the triggers of its roles
(https://piloop.autoresearch.inc/concepts/roles) insert ``agent_sessions`` rows in state ``requested``. Then the
requested rows launch in id order, as far as the role's ``max_concurrent`` allows, while the executor has a free agent
slot (``Executor.capacity``). The home's launch policy picks the campaign that each free slot goes to, and a
campaign's harness choice the harness of each agent. Anyone can insert a requested row by hand, for example the CLI or an agent. Such a row launches in
the same second phase.
"""

import itertools
import os
from collections import Counter
from collections.abc import Callable, Collection, Mapping, Sequence
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from typing import ClassVar, Literal, assert_never

from pydantic import ValidationError
from sqlalchemy import Select, delete, exists, select
from sqlalchemy.orm import Session

from piloop.actor import Agent, System
from piloop.agents.results import (
    AgentSessionFinished,
    AgentSessionRunning,
    AgentSessionStarted,
    StartupAnomaly,
)
from piloop.agents.roles import briefing, compose_brief, compose_resumed, render_sections, role_of
from piloop.campaign import escalations
from piloop.campaign.campaigns import WORKSPACE, CampaignAdded, workspace_dir
from piloop.campaign.launches import choose_harness, close, earlier, full_harnesses, remove_checkouts, resumed_on
from piloop.campaign.stop import (
    CampaignClosed,
    CancelRequested,
    StopRequested,
    agent_cancel,
    answer_done,
    asked,
    check as check_stop,
    escalated,
    held as held_now,
    hours_due,
    left as left_now,
    requested,
    review_skipped,
    timeout as agent_timeout,
    unclosed,
)
from piloop.config import CampaignConfig
from piloop.daemon.loop import Consumer, Kinds, Phase, Soonest, Turn
from piloop.db.events import AnomalyReported, emit, has, in_order
from piloop.db.models import (
    LIVE,
    NOT_RETIRED,
    OPEN,
    AgentSession,
    AgentSessionState,
    Campaign,
    Comment,
    Event,
    EventModel,
    Pause,
    ReadBy,
    count,
    utcnow,
)
from piloop.db.triggers import DAEMON_ACTOR
from piloop.db.triggers.changelog import (
    AgentSessionRequested,
    CampaignConfigChanged,
    CampaignPausedChanged,
    CampaignStatusChanged,
    CandidateDeleted,
    IdeaCreated,
    IdeaDeleted,
    IdeaLinked,
    IdeaPriorityChanged,
    IdeaStatusChanged,
    IdeaTrackChanged,
    OriginChanged,
    VerdictChanged,
)
from piloop.ids import AgentSessionId, CampaignStatus, CommentId, CommentKind, RoleName, TargetKind, TargetRef
from piloop.plugins.campaign_type.contract import CAMPAIGNS, CannotLaunch, Planner, Take
from piloop.plugins.campaign_type.kit.backlog.ideas import added_by
from piloop.plugins.campaign_type.kit.candidates.references import CandidateImported
from piloop.plugins.campaign_type.kit.evaluation.measure import MeasurementRecorded, RemeasureRequested, score
from piloop.plugins.campaign_type.kit.evaluation.submissions import Submitted
from piloop.plugins.campaign_type.kit.evaluation.tables import Evaluation
from piloop.plugins.campaign_type.kit.runs.controller import (
    RUNS,
    RunFinished,
)
from piloop.plugins.campaign_type.kit.runs.ids import RunEnd, RunId
from piloop.plugins.campaign_type.kit.runs.stop import runs_without_keep, settled
from piloop.plugins.campaign_type.kit.runs.tables import run_of
from piloop.plugins.campaign_type.kit.verdicts.judge import (
    CandidateJudged,
    Rebased,
    RejudgeRequested,
    checkout_candidate,
)
from piloop.plugins.contribution import Contribution, DaemonHost, Leaving, closers
from piloop.plugins.escalation.contract import Choice, EscalationView
from piloop.plugins.executor.consumers import LIMITS
from piloop.plugins.executor.contract import AgentKey, LeftJob
from piloop.plugins.fact_source.contract import FACT_SOURCES
from piloop.plugins.fact_source.facts import Facts
from piloop.plugins.host import ContractBroken
from piloop.plugins.launch_policy.contract import LAUNCH_POLICIES, LaunchCase, LaunchPolicy, Share
from piloop.plugins.role.contract import PUSHED, Continuity, ImplementerRole, SweepRole
from piloop.plugins.runtime.contract import handle_for, runtime_for
from piloop.plugins.section.contract import Rendering
from piloop.plugins.stop_rule.contract import GO, STOP_RULES, Go, Hold, Stop, WhileHeld
from piloop.plugins.trigger.contract import TRIGGERS, Backlog, Fire, Quiet, Steer
from piloop.plugins.trigger.kit.incidents import assigned

AGENT_SESSIONS = 'agent_sessions'
"""The directory under the home of each sweep's place: ``agent_sessions/<id>``."""
EventModel.retire('agent_session.jobs_left')  # 0.2.0 listed the jobs of an idle implementer; its finding ends it now


class LaunchPaused(EventModel):
    """The startup breaker paused a campaign's launches."""

    READ_BY: ClassVar[ReadBy] = ReadBy.READERS
    kind: Literal['launch.paused'] = 'launch.paused'
    comment: CommentId
    """The escalation."""
    deaths: int
    load: str
    """The load average when launches paused."""


class LaunchResumed(EventModel):
    """A campaign's paused launches resumed. It ends the pause that ``LaunchPaused`` started."""

    READ_BY: ClassVar[ReadBy] = ReadBy.READERS
    kind: Literal['launch.resumed'] = 'launch.resumed'
    comment: CommentId
    """The escalation of the pause."""


CAMPAIGN_STATE: Kinds = frozenset(
    {
        CampaignAdded,
        CampaignStatusChanged,
        CampaignPausedChanged,
        CampaignConfigChanged,
        StopRequested,
        AgentSessionRequested,
        AgentSessionStarted,
        AgentSessionRunning,
        AgentSessionFinished,
        StartupAnomaly,
        RunFinished,
        IdeaCreated,
        IdeaStatusChanged,
        IdeaPriorityChanged,
        IdeaTrackChanged,
        IdeaLinked,
        IdeaDeleted,
        CandidateJudged,
        VerdictChanged,
        OriginChanged,
        CandidateDeleted,
        CandidateImported,
        Rebased,
        RemeasureRequested,
        RejudgeRequested,
        MeasurementRecorded,
        Submitted,
        *LIMITS,
    }
)
"""What the pass over the campaigns reads: their ideas, runs, candidates and agents, their status and config, and the
slots, with the executor's ``capacity_events``. Every fact of the ``campaign`` and ``optimization`` sources changes with one of these, or with time (``hours_due``); a reading of a
source that reads has its own due."""


def campaign_kinds() -> Kinds:
    """What wakes the pass over the campaigns.

    Their state, what each trigger reads beyond it (``WAKES_ON``), and what a fact source writes when it reads
    (``CHANGES``).
    """
    return CAMPAIGN_STATE.union(
        *(p.model.WAKES_ON for p in TRIGGERS.providers().values()),
        *(p.model.CHANGES for p in FACT_SOURCES.providers().values()),
    )


class Campaigns:
    """The pass over the campaigns, and the agents that it launches: the campaign type capability's machinery.

    One instance per daemon. Its consumer runs the pass (``run``), and its lifecycle takes over what a previous daemon
    process left (``start``) and writes what is open when this one stops (``stop``).
    """

    def __init__(self, host: DaemonHost) -> None:
        self._host = host
        self._db, self._tracer, self._home = host.db, host.tracer, host.home
        self._agents = host.agents
        self._launch_policy = LAUNCH_POLICIES.selected(host.plugins)
        self._controller = host.controller
        self._runs_dir = host.home / RUNS
        self._agent_sessions_dir = host.home / AGENT_SESSIONS
        self._runtime_factory = host.runtime_factory
        self._types = host.planners
        self._futures: dict[AgentSessionId, Future[None]] = {}
        """The thread of each agent that this daemon process launched or adopted."""
        self._soonest = Soonest()
        """The earliest due that the pass found so far."""
        self.launched: list[AgentSessionId] = []
        """What the latest pass launched."""

    def in_flight(self) -> dict[str, Future[None]]:
        """The threads of the agents in flight, after it forgets those that ended."""
        self._collect()
        return {f'agent_session#{i}': f for i, f in self._futures.items()}

    def stop(self, leaving: Leaving, timeout: float) -> None:
        """Write what is still open when the daemon stops for good, as ``start`` does for a crashed daemon.

        The executor stopped first, and the daemon waited for the threads in flight. An agent that started and did not
        report in time is left with its run to the next daemon, which applies its report, as after a crash: it may
        carry saved work. At an in-place restart, nothing is written: the next daemon process adopts the agents.
        """
        match leaving:
            case Leaving.STOP:
                self._collect()
                with self._db.session() as s:
                    live = s.scalars(select(AgentSession).where(AgentSession.state.in_(LIVE))).all()
                    unreported = [w.id for w in live if w.handle]
                self._fail_open(RunEnd.DAEMON_STOPPED, reaped=False, sparing=unreported)
            case Leaving.HAND_OVER:
                pass
            case _:
                assert_never(leaving)

    def start(self) -> None:
        """Hand the jobs that a previous daemon process left to the executor (``Executor.adopt``). Fail the rest.

        The executor takes over what it can find: ``workers`` gives every live lease a full period, so nothing is queued
        again only because of the restart, and its worker re-attaches to each agent that still has its worktree, and
        reports the rest gone, which ends their runs ``worker_lost``, not as attempts. For each agent it adopts,
        the daemon waits for its end as if no daemon had stopped (``RunController.reattach``, or ``_reattach_sweep`` for
        a sweep). Both an in-place restart (``hand_over``) and a crash leave agents for this. An agent that it does not
        adopt is killed, and its row and its run fail. A closed run whose commits were not all scored
        (``runs.scoring``) is scored now (``RunController.resume``), whatever its idea's status. An idea that stayed
        ``scheduled`` does not run again, and no dependent idea takes it before its keeps are known. The worktrees of
        failed runs and sweeps are removed. The evaluation queue starts empty: its jobs were the previous daemon's, and
        what still needs a measurement asks again.
        """
        with self._tracer.span('piloop.reap'):
            # only reads: a job of an adopted agent stores its linked inputs, which can take a while, and a write
            # lock held meanwhile would make every command that writes fail with "database is locked"
            with self._db.session() as s:
                live = s.scalars(select(AgentSession).where(AgentSession.state.in_(LIVE))).all()
                runs = {w.id: run_of(w) for w in live}
                left = [LeftJob(AgentKey(w.id)) for w in live]
            with self._db.session() as s:
                s.execute(delete(Evaluation))
                s.commit()
            refused = set(self._controller.executor.adopt(left))
            adopted = [
                job.key.agent_session for job in left if isinstance(job.key, AgentKey) and job.key not in refused
            ]
            self._fail_open(RunEnd.STALE, reaped=True, sparing=adopted)
            for agent_session_id in adopted:
                run_id = runs[agent_session_id]
                self._tracer.event('agent_session.reattached', agent_session=agent_session_id, run=run_id)
                body = (
                    partial(self._reattach_sweep, agent_session_id)
                    if run_id is None
                    else partial(self._controller.reattach, run_id, agent_session_id)
                )
                self._futures[agent_session_id] = self._host.thread(body)
            for planner in self._types.values():
                planner.resume_unscored()

    def _fail_open(self, end: RunEnd, *, reaped: bool, sparing: Collection[AgentSessionId] = ()) -> None:
        """End each run in flight as ``end`` and queue its idea again. Close each live agent except ``sparing``.

        Requested implementers and their queued runs are not changed, because they are requests, not processes. An idea
        that is ``scheduled`` without an open run stays so. Its run finished and only its scoring stopped, which
        ``reap`` resumes. With ``reaped``, the rows are those that a previous daemon left and its executor did not
        adopt: their agents are killed (``_kill_stale``), and their worktrees removed. Otherwise the executor stopped
        its jobs, and a live run thread removes its own worktrees in its ``finally`` block: removing a worktree while
        an evaluator still runs in it is worse than leaving it a moment longer.
        """
        with self._db.session() as s:
            spared = AgentSession.id.in_(sparing)
            for planner in self._types.values():
                planner.fail_open(s, end, reaped=reaped, sparing=sparing)
            for agent_session in s.scalars(select(AgentSession).where(AgentSession.state.in_(LIVE), ~spared)):
                # a ``starting`` row that its daemon left before its start window has no process
                if reaped and agent_session.runtime is not None:
                    self._kill_stale(s, agent_session)
                if reaped and run_of(agent_session) is None and agent_session.campaign_id is not None:
                    campaign = s.get_one(Campaign, agent_session.campaign_id)
                    remove_checkouts(self._agent_sessions_dir / str(agent_session.id), campaign, self._tracer)
                close(s, agent_session, end, self._tracer)
            s.commit()

    def _kill_stale(self, s: Session, agent_session: AgentSession) -> None:
        """Ask the runtime to kill an agent that no executor adopted, which outlives its daemon. Best effort.

        Its job ended with no report, so no executor stops it.
        """
        try:
            runtime_for(agent_session, self._runtime_factory).kill(handle_for(agent_session))
        except Exception as e:  # The agent can be gone already. The database row is what matters.
            self._tracer.event('agent_session.kill_failed', agent_session=agent_session.id, error=str(e))

    def run(self, turn: Turn) -> datetime | None:
        """Pass over every campaign: its stop rule, its remeasures, its triggers, and the launches.

        Returns when a pass must come for time alone: the earliest ``due_in`` of a trigger, the end of the hours that the
        stop rule leaves or of a startup pause, or the next reading of a fact source.

        A stopped campaign requests nothing and is measured again only on request (``ledger.due``). The pass asks the
        stop rule (``piloop.campaign.stop.check``) of each active campaign, and of each stopped one whose
        closing review waits for a hold (``stop.review_waits``). A campaign that it holds gets only the first step. A
        paused campaign (``piloop pause``) gets no triggers, does not stop at a limit of its stop rule, and launches
        nothing, and with ``all`` it is not measured again either. A stop request stops a held or paused campaign too.
        A stopped campaign launches its closing review, and what a human requests, whatever the pause; while the stop
        rule holds, as its ``while_held`` says (``launches``).
        """
        self._collect()
        self._soonest = Soonest()
        launching: list[Launching] = []
        with self._db.session() as s:
            for campaign in s.scalars(select(Campaign).where(NOT_RETIRED)).all():
                try:
                    cfg = campaign.config
                except ValidationError as e:
                    self._tracer.event('campaign.invalid_config', campaign=campaign.name, error=str(e))
                    continue
                answer = escalated(s, campaign, cfg, check_stop(s, campaign, cfg)) if asked(s, campaign, cfg) else GO
                self._soonest.add(hours_due(campaign, answer, turn.now))
                held = answer.reason if isinstance(answer, Hold) else None
                if held is None and campaign.paused is not Pause.ALL:
                    self._planner(campaign).remeasure(s, campaign)
                if campaign.status is CampaignStatus.ACTIVE:
                    # A stop request wins over a hold and a pause.
                    if (stop := requested(s, campaign)) is not None:
                        self._triggers(s, campaign, cfg, stop, held, turn.now)
                    elif not isinstance(answer, Hold) and campaign.paused is None:
                        self._triggers(s, campaign, cfg, answer, None, turn.now)
                s.flush()
                if campaign.status is CampaignStatus.STOPPED and unclosed(s, campaign) and settled(s, campaign):
                    self._close_campaign(s, campaign, cfg, cfg.roles.sweeping(), turn.now)
                    held = held_now(s, campaign, cfg)  # a closing review may wait for the hold
                if campaign.status is CampaignStatus.RETIRED:
                    # A feed read before the pass's first write (``read_transaction``) reloads what the pass read, so
                    # a retire that committed since shows here.
                    continue
                phase = quiet_phase(s, campaign, held)
                awake = awake_roles(cfg, phase)
                if phase is not None and awake:
                    for name in awake:
                        self._request(
                            s, campaign, cfg, name, cfg.roles.sweeping()[name], turn.now, Facts(), quiet=phase
                        )
                if launches(campaign, cfg, held):
                    launching.append(Launching(campaign, cfg, None))
                elif awake:
                    launching.append(Launching(campaign, cfg, awake))
            launched = self._launch(s, launching, turn.now)
            s.commit()
        unsubmitted = self._controller.launcher.unsubmitted
        for agent_session_id, run_id in launched:
            unsubmitted.add(agent_session_id)
            body = (
                partial(self._controller.execute, run_id, agent_session_id)
                if run_id
                else partial(self._sweep, agent_session_id)
            )
            future = self._futures[agent_session_id] = self._host.thread(body)
            # a body that ends before it submits (a run that fails first) must not hold the slot
            future.add_done_callback(lambda _, i=agent_session_id: unsubmitted.discard(i))
            future.add_done_callback(self._ended)
        self.launched = [agent_session_id for agent_session_id, _ in launched]
        return self._soonest.at

    def _planner(self, campaign: Campaign) -> Planner:
        """The campaign's type at work."""
        return self._types[campaign.type]

    def _ended(self, _future: Future[None]) -> None:
        """A thread of an agent or of scoring ended: the next turn forgets it and fills its slot."""
        self._host.wake(CAMPAIGNS)

    def _triggers(
        self, s: Session, campaign: Campaign, cfg: CampaignConfig, answer: Go | Stop, held: str | None, now: datetime
    ) -> None:
        """Stop the campaign if the answer is a stop. Otherwise queue runs and check every role's triggers.

        ``held`` is why the stop rule holds launches: then only a stop request gets here. A stop rule with
        ``while_held = "skip"`` then closes the campaign with no closing review (``CampaignClosed``) and posts an
        escalation.

        Triggers requested everything that is pending when the campaign stops, so it is all cancelled. Requested rows
        are closed and queued runs end ``RunEnd.CAMPAIGN_STOPPED``. Their ideas go back to ``ready``. Implementers in
        flight are told to stop (``_wind_down``), and their commits are scored. Then the daemon closes the campaign
        (``_close_campaign``). A stopped campaign gets nothing more from the triggers. Rows that a human inserts after
        the stop still launch.
        """
        if isinstance(answer, Go):
            self._planner(campaign).queue(s, campaign, answer.left)
            s.flush()
            left = left_now(s, campaign, cfg)  # the runs just queued use up what the rule left
            for name, role in cfg.roles.sweeping().items():
                self._request(s, campaign, cfg, name, role, now, left)
            return
        reason = answer.reason
        campaign.status, campaign.stopped_reason = CampaignStatus.STOPPED, reason
        self._tracer.event('campaign.stopped', campaign=campaign.name, reason=reason)
        self._drain(s, campaign)
        s.flush()
        answer_done(s)  # a stop with ``now`` waits for ``_wind_down`` to end the running implementers
        if held is not None and STOP_RULES.selected(cfg.plugins).while_held is WhileHeld.SKIP:
            review_skipped(s, campaign, held)
            self._close_campaign(s, campaign, cfg, {}, now)

    def _close_campaign(
        self, s: Session, campaign: Campaign, cfg: CampaignConfig, sweeping: Mapping[RoleName, SweepRole], now: datetime
    ) -> None:
        """Close a stopped campaign: each provider that ``Closes`` does its share, then the triggers of ``sweeping`` fire.

        The pass closes a campaign that the daemon stopped once it ``settled``, with every sweeping role: by default,
        the watchdog's closing review, which can then search the campaign's reports. A stop rule with
        ``while_held = "skip"`` closes it at the stop, with none. The stop and the close are events (``unclosed``), so a daemon that
        restarts in between still closes it, once.
        """
        for provider, closer in closers(cfg.plugins).items():
            result = closer.closing(s, campaign)
            self._tracer.event('campaign.closing', campaign=campaign.name, provider=provider, result=result)
        for name, role in sweeping.items():
            self._request(s, campaign, cfg, name, role, now, Facts(), stopping=True)
        s.flush()  # the status change's event first: the close comes after the stop
        emit(s, CampaignClosed(), TargetRef(TargetKind.CAMPAIGN, campaign.id), System.DAEMON)

    def _drain(self, s: Session, campaign: Campaign) -> None:
        self._planner(campaign).drain(s, campaign)
        for agent_session in s.scalars(
            select(AgentSession).where(
                AgentSession.campaign_id == campaign.id, AgentSession.state == AgentSessionState.REQUESTED
            )
        ):
            close(s, agent_session, RunEnd.CAMPAIGN_STOPPED, self._tracer)

    def _request(
        self,
        s: Session,
        campaign: Campaign,
        cfg: CampaignConfig,
        name: RoleName,
        role: SweepRole,
        now: datetime,
        left: Facts,
        *,
        stopping: bool = False,
        quiet: Quiet | None = None,
    ) -> None:
        """Insert requested rows when the role's triggers fire, and write the notes of those that steer.

        The triggers fire in the order of the role's list (``piloop.plugins.trigger``): those with ``ON_STOP`` when the
        campaign is ``stopping``, else the others, and in a ``quiet`` phase only those that name it
        (``Trigger.WHILE``). While a request of the role waits, a fire requests nothing more. A fire that needs a row of
        its own (``Fire.alone``) shares its row only with the other such fires, when one comes first, and otherwise
        waits for a later pass. The others share one batch: the largest count of rows, with every reason. The first row
        gets the events of the fires. Then each note of a trigger that steers goes on the role's open row
        (``_steer``). A trigger that raises is traced and does not fire. The ``due_in`` of each trigger that did not
        fire goes into the pass's due.
        """
        latest = s.scalars(
            select(AgentSession)
            .where(AgentSession.campaign_id == campaign.id, AgentSession.kind == name)
            .order_by(AgentSession.id.desc())
            .limit(1)
        ).first()
        waits = latest is not None and latest.state is AgentSessionState.REQUESTED
        p = self._pass(s, campaign, cfg, name, role, now, left, quiet)
        fires: list[Fire] = []
        steers: list[Steer] = []
        triggers = cfg.triggers_of(name)
        for trigger in (t for t in triggers if t.ON_STOP is stopping and (quiet is None or quiet in t.WHILE)):
            try:
                fire = trigger.fire(p)
                due = trigger.due_in(p) if fire is None else None
            except Exception as e:  # A trigger of a package, or a query that the schema no longer runs.
                self._tracer.event(
                    'trigger.failed', campaign=campaign.name, role=name, trigger=TRIGGERS.name_of(trigger), error=str(e)
                )
                continue
            match fire:
                case Steer():
                    steers.append(fire)
                case Fire() if waits:  # one pending request per role is enough
                    pass
                case Fire():
                    fires.append(fire)
                case None:
                    self._soonest.add(None if due is None else now + due)
                case _:
                    assert_never(fire)
        if fires:
            self._fire(s, campaign, name, fires)
        for steer in steers:
            self._steer(s, p, steer)

    def _fire(self, s: Session, campaign: Campaign, name: RoleName, fires: list[Fire]) -> None:
        """Insert the requested rows of ``fires``: those of the first's kind (``Fire.alone``), in one batch."""
        fires = [f for f in fires if f.alone is fires[0].alone]
        count = max(f.count for f in fires)
        reason = '; '.join(f.reason for f in fires)
        rows = [AgentSession(kind=name, campaign_id=campaign.id, reason=reason) for _ in range(count)]
        s.add_all(rows)
        s.flush()
        for fire in fires:
            for event in fire.events:
                emit(s, event, TargetRef(TargetKind.AGENT_SESSION, rows[0].id), System.DAEMON)
        self._tracer.event('agent_session.requested', campaign=campaign.name, kind=name, reason=reason, count=count)

    def _steer(self, s: Session, p: RolePass, steer: Steer) -> None:
        """Write ``steer`` as a message to the role's newest open row, or to a row requested for it, and its events on it."""
        row = s.scalars(p.mine().where(AgentSession.state.in_(OPEN)).order_by(AgentSession.id.desc()).limit(1)).first()
        if row is None:
            row = AgentSession(kind=p.role, campaign_id=p.campaign.id, reason=steer.reason)
            s.add(row)
            s.flush()
            self._tracer.event(
                'agent_session.requested', campaign=p.campaign.name, kind=p.role, reason=steer.reason, count=1
            )
        message = Comment(
            target=TargetRef(TargetKind.AGENT_SESSION, row.id),
            author=DAEMON_ACTOR,
            kind=CommentKind.MESSAGE,
            body=steer.text,
        )
        s.add(message)
        s.flush()
        for event in steer.events:
            emit(s, event, TargetRef(TargetKind.COMMENT, message.id), System.DAEMON)
        self._tracer.event('agent_session.steered', campaign=p.campaign.name, agent_session=row.id, comment=message.id)

    def _pass(
        self,
        s: Session,
        campaign: Campaign,
        cfg: CampaignConfig,
        name: RoleName,
        role: SweepRole,
        now: datetime,
        left: Facts,
        quiet: Quiet | None = None,
    ) -> RolePass:
        """Return what the triggers of role ``name`` read in this pass, with what the stop rule ``left``."""
        last = s.scalars(
            select(AgentSession)
            .where(AgentSession.campaign_id == campaign.id, AgentSession.kind == name)
            .order_by(AgentSession.id.desc())
            .limit(1)
        ).first()
        return RolePass(
            s=s,
            now=now,
            campaign=campaign,
            role=name,
            last=last,
            quiet=quiet,
            max_concurrent=role.max_concurrent,
            interval=role.interval,
            backlog=partial(self._planner(campaign).backlog, s, campaign, name, left),
            without_keep=partial(runs_without_keep, s, campaign),
        )

    def _launch(
        self, s: Session, launching: Sequence[Launching], now: datetime
    ) -> list[tuple[AgentSessionId, RunId | None]]:
        """Take the requested agents of ``launching`` while the executor has a free agent slot (``_take``).

        The home's launch policy picks the campaign that each slot goes to. Without a bound on the slots, the campaign
        it picks takes all it can. A row whose campaign was retired since the pass read it is closed instead
        (``_unretired``). Returns ``(agent_session_id, run_id)`` pairs.

        Raises:
            ContractBroken: The policy picked a campaign that waits for no slot.
        """
        # before the executor's count: a job submitted in between then counts twice, for one pass, never in neither
        unsubmitted = len(self._controller.launcher.unsubmitted)
        free = self._controller.executor.capacity().agents
        if free is not None:
            free -= unsubmitted
        rows = s.execute(
            select(Campaign.id, Campaign.name).join(AgentSession).where(AgentSession.state.in_(LIVE))
        ).all()
        live = Counter(campaign_id for campaign_id, _ in rows)
        names = {campaign_id: name for campaign_id, name in rows} | {e.campaign.id: e.campaign.name for e in launching}
        left = {entry.campaign.id: entry for entry in launching}
        taken: list[tuple[AgentSessionId, RunId | None]] = []
        while left and (free is None or free > 0):
            shares = tuple(Share(c, names[c], live[c], c in left) for c in sorted(live.keys() | left.keys()))
            if (picked := self._launch_policy.pick(LaunchCase(free, shares))) is None:
                break
            if (nxt := left.get(picked)) is None:
                raise ContractBroken(
                    f'launch policy {type(self._launch_policy).__name__} picked campaign {picked}, which waits for no'
                    ' slot'
                )
            got = self._take(s, nxt.campaign, nxt.cfg, now, most=None if free is None else 1, roles=nxt.roles)
            if free is None or not got:
                del left[picked]
            else:
                free -= len(got)
            live[picked] += len(got)
            taken += got
        return self._unretired(s, taken)

    def _unretired(
        self, s: Session, taken: list[tuple[AgentSessionId, RunId | None]]
    ) -> list[tuple[AgentSessionId, RunId | None]]:
        """``taken`` without the rows of a campaign that is retired now, which are closed.

        The pass read its campaigns before it held the write lock, so a retire can commit in between. The flush takes
        the lock, so the read after it is the last word: no retire commits until the pass does.
        """
        if not taken:
            return taken
        s.flush()
        ids = [agent_session_id for agent_session_id, _ in taken]
        retired = set(
            s.scalars(
                select(AgentSession.id)
                .join(Campaign, AgentSession.campaign_id == Campaign.id)
                .where(AgentSession.id.in_(ids), Campaign.status == CampaignStatus.RETIRED)
            )
        )
        for agent_session_id in retired:
            close(s, s.get_one(AgentSession, agent_session_id), 'its campaign is retired', self._tracer)
        return [pair for pair in taken if pair[0] not in retired]

    def _take(
        self,
        s: Session,
        campaign: Campaign,
        cfg: CampaignConfig,
        now: datetime,
        *,
        most: int | None = None,
        roles: frozenset[RoleName] | None = None,
    ) -> list[tuple[AgentSessionId, RunId | None]]:
        """Mark requested rows ``starting``, up to each role's ``max_concurrent``, and ``most`` in all.

        With ``roles``, only the rows of those roles: in a quiet phase, the roles awake in it (``awake_roles``).

        A run role's row without a run is refused.

        A sweep's harness is chosen here, by the campaign's harness choice (``choose_harness``), never a harness at its
        cap (``full_harnesses``). A sweep pinned to such a harness stays requested. A run's harness was chosen when it was queued.
        A request for a role that ``piloop.toml`` no longer has is refused, and its queued run fails
        (``RunEnd.ROLE_REMOVED``), so that its idea goes back to the queue.

        Returns ``(agent_session_id, run_id)`` pairs. The run is what an implementer executes. Nothing is taken while the startup
        breaker holds (``_paused``).
        """
        taken: list[tuple[AgentSessionId, RunId | None]] = []
        if (resumes := self._paused(s, campaign, now)) is not None:
            self._soonest.add(resumes)
            return taken
        all_roles = cfg.roles.all()
        unknown = select(AgentSession).where(
            AgentSession.campaign_id == campaign.id,
            AgentSession.state == AgentSessionState.REQUESTED,
            AgentSession.kind.not_in(all_roles),
        )
        for agent_session in s.scalars(unknown):
            close(s, agent_session, f'no [plugins.role.{agent_session.kind}] in piloop.toml', self._tracer)
            self._planner(campaign).ended(s, agent_session, RunEnd.ROLE_REMOVED)
        full = full_harnesses(s, campaign, cfg)
        for kind, rc in all_roles.items():
            if roles is not None and kind not in roles:
                continue
            mine = (AgentSession.campaign_id == campaign.id, AgentSession.kind == kind)
            active = count(s, select(AgentSession).where(*mine, AgentSession.state.in_(LIVE)))
            requested = s.scalars(
                select(AgentSession)
                .where(*mine, AgentSession.state == AgentSessionState.REQUESTED)
                .order_by(AgentSession.id)
                .limit(max(rc.max_concurrent - active, 0))
            ).all()
            rotation = cfg.role_harnesses(kind)
            for agent_session in requested:
                if most is not None and len(taken) >= most:
                    return taken
                if isinstance(rc, ImplementerRole):
                    taking = self._planner(campaign).take(s, agent_session, full)
                    match taking:
                        case CannotLaunch(reason=reason):
                            close(s, agent_session, reason, self._tracer)
                            continue
                        case Take.WAIT:
                            continue
                        case Take.LAUNCH:
                            pass
                        case _:
                            assert_never(taking)
                else:
                    harness = agent_session.harness or choose_harness(
                        cfg,
                        kind,
                        full,
                        earlier(s, agent_session),
                        resumed_on(s, campaign.id, kind, rc, rotation, agent_session.id),
                    )
                    if harness is None or harness in full:
                        continue
                    agent_session.harness = harness
                agent_session.state = AgentSessionState.STARTING
                taken.append((agent_session.id, run_of(agent_session)))
                full = full_harnesses(s, campaign, cfg)
        return taken

    def _paused(self, s: Session, campaign: Campaign, now: datetime) -> datetime | None:
        """Return when launches resume if ``startup_breaker`` agents failed at startup in ``startup_window``, else None.

        Requested rows wait, and launch when those failures are older than the window. The window is the back-off.
        Each pause posts one escalation (priority 1) with the load average and the failures' errors, which end with
        the agents' last screens. The escalation is updated in place while the pause holds, so an acknowledgement
        means that the human saw it. A new escalation is posted only after launches resume and pause again.
        """
        agent_sessions = select(AgentSession.id).where(AgentSession.campaign_id == campaign.id)
        deaths = in_order(
            s,
            StartupAnomaly,
            Event.target_kind == TargetKind.AGENT_SESSION,
            Event.target_id.in_(agent_sessions),
            Event.at >= now - self._launch_policy.startup_window,
        )
        on_campaign = TargetRef(TargetKind.CAMPAIGN, campaign.id)
        latest = s.scalars(
            select(Event)
            .where(has(LaunchPaused) | has(LaunchResumed), Event.target == on_campaign)
            .order_by(Event.id.desc())
            .limit(1)
        ).first()
        pause = latest.payload if latest is not None and isinstance(latest.payload, LaunchPaused) else None
        if len(deaths) < self._launch_policy.startup_breaker:
            if pause is not None:
                emit(s, LaunchResumed(comment=pause.comment), on_campaign, System.DAEMON)
            return None
        escalation = s.get(Comment, pause.comment) if pause is not None else None
        if pause is None or escalation is None:
            self._escalate_pause(s, campaign, deaths)
        else:
            escalations.revise(s, escalation, pause_asked(deaths, pause.load, self._launch_policy))
        return deaths[-self._launch_policy.startup_breaker][0].at + self._launch_policy.startup_window

    def _escalate_pause(self, s: Session, campaign: Campaign, deaths: Sequence[tuple[Event, StartupAnomaly]]) -> None:
        load = ' / '.join(f'{x:.1f}' for x in os.getloadavg())
        comment = escalations.escalate(
            s,
            TargetRef(TargetKind.CAMPAIGN, campaign.id),
            pause_asked(deaths, load, self._launch_policy),
            author=System.DAEMON,
            priority=1,
        )
        paused = LaunchPaused(comment=comment.id, deaths=len(deaths), load=load)
        emit(s, paused, TargetRef(TargetKind.CAMPAIGN, campaign.id), System.DAEMON)
        self._tracer.event(paused.kind, campaign=campaign.name, deaths=len(deaths), load=load)

    def _sweep(self, agent_session_id: AgentSessionId) -> None:
        """Run a sweep, an agent without a run, in a worktree at the head, with a brief made from its row.

        The brief shows the role's sections (``[plugins.role.<name>] sections``). Its ``context`` section ends with the
        sweep's lines: the checkout, the workspace, the role's own (``SweepRole.context``), then the notes on the row so
        far, which it gets here and not again. A sweep that got anomalies is
        an incident sweep: its sections read them (``Rendering.given``), and the brief adds the role's incident prompt.
        Every other sweep's brief adds the role's routine prompt. The watchdog's are its incident sweep and its
        progress review. Nothing that a sweep commits is scored. In a worktree at the head, the commits are removed
        with the worktree when the agent ends.
        """
        with self._db.session() as s:
            agent_session = s.get_one(AgentSession, agent_session_id)
            campaign = s.get_one(Campaign, agent_session.campaign_id)
            cfg = campaign.config
            role = role_of(s, agent_session)
            table = cfg.roles.sweeping()[agent_session.kind]
            timeout = agent_timeout(s, campaign, cfg, table.timeout, utcnow())
            if isinstance(timeout, Stop):  # requested again: the pass that stops the campaign closes it
                agent_session.state = AgentSessionState.REQUESTED
                s.commit()
                self._tracer.event('agent_session.not_started', agent_session=agent_session_id, reason=timeout.reason)
                return
            base = checkout_candidate(s, campaign)
            values = ', '.join(
                f'{t.primary.name}={"unmeasured" if (v := score(base, campaign.evaluator_id, t.primary)) is None else v}'
                for t in cfg.all_tracks
            )
            now = utcnow()
            given = tuple(assigned(s, agent_session_id))
            notes = s.scalars(
                select(Comment)
                .where(Comment.target == TargetRef(TargetKind.AGENT_SESSION, agent_session_id))
                .where(Comment.kind == PUSHED)
                .order_by(Comment.id)
            ).all()
            role_pass = self._pass(s, campaign, cfg, agent_session.kind, table, now, left_now(s, campaign, cfg))
            sweep = SweepPass(**vars(role_pass), agent_session=agent_session)
            workspace = workspace_dir(self._home, campaign.id)
            lines = [
                f'- Your cwd is a detached checkout of candidate#{base.id}, the head ({values}): read and run the'
                ' code, try things. Nothing you commit here is scored or kept; it goes with the checkout when you end.',
                f'- Shared workspace for notes and artifacts to other agents: `./{WORKSPACE}/` (not committed). The files'
                ' of a done idea are archived, and removed from it after some days or when the disk is short; `piloop'
                f' restore {campaign.name} <idea id>|<workspace path>` writes them back.',
                *table.context(sweep),
                *(['', '# Notes', *(f'{n.body}\n' for n in notes)] if notes else []),
                f'- Sweep started at: {now:%Y-%m-%d %H:%M:%S} (UTC; the sections below are as of then).',
            ]
            agent = Agent(role=agent_session.kind, session=agent_session_id)
            brief_of = briefing(role, campaign, agent, reason=agent_session.reason or 'requested', lines=lines)
            rendering = Rendering(s, now, campaign, workspace, self._controller.cas, given, brief=brief_of)
            sections = render_sections(cfg.sections_of(agent_session.kind), rendering)
            brief = compose_brief(role, campaign, sections.whole, incident=bool(given))
            match table.continuity:
                case Continuity.RESUME:
                    resumed = compose_resumed(table, agent, sections.resumed)
                case Continuity.FRESH:
                    resumed = None
                case _:
                    assert_never(table.continuity)
        sources = self._controller.sources  # outside the session: it puts the linked inputs into the CAS
        artifact = sources.agent(campaign, base.revision)
        place = sweep_place(agent_session_id)
        cas = self._controller.cas
        self._controller.launcher.launch(
            agent_session_id,
            brief=cas.put(brief.encode()),
            place=place,
            artifact=artifact,
            timeout=timeout,
            resumed_brief=None if resumed is None else cas.put(resumed.encode()),
            read=[n.id for n in notes],
        )

    def _reattach_sweep(self, agent_session_id: AgentSessionId) -> None:
        """Wait for the agent of a sweep that a previous daemon process started, until it ends."""
        self._controller.launcher.reattach(agent_session_id)

    def _collect(self) -> None:
        """Forget finished threads. If a thread died before its launch recorded anything, close its row here."""
        for agent_session_id, fut in list(self._futures.items()):
            if fut.done():
                del self._futures[agent_session_id]
                if (exc := fut.exception()) is not None:
                    self._tracer.event('agent_session.thread_died', agent_session=agent_session_id, error=str(exc))
                    self._close_dead(agent_session_id, f'{type(exc).__name__}: {exc}')

    def _close_dead(self, agent_session_id: AgentSessionId, error: str) -> None:
        with self._db.session() as s:
            close(s, s.get_one(AgentSession, agent_session_id), error, self._tracer)
            s.commit()


@dataclass(frozen=True)
class RolePass:
    """The ``Pass`` of one sweeping role of one campaign: what its triggers read in this pass."""

    s: Session
    now: datetime
    campaign: Campaign
    role: RoleName
    last: AgentSession | None
    quiet: Quiet | None
    max_concurrent: int
    interval: float
    backlog: Callable[[int], Backlog]
    without_keep: Callable[[datetime | None], int]

    def mine(self) -> Select[AgentSession]:
        """The role's rows in the campaign."""
        return select(AgentSession).where(AgentSession.campaign_id == self.campaign.id, AgentSession.kind == self.role)

    def live(self) -> int:
        return count(self.s, self.mine().where(AgentSession.state.in_(LIVE)))

    def killed_streak(self) -> int:
        states = self.s.scalars(self.mine().with_only_columns(AgentSession.state).order_by(AgentSession.id.desc()))
        return len(list(itertools.takewhile(lambda state: state is AgentSessionState.KILLED, states)))

    def cancel(self, agent_session: AgentSession, /) -> CancelRequested | None:
        cancel = agent_cancel(self.s, agent_session)
        return cancel[1] if cancel is not None else None

    def empty_sweeps(self, states: tuple[AgentSessionState, ...]) -> list[AgentSession]:
        """See ``Pass.empty_sweeps``.

        The new-results back-off of ``low_water`` skips killed sweeps. A killed sweep stopped early, so its empty
        result says nothing about what was left to propose. The idle rest counts killed sweeps. Otherwise, a harness
        that dies every time would be launched again on every pass.
        """
        signed = added_by(self.s, self.campaign.id)
        ended = self.s.scalars(self.mine().where(AgentSession.state.in_(states)).order_by(AgentSession.id.desc()))
        return list(itertools.takewhile(lambda agent_session: f'{self.role}#{agent_session.id}' not in signed, ended))

    def routine(self) -> AgentSession | None:
        given = exists().where(
            has(AnomalyReported), Event.target_kind == TargetKind.AGENT_SESSION, Event.target_id == AgentSession.id
        )
        return self.s.scalars(self.mine().where(~given).order_by(AgentSession.id.desc()).limit(1)).first()


@dataclass(frozen=True)
class SweepPass(RolePass):
    """The ``Sweep`` of one agent of a sweeping role: its role's ``Pass``, and its row."""

    agent_session: AgentSession


def sweep_place(agent_session_id: AgentSessionId) -> str:
    """The place of a sweep under the daemon's home: its log directory, with its worktree in it."""
    return f'{AGENT_SESSIONS}/{agent_session_id}'


def pause_body(deaths: Sequence[tuple[Event, StartupAnomaly]], load: str, policy: LaunchPolicy) -> str:
    """Format the pause escalation: the number of failures, the load average and the latest failures' errors."""
    minutes = policy.startup_window.total_seconds() / 60
    lines = [
        f'Launches paused: {len(deaths)} agents died at startup in the last {minutes:g} minutes. Load average'
        f' {load} on {os.cpu_count()} CPUs when launches paused. Launches resume automatically when fewer than'
        f' {policy.startup_breaker} deaths are that recent. If the host is overloaded, stop what overloads it. The latest'
        ' deaths:',
        *(
            f'- {e.target} ({death.role} on {death.harness}): {death.error}'
            for e, death in deaths[-policy.startup_breaker :]
        ),
    ]
    return '\n'.join(lines)


def pause_asked(deaths: Sequence[tuple[Event, StartupAnomaly]], load: str, policy: LaunchPolicy) -> EscalationView:
    """What the escalation of a launch pause asks: what to do while agents die at startup, with ``pause_body``."""
    minutes = policy.startup_window.total_seconds() / 60
    return EscalationView(
        headline='Agents fail as they start, so new launches are paused. What do you do?',
        why=f'{len(deaths)} agents failed in the last {minutes:g} minutes. The host may be too busy.',
        choices=(
            Choice(id='A', label='Wait.', result='Launches start again when the failures are older.'),
            Choice(id='B', label='Stop what makes the host busy.', result='Launches start again sooner.'),
        ),
        details=pause_body(deaths, load, policy),
    )


@dataclass(frozen=True)
class Launching:
    """A campaign whose requested agents the pass launches: every role's, or only ``roles`` in a quiet phase."""

    campaign: Campaign
    cfg: CampaignConfig
    roles: frozenset[RoleName] | None


def quiet_phase(s: Session, campaign: Campaign, held: str | None) -> Quiet | None:
    """The quiet phase of ``campaign``, given why the stop rule holds it (``held``), or None while it runs.

    A stopped campaign is quiet once its closing review was requested.
    """
    match campaign.status:
        case CampaignStatus.STOPPED:
            return None if unclosed(s, campaign) else Quiet.STOPPED
        case CampaignStatus.ACTIVE if held is not None:
            return Quiet.HELD
        case CampaignStatus.ACTIVE if campaign.paused is not None:
            return Quiet.PAUSED
        case CampaignStatus.ACTIVE:
            return None
        case CampaignStatus.RETIRED:
            raise ValueError(f'{campaign.name} is retired: the pass over the campaigns leaves it out')
        case _:
            assert_never(campaign.status)


def awake_roles(cfg: CampaignConfig, phase: Quiet | None) -> frozenset[RoleName]:
    """The sweeping roles with a trigger that names ``phase`` (``Trigger.WHILE``). None of them while the campaign runs."""
    if phase is None:
        return frozenset()
    return frozenset(name for name in cfg.roles.sweeping() if any(phase in t.WHILE for t in cfg.triggers_of(name)))


def launches(campaign: Campaign, cfg: CampaignConfig, held: str | None) -> bool:
    """Whether the pass launches the campaign's requested agents, given why the stop rule holds (``held``).

    An active campaign launches when nothing holds it: no hold and no pause. A stopped campaign launches its closing
    review and what a human requests, paused or not. While the stop rule holds, it launches unless the rule's
    ``while_held`` is ``wait``.
    """
    if campaign.status is CampaignStatus.ACTIVE:
        return held is None and campaign.paused is None
    return held is None or STOP_RULES.selected(cfg.plugins).while_held is not WhileHeld.WAIT


def contribution(host: DaemonHost) -> Contribution:
    """The consumer ``campaigns``, last in a turn: it plans from what every other consumer left, and launches."""
    campaigns = Campaigns(host)
    wakes = campaign_kinds() | host.controller.executor.capacity_events()
    return Contribution(consumers=(Consumer(CAMPAIGNS, wakes, campaigns.run, Phase.PLAN),), lifecycle=campaigns)
