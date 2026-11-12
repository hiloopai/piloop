"""Execute one run: make a worktree, run the worker, close the run, score its commits and end its claim.

The run closes when its implementer ends, so the implementer slot is free while the daemon measures the commits. The idea stays
``claimed`` until its candidates are scored.
"""

import math
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from functools import partial
from pathlib import Path
from typing import ClassVar, Literal, assert_never

from covalence import Cid
from sqlalchemy import func, select
from sqlalchemy.orm import Session, undefer

from piloop.actor import Agent, System
from piloop.agents.launcher import Launcher
from piloop.agents.results import (
    AGENT_SESSION_STATES,
    AgentEnded,
    CampaignResults,
    DaemonStopped,
    HandedOver,
    Lost,
    NotStarted,
    Ran,
    WorkUnsaved,
    end_agent,
    finish,
)
from piloop.agents.roles import briefing, compose_brief, compose_resumed, read_brief, render_sections, role
from piloop.campaign.campaigns import (
    WORKSPACE,
    Sources,
    checkout_of,
    record_of,
    vcs_of,
    workspace_dir,
)
from piloop.campaign.dirs import campaign_dir
from piloop.campaign.stop import run_cancel, seconds, timeout as agent_timeout
from piloop.config import TRANSCRIPT, CampaignConfig
from piloop.db.events import Topic, emit, has, latest
from piloop.db.machine import Machine, Refused, transition
from piloop.db.models import (
    LIVE,
    AgentSession,
    AgentSessionState,
    Campaign,
    Event,
    EventModel,
    ReadBy,
    RepoTransfer,
    utcnow,
)
from piloop.db.requests import RequestAnswered
from piloop.db.session import Database, write_transaction
from piloop.db.triggers.changelog import (
    AgentSessionRequested,
    EvaluationCancelled,
    EvaluationDeleted,
    EvaluationRequeued,
)
from piloop.ids import (
    AgentSessionId,
    CampaignId,
    CampaignStatus,
    Revision,
    RoleName,
    TargetKind,
    TargetRef,
)
from piloop.net.tokens import Tokens
from piloop.otel import Span, Tracer
from piloop.plugins.campaign_type.contract import CAMPAIGN_TYPES
from piloop.plugins.campaign_type.kit.backlog.tables import Idea
from piloop.plugins.campaign_type.kit.candidates.ids import CANDIDATE, CandidateId, Origin
from piloop.plugins.campaign_type.kit.candidates.references import resolve
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.evaluation import queue, submissions, task_holds
from piloop.plugins.campaign_type.kit.evaluation.evaluate import Score
from piloop.plugins.campaign_type.kit.evaluation.ids import EvaluatorId, MeasurementId
from piloop.plugins.campaign_type.kit.evaluation.jobs import (
    EVALUATION,
    EvaluationJob,
    InfraFailed,
    Sample,
    not_started,
    record,
    unmade,
)
from piloop.plugins.campaign_type.kit.evaluation.measure import (
    Split,
    current_evaluator,
    gate_of,
    remeasure_asked,
    retired,
    score,
    sealed_evaluator,
    status,
)
from piloop.plugins.campaign_type.kit.evaluation.ops import hold_on_failed_baseline
from piloop.plugins.campaign_type.kit.evaluation.submissions import Submitted, submitted
from piloop.plugins.campaign_type.kit.evaluation.tables import Evaluation, Evaluator, Measurement
from piloop.plugins.campaign_type.kit.pool.due import firsts
from piloop.plugins.campaign_type.kit.pool.pool import (
    UNSCORED,
    Pool,
    baseline,
    branching,
    confirm_runs,
    criterion,
    track_of,
)
from piloop.plugins.campaign_type.kit.pool.scored import CandidateScored, record_scored
from piloop.plugins.campaign_type.kit.runs.ids import FINISHED_RUNS, OPEN_RUNS, RUN, RunEnd, RunEnding, RunId, RunStatus
from piloop.plugins.campaign_type.kit.runs.ops import RunEndRequested, end_requested
from piloop.plugins.campaign_type.kit.runs.tables import Run, working_on
from piloop.plugins.cas.contract import CAS, Cas
from piloop.plugins.executor.contract import (
    DEADLINE_ENV,
    AgentKey,
    Executor,
    ExecutorConfig,
    Host,
    JobKey,
    Source,
)
from piloop.plugins.network.contract import NETWORKS, Network, NetworkHost
from piloop.plugins.role.contract import Continuity
from piloop.plugins.runtime.contract import ExitStatus, RuntimeFactory, make_runtime
from piloop.plugins.secrets.contract import SECRETS, HomeSecrets, SecretsProvider
from piloop.plugins.section.contract import Rendering
from piloop.plugins.stop_rule.contract import Stop
from piloop.plugins.vcs.contract import Pin, Repository, Transfer, Version
from piloop.settings import AgentSettings, EvaluationSettings, StorageSettings

UNSAVED = 'what the implementer left unsaved could not be saved, agent_session.work_unsaved has it in the CAS'
RUNS = 'runs'
SETTLES = tuple(Topic(m) for m in (Submitted, CandidateScored, EvaluationCancelled, EvaluationDeleted, RequestAnswered))
"""What can settle a submission (``submissions.unscored``): its score, or a cancel of its evaluation or remeasure."""
"""The directory under the home of each run's place: ``runs/<id>``, its implementer's log directory and worktree."""


EventModel.retire('idea.recovery_requeued')  # 0.5 requeued an idea from a cut run's commits; a claim ends instead


class RunStarted(EventModel):
    """A run's implementer was launched, with its brief: the run is ``running``."""

    READ_BY: ClassVar[ReadBy] = ReadBy.READERS
    kind: Literal['run.started'] = 'run.started'
    harness: str
    model: str


class RunRelaunched(EventModel):
    """A run's implementer failed to start, and the daemon launched another."""

    READ_BY: ClassVar[ReadBy] = ReadBy.READERS
    kind: Literal['run.relaunched'] = 'run.relaunched'
    agent_session: AgentSessionId
    """The implementer that failed to start."""
    launches: int


class RunReceived(EventModel):
    """The run's versions came into the repo of record, oldest first: what ``resume`` scores after a stop."""

    READ_BY: ClassVar[ReadBy] = ReadBy.READERS
    kind: Literal['run.received'] = 'run.received'
    revisions: list[Revision]


EventModel.retire('comment.finding')  # 0.5's kind of a worker's report; it is a note now, and old rows stay as they are


class RunFinished(EventModel):
    """A run closed (``close_run``): how it ended, and the text that says why."""

    kind: Literal['run.finished'] = 'run.finished'
    end: RunEnd
    error: str | None


RUN_STATES: Machine[RunStatus] = Machine(
    Run.status,
    {
        RunStatus.RUNNING: frozenset({RunStatus.QUEUED}),
        RunStatus.QUEUED: frozenset({RunStatus.RUNNING}),
        **dict.fromkeys(FINISHED_RUNS, frozenset(OPEN_RUNS)),
    },
    RUN,
)
"""A run's status: it starts (``RunStarted``), is queued again after a failed start (``RunRelaunched``), and closes
once (``close_run``). A closed run never opens again."""


class Measuring(StrEnum):
    """How a request for a measurement ended (``RunController._measure``)."""

    MEASURED = 'measured'
    """A measurement of it is recorded: a score, or a crash."""
    CANCELLED = 'cancelled'
    """Someone cancelled it (``queue.cancel``): no measurement."""
    STOPPED = 'daemon_stopped'
    """The daemon stopped: the next one asks again."""
    REPLACED = 'replaced'
    """Its campaign measures with another evaluator now (``queue.withdraw``): no measurement, and the current evaluator
    measures it instead."""
    ABANDONED = 'abandoned'
    """It failed ``[evaluation] stop_after_tries`` times in a row for a cause that is not its candidate's: no measurement,
    and evaluations wait until the affected task or campaign resumes."""


type Halted = Literal[Measuring.CANCELLED, Measuring.STOPPED, Measuring.ABANDONED]
"""Why a run's candidate has no measurement to score it on."""


class RunController:
    """Run one ``Run`` to completion on the calling thread. ``Launcher.launch`` runs the agent.

    It owns the daemon's executor (``[plugins.executor]``), which runs every agent and every evaluation, and the core
    operations that apply their results (``CampaignResults``).
    """

    def __init__(
        self,
        db: Database,
        tracer: Tracer,
        home: Path,
        runtime_factory: RuntimeFactory = make_runtime,
        *,
        executor: ExecutorConfig,
        cas: Cas | None = None,
        reserve: int = StorageSettings().reserve,
        evaluation: EvaluationSettings | None = None,
        agents: AgentSettings | None = None,
        network: Network | None = None,
        secrets: SecretsProvider | None = None,
        wake: Callable[[str], None] = lambda _name: None,
    ) -> None:
        """``wake`` runs a consumer of the daemon's loop by its name (``Host.wake``)."""
        self._db = db
        self._tracer = tracer
        self._runs_dir = home / RUNS
        self._home = home
        self._evaluation = evaluation if evaluation is not None else EvaluationSettings()
        self._agents = agents if agents is not None else AgentSettings()
        self.cas = cas if cas is not None else CAS.selected({}).open(home)
        self.sources = Sources(self.cas, home, db)
        """The artifacts of jobs: a campaign's repo of record, and its linked inputs in the CAS."""
        self._config = executor
        """The executor's ``[plugins.executor]``, as ``config.toml`` has it."""
        secrets = secrets if secrets is not None else SECRETS.selected({})
        results = CampaignResults(
            db,
            tracer,
            home,
            self.cas,
            secrets=secrets,
            exit_output_chars=self._agents.exit_output_chars,
            startup_seconds=self._agents.startup_seconds,
            startup_turns=self._agents.startup_turns,
        )
        if network is None:
            network = NETWORKS.selected({}).open(NetworkHost(Tokens(db), HomeSecrets(db, secrets)))
        host = Host(
            db,
            results,
            tracer,
            home,
            self.cas,
            partial(checkout_of, db),
            partial(record_of, db),
            runtime_factory,
            reserve,
            network,
            secrets,
            wake,
        )
        self.executor: Executor[ExecutorConfig] = self._config.open(host)
        self.launcher = Launcher(db, tracer, home, self.executor)
        self._jobs: dict[int, EvaluationJob] = {}
        """The job of each evaluation that this daemon asked for, by its queue row, until the row goes: a lost one is
        submitted again. A row without one is another daemon's: a reap drops it, and what needs it asks again."""

    def _wait(self, evaluation_id: int, candidate_id: CandidateId) -> bool:
        """Wait until the evaluation leaves the queue, and return True; False once the daemon stops.

        While it waits for a slot, and one is free, it submits the next ones (``dispatch``): a lost evaluation waits
        again (``EvaluationRequeued``), and nothing else may end to free a slot for it. It reads the free slots after
        its session: the executor reads in a session of its own, and one thread never holds two connections
        (``Database``). The events of its candidate's evaluations wake it, and those that say that the executor has
        more slots (``Executor.capacity_events``). Another evaluation's end frees a slot too: its own request
        dispatches then.
        """
        on = TargetRef(CANDIDATE, candidate_id)
        mine = [Topic(EvaluationDeleted, target=on), Topic(EvaluationRequeued, target=on)]
        with self._db.watching([*mine, *(Topic(m) for m in self.executor.capacity_events())]) as watch:
            while not self.launcher.cancelled.is_set() and not self._db.closed:
                with self._db.session() as s:
                    if (evaluation := s.get(Evaluation, evaluation_id)) is None:
                        return True
                    waiting = evaluation.started_at is None
                if waiting and ((free := self.executor.capacity().free(EVALUATION)) is None or free > 0):
                    self.dispatch()
                else:
                    watch.wait(math.inf)
        return False

    def dispatch(self) -> None:
        """Submit the next evaluations of the queue while the executor has free slots, in the queue's order.

        The order and the fairness are the queue's (``queue.waiting``), whichever the executor. Called after each
        request, after each evaluation that ends, after each infra error, and on each pass. It first takes what waits
        under a retired evaluator from the queue (``queue.withdraw``): its requests ask the current one.
        """
        with self.launcher.lock:
            if self.launcher.cancelled.is_set():
                return
            free = self.executor.capacity().free(EVALUATION)
            with self._db.session() as s:
                write_transaction(s)
                queue.withdraw(s)
                starting: list[EvaluationJob] = []
                for evaluation in queue.waiting(s):
                    if free is not None and len(starting) >= free:
                        break
                    if (job := self._jobs.get(evaluation.id)) is not None and queue.start(s, evaluation.id):
                        starting.append(job)
                s.commit()
            for job in starting:
                try:
                    self.executor.submit(job)
                except Exception as e:  # any refusal: the row must not run with no job, so it leaves the queue
                    with self._db.session() as s:
                        not_started(s, job, f'{type(e).__name__}: {e}')
                        s.commit()
                    self._tracer.event('evaluation.not_started', evaluation=job.evaluation, error=str(e))

    def stop(self, timeout: float) -> None:
        """Submit no more jobs, and stop the executor's: each still reports. Waits up to ``timeout`` for them.

        Nothing submits a job after this (``launcher.lock``).
        """
        self.launcher.stop()
        self.executor.stop(timeout)

    def stop_cancelled(self) -> None:
        """Cancel each running evaluation that someone cancelled (``piloop eval cancel``).

        Its executor stops the evaluator, and the result records nothing (``queue.cancel`` recorded the crash).
        """
        with self._db.session() as s:
            cancelled = s.scalars(
                select(Evaluation.id).where(Evaluation.cancelled_by.is_not(None), Evaluation.started_at.is_not(None))
            ).all()
        for evaluation_id in cancelled:
            self.executor.cancel(JobKey(EVALUATION, evaluation_id))

    def hand_over(self) -> None:
        """Stop for an in-place restart. The agents keep running for the next daemon, whose executor adopts them."""
        self.launcher.handover.set()
        self.launcher.cancel()
        self.executor.hand_over()

    def close(self) -> None:
        """Release the executor. The daemon calls it last, before it closes the database."""
        self.executor.close()

    def execute(self, run_id: RunId, agent_session_id: AgentSessionId) -> None:
        """Make the worktree, run the implementer, evaluate and record. Never raises: failures are recorded on the run."""
        self._guarded(run_id, agent_session_id, lambda span: self._execute(run_id, agent_session_id, span))

    def reattach(self, run_id: RunId, agent_session_id: AgentSessionId) -> None:
        """Finish a run whose agent a previous daemon process started (``Launcher.reattach``). Never raises.

        The daemon watches the agent in the run's worktree until it ends. Then it scores and settles the run as
        ``execute`` does.
        """
        self._guarded(run_id, agent_session_id, lambda span: self._finish(run_id, agent_session_id, None, span))

    def _guarded(self, run_id: RunId, agent_session_id: AgentSessionId, body: Callable[[Span], None]) -> None:
        """Run ``body``. A failure fails the run, and closes its implementer, or ends it once its job was submitted.

        A failure before the launch is no attempt at the idea (``RunEnd.NOT_STARTED``): its agent never ran. One after
        it ends the agent through its executor, as nothing waits for it any more.
        """
        with self._tracer.span('piloop.run', run=run_id) as span:
            try:
                body(span)
            except Exception as e:
                span.error(e)
                error = f'{type(e).__name__}: {e}'
                with self._db.session() as s:
                    closed = _close_unstarted(s, agent_session_id, error)
                    agent = s.get_one(AgentSession, agent_session_id)
                    live = agent.state in LIVE
                    if live:
                        end_agent(s, agent, error, RunEnd.FAILED, self._tracer)
                    s.commit()
                if live:
                    self.executor.cancel(AgentKey(agent_session_id))
                end = RunEnd.NOT_STARTED if closed else RunEnd.FAILED
                self._close(span, run_id, end, error)
                self._settle(run_id, end, None)

    def _execute(self, run_id: RunId, agent_session_id: AgentSessionId, span: Span) -> None:
        with self.launcher.lock:
            if self.launcher.cancelled.is_set():  # Queued after stop(). The scheduler already failed the row.
                self._stopped(span, run_id)
                return
            with self._db.session() as s:
                run = s.get_one(Run, run_id)
                campaign = run.campaign
                cfg = campaign.config
                timeout = agent_timeout(s, campaign, cfg, cfg.role(run.role).timeout, utcnow())
                if isinstance(timeout, Stop):  # the run stays queued, its agent requested again: the stop closes both
                    agent = s.get_one(AgentSession, agent_session_id)
                    again = AgentSessionRequested(role=agent.kind, campaign=agent.campaign_id, reason=agent.reason)
                    try:
                        transition(
                            s, AGENT_SESSION_STATES, agent_session_id, AgentSessionState.REQUESTED, again, System.DAEMON
                        )
                    except Refused:  # a stop or a cancel ended it since the pass took it: it never starts
                        _close_unstarted(s, agent_session_id, timeout.reason)
                    s.commit()
                    self._tracer.event(
                        'agent_session.not_started', agent_session=agent_session_id, reason=timeout.reason
                    )
                    return
                base = s.get_one(Candidate, run.base_candidate_id)
                resumed: Cid | None = None
                if run.brief_cid is None:
                    agent = Agent(role=run.role, session=agent_session_id)
                    brief = briefing(
                        role(cfg, run.role, Path(campaign.path)),
                        campaign,
                        agent,
                        reason='',  # a free slot: nothing that the worker acts on
                        lines=self._context(s, cfg, run, base, agent, timeout),
                    )
                    workspace = workspace_dir(self._home, campaign.id)
                    rendering = Rendering(s, utcnow(), campaign, workspace, self.cas, brief=brief)
                    sections = render_sections(cfg.sections_of(RoleName(run.role)), rendering)
                    composed = compose_brief(role(cfg, run.role, Path(campaign.path)), campaign, sections.whole)
                    run.brief_cid = self.cas.put(composed.encode())
                    table = cfg.role(run.role)
                    match table.continuity:
                        case Continuity.RESUME:
                            resumed = self.cas.put(compose_resumed(table, agent, sections.resumed).encode())
                        case Continuity.FRESH:
                            pass
                        case _:
                            assert_never(table.continuity)
                brief = run.brief_cid
                assert brief is not None  # composed above, or by the launch that a relaunch follows
                try:
                    transition(
                        s,
                        RUN_STATES,
                        run_id,
                        RunStatus.RUNNING,
                        RunStarted(harness=run.harness, model=run.model),
                        System.DAEMON,
                        # A relaunch keeps the start time, so comments since then are sent to the new implementer again.
                        started_at=run.started_at or utcnow(),
                        log_dir=str(self._runs_dir / str(run_id)),
                    )
                except Refused as e:  # a stop closed the run after the pass took its implementer: nothing starts
                    s.rollback()
                    _close_unstarted(s, agent_session_id, str(e))
                    s.commit()
                    span.set(status='already_closed', found=e.found)
                    return
                s.commit()
        self._finish(run_id, agent_session_id, Start(brief, timeout, resumed), span)

    def _finish(self, run_id: RunId, agent_session_id: AgentSessionId, start: Start | None, span: Span) -> None:
        """Run the agent, then record the outcome, score the commits and settle the idea.

        With a ``start``, a new agent starts in a new worktree. With None, the daemon re-attaches to the agent that is
        already in the worktree. A handed-over agent leaves the worktree and the rows to the next daemon.
        """
        with self._db.session() as s:
            run = s.get_one(Run, run_id)
            campaign = run.campaign
            cfg = campaign.config
            base_revision = s.get_one(Candidate, run.base_candidate_id).revision
            role_timeout = cfg.role(run.role).timeout
            harness, model, campaign_id = run.harness, run.model, campaign.id
            name, vcs, campaign_path = campaign.name, vcs_of(campaign), Path(campaign.path)
        log_dir = self._runs_dir / str(run_id)
        with closing(vcs.open(campaign_path)) as repo:
            span.set(agent_session=agent_session_id, harness=harness, model=model)
            if start is None:
                ended = self.launcher.reattach(agent_session_id)
            else:
                ended = self.launcher.launch(
                    agent_session_id,
                    brief=start.brief,
                    place=run_place(run_id),
                    artifact=self.sources.agent(self._campaign(campaign_id), base_revision),
                    timeout=start.timeout,
                    capture=TargetRef(RUN, run_id),
                    resumed_brief=start.resumed,
                )
            # a daemon stop owns the rows, also when it came just after the report applied
            stopping = self.launcher.cancelled.is_set()
            match ended:
                case HandedOver():  # the next daemon waits for the agent again
                    span.set(status='handed_over')
                case DaemonStopped(work=work) | Ran(work=work) if stopping:
                    self._cut(span, run_id, agent_session_id, name, repo, base_revision, work)
                case NotStarted() | Lost() if stopping:
                    self._cut(span, run_id, agent_session_id, name, repo, base_revision, None)
                case DaemonStopped(work=work):  # the stop of an earlier daemon process, which ended before the rows
                    self._cut_by_earlier_stop(
                        span, run_id, campaign_id, agent_session_id, name, repo, base_revision, work
                    )
                case Lost(reason=reason):
                    self._close(span, run_id, RunEnd.WORKER_LOST, reason)
                    self._settle(run_id, RunEnd.WORKER_LOST, None)
                case NotStarted():
                    if not self._relaunch(span, run_id, agent_session_id, log_dir):
                        with self._db.session() as s:
                            end = RunEnd.CANCELLED if run_cancel(s, run_id) is not None else RunEnd.NOT_STARTED
                        self._close(span, run_id, end, None)
                        self._settle(run_id, end, None)
                case Ran():
                    self._ran(
                        span,
                        run_id,
                        campaign_id,
                        agent_session_id,
                        repo,
                        base_revision,
                        ended,
                        stop_cut=self._cut_by_stop(agent_session_id, role_timeout),
                    )
                case _:
                    assert_never(ended)

    def _cut_by_stop(self, agent_session_id: AgentSessionId, role_timeout: float) -> bool:
        """Whether the campaign's stop cut the agent's session shorter than its role timeout, as its deadline says.

        The deadline is the one of its launch: a daemon that re-attaches to the agent after the campaign stopped reads
        the same. An agent that 0.6 launched has none: it was not cut.
        """
        with self._db.session() as s:
            agent = s.get_one(AgentSession, agent_session_id)
            return agent.deadline is not None and (agent.deadline - agent.started_at).total_seconds() < role_timeout

    def _cut(
        self,
        span: Span,
        run_id: RunId,
        agent_session_id: AgentSessionId,
        name: str,
        repo: Repository,
        base_revision: Revision,
        work: Transfer | None,
    ) -> None:
        """Keep the work of a run that a daemon stop cut (``_keep_cut_work``). The stop writes the rows."""
        cut = self._ending(run_id) is None  # else the stop closed it as handed off
        self._keep_cut_work(run_id, name, repo, self._received(run_id, repo, base_revision, work), cut=cut)
        self._stopped(span, run_id)

    def _cut_by_earlier_stop(
        self,
        span: Span,
        run_id: RunId,
        campaign_id: CampaignId,
        agent_session_id: AgentSessionId,
        name: str,
        repo: Repository,
        base_revision: Revision,
        work: Transfer | None,
    ) -> None:
        """Keep the work of a run that the stop of an earlier daemon process cut, and write the rows that it did not.

        That process ended before it read the agent's report, so the run is still open. It ends as the stop would have
        ended it: ``RunEnd.DAEMON_STOPPED``, not an attempt, or handed off when its worker ended it, whose commits are
        scored now.
        """
        ending = self._ending(run_id)
        versions = self._received(run_id, repo, base_revision, work)
        self._keep_cut_work(run_id, name, repo, versions, cut=ending is None)
        end, error = handed_off(ending) if ending is not None else (RunEnd.DAEMON_STOPPED, None)
        self._close(span, run_id, end, error)
        last = self._unsubmitted(run_id, campaign_id, repo, versions) if ending is not None and versions else None
        self._settle(run_id, end, last)

    def _stopped(self, span: Span, run_id: RunId) -> None:
        """Trace the end that a daemon stop writes on the run (``Scheduler._fail_run``), which owns the row."""
        with self._db.session() as s:
            run = s.get_one(Run, run_id)
            ending = end_requested(s, run) if run.status is RunStatus.RUNNING else None
            end, error = handed_off(ending) if ending is not None else (RunEnd.DAEMON_STOPPED, None)
        span.set(status=end.status, end=end, error=error)

    def _ran(
        self,
        span: Span,
        run_id: RunId,
        campaign_id: CampaignId,
        agent_session_id: AgentSessionId,
        repo: Repository,
        base_revision: Revision,
        ended: Ran,
        *,
        stop_cut: bool = False,
    ) -> None:
        """Close the run of an agent that worked (``run_outcome``), score its versions and settle its idea.

        ``stop_cut``: the campaign's stop cut its session shorter than its role timeout.
        """
        outcome = ended.process
        versions = self._received(run_id, repo, base_revision, ended.work)
        ending = self._ending(run_id)
        with self._db.session() as s:
            stop_ended = s.scalar(
                select(Event.id).where(
                    Event.target == TargetRef(TargetKind.AGENT_SESSION, agent_session_id), has(AgentEnded)
                )
            )
            cancelled = run_cancel(s, run_id) is not None
            unsaved = latest(s, WorkUnsaved, Event.target == TargetRef(TargetKind.AGENT_SESSION, agent_session_id))
            sent = bool(submitted(s, run_id))
        facts = RunFacts(
            ending=ending,
            ended=stop_ended is not None,
            cancelled=cancelled,
            timed_out=outcome.status is ExitStatus.TIMED_OUT,
            stop_cut=stop_cut,
            error=outcome.error,
            unsaved=unsaved[1].error if unsaved is not None else None,
            submitted=sent,
        )
        end, error = run_outcome(facts)
        self._close(span, run_id, end, error)
        last = self._unsubmitted(run_id, campaign_id, repo, versions) if versions else None
        self._settle(run_id, end, last)

    def _relaunch(self, span: Span, run_id: RunId, agent_session_id: AgentSessionId, log_dir: Path) -> bool:
        """Queue the run again with a new implementer, up to its role's ``startup_relaunches``, after it failed to start.

        The run stays open, so it keeps its slot, attempt number and brief. The ``implementer#<id>`` in the brief becomes the
        new implementer's id. The new implementer signs its notes with its own id. The scheduler launches the new agent session row like any other. The old implementer's transcript is
        renamed so that the new one does not overwrite it. A request made while the daemon stops waits for the next
        daemon, as every request does. A cancelled run (``run_cancel``) is not queued again, and neither is a run that
        its campaign would not launch now (``unlaunchable``): it ends at once and frees its slot, rather than wait.
        """
        with self.launcher.lock:
            with self._db.session() as s:
                run = s.get_one(Run, run_id)
                role = run.campaign.config.roles.doing_runs().get(run.role)
                launches = s.scalar(select(func.count()).select_from(AgentSession).where(working_on(run_id))) or 0
                if role is None or launches > role.startup_relaunches or run_cancel(s, run_id) is not None:
                    return False
                if (why := unlaunchable(s, run.campaign, role.timeout, role.wrap_up_lead)) is not None:
                    span.set(relaunch=why)
                    return False
                relaunched = RunRelaunched(agent_session=agent_session_id, launches=launches)
                transition(s, RUN_STATES, run_id, RunStatus.QUEUED, relaunched, System.DAEMON)
                agent_session = AgentSession(
                    kind=run.role,
                    campaign_id=run.campaign_id,
                    subject=TargetRef(RUN, run_id),
                    harness=run.harness,
                    model=run.model,
                    reason=f'run#{run_id} relaunched: {run.role}#{agent_session_id} failed to start',
                )
                s.add(agent_session)
                s.flush()
                # Edit the brief, do not compose it again: a human may have changed it (``UPDATE runs SET brief_cid``).
                signed = rf'\b({re.escape(run.role)})#{agent_session_id}\b'
                assert run.brief_cid is not None  # the launch that failed wrote it
                resigned = re.sub(signed, rf'\g<1>#{agent_session.id}', read_brief(self.cas, run.brief_cid))
                run.brief_cid = self.cas.put(resigned.encode())
                s.commit()
            transcript = log_dir / TRANSCRIPT
            if transcript.exists():
                transcript.rename(log_dir / f'transcript-implementer{agent_session_id}.txt')
        span.set(status='relaunched')
        return True

    def _keep_cut_work(self, run_id: RunId, name: str, repo: Repository, versions: list[Version], *, cut: bool) -> None:
        """Keep the work of a run that a daemon stop interrupted, unsaved work included.

        What the agent left unsaved was saved on its own (``AgentJob.capture``).

        The stop owns the run's row. A run that the stop ``cut`` is not an attempt (``ATTEMPTS``), so its
        commits are not measured: each new one is ``recovered``. A run whose implementer had left its
        worker had ended it was handed off: the stop closed it as its end says, and the next daemon scores the pinned
        commits (``resume``).
        """
        if not versions:
            return
        for i, version in enumerate(versions):
            repo.keep(version.revision, run_pin(name, run_id, i))
        if not cut:
            return
        with self._db.session() as s:
            run = s.get_one(Run, run_id)
            candidate = run.base
            for version in versions:
                candidate, _ = resolve(
                    s, run.campaign_id, version, parent=candidate, run_id=run_id, origin=Origin.RECOVERED
                )
            s.commit()

    def _received(
        self, run_id: RunId, repo: Repository, base_revision: Revision, work: Transfer | None
    ) -> list[Version]:
        """Take the run's versions into the repo of record (``Repository.receive``), and return them, oldest first.

        They are recorded (``RunReceived``), for ``resume``, and the transfer with them (``RepoTransfer``), for a home
        made again on another host. The worker saved the unsaved work first, as a version of
        its own: a run that ends keeps it ``skipped`` (``_unsubmitted``), and one that a stop cut keeps it ``recovered``
        (``_keep_cut_work``).
        """
        versions = repo.receive(self.cas, work, base_revision) if work is not None else []
        with self._db.session() as s:
            received = RunReceived(revisions=[v.revision for v in versions])
            emit(s, received, TargetRef(RUN, run_id), System.DAEMON)
            if work is not None:
                transfer = RepoTransfer(
                    campaign_id=s.get_one(Run, run_id).campaign_id, base=base_revision, head=work.head, blob=work.blob
                )
                s.add(transfer)
            s.commit()
        return versions

    def _campaign(self, campaign_id: CampaignId) -> Campaign:
        """The campaign's row, for ``Sources``, which runs outside every transaction: minutes the first time."""
        with self._db.session() as s:
            return s.get_one(Campaign, campaign_id, options=[undefer(Campaign.config)])

    def _ending(self, run_id: RunId) -> RunEndRequested | None:
        """How the run's worker ended the run (``end_requested``), or None."""
        with self._db.session() as s:
            return end_requested(s, s.get_one(Run, run_id))

    def resume(self, run_id: RunId) -> None:
        """Score the commits of a closed run and settle its idea, after a daemon stopped while it scored them.

        The versions are the ones that the run received (``RunReceived``). Never raises. If scoring cannot resume, for example because
        the repo is gone, the run settles as failed: its idea is open again.
        """
        with self._tracer.span('piloop.resume', run=run_id) as span:
            with self._db.session() as s:
                run = s.get_one(Run, run_id)
                campaign = run.campaign
                end, campaign_id = run.end, campaign.id
                vcs, campaign_path = vcs_of(campaign), Path(campaign.path)
                received = latest(s, RunReceived, Event.target == TargetRef(RUN, run_id))
            last = None
            try:
                with closing(vcs.open(campaign_path)) as repo:
                    revisions = received[1].revisions if received is not None else []
                    versions = [repo.version(revision) for revision in revisions]
                    span.set(versions=len(versions))
                    last = self._unsubmitted(run_id, campaign_id, repo, versions)
            except Exception as e:
                span.error(e)
                end = RunEnd.FAILED
            assert end is not None  # a run whose commits are scored is closed
            self._settle(run_id, end, last)

    def remeasure(self, candidate_id: CandidateId) -> None:
        """Evaluate a candidate with the campaign's current evaluator, as ``pool.due`` asks, then confirm it if due.

        Never raises. If its campaign's repo is gone, a crashed measurement is recorded. It is a result like any other,
        so the candidate is not tried again with this evaluator unless someone asks (``request_remeasure``). Any other
        failure is not the candidate's: it is an infra error (``_measure``), or the span records it and the next pass
        asks again. What someone asked for gets an evaluation of its own with the evaluations that they asked for
        (``Evaluation.ask``), and the baseline and the carried best the ones that confirm them (``Status.needs``);
        anything else gets one. A candidate whose mean now beats the best's, such as a new reference, is confirmed
        (``_confirm``), and a run's worker hears where its candidate stands (``CandidateScored``). A confirmed candidate
        that the sealed split is due to score (``Pool.seal_due``) is only scored there. A baseline that fails and never
        measured pauses its campaign (``hold_on_failed_baseline``).
        """
        with self._db.session() as s:
            candidate = s.get_one(Candidate, candidate_id)
            campaign = s.get_one(Campaign, candidate.campaign_id)
            cfg = campaign.config
            evaluator_id = current_evaluator(s, campaign, cfg).id
            s.commit()
            asked, now = remeasure_asked(s, candidate), status(candidate, evaluator_id)
            confirming = candidate.id in {c.id for c in firsts(s, campaign)}
            need = now.needs(confirm_runs(s, campaign, evaluator_id))
            repeats = asked[1].repeats if asked is not None else max(need, 1) if confirming else 1
            ask = asked[0].id if asked is not None else None
            visible = asked is not None or confirming or not now.measured
            campaign_id, revision, run_id = campaign.id, candidate.revision, candidate.run_id
            vcs, campaign_path = vcs_of(campaign), Path(campaign.path)
        eval_dir = campaign_dir(self._home, campaign_id) / 'measure' / f'{candidate_id}-e{evaluator_id}'
        try:
            with closing(vcs.open(campaign_path)):
                pass
        except Exception as e:  # a repo that is gone is the candidate's crash, not a job's
            with self._db.session() as s:
                error = Sample(metrics={}, seconds=0.0, error=f'{type(e).__name__}: {e}')
                record(s, self.cas, None, candidate_id, evaluator_id, error)
                hold_on_failed_baseline(s, s.get_one(Candidate, candidate_id), None)
                s.commit()
            return
        with self._tracer.span('piloop.remeasure', candidate=candidate_id) as span:
            try:
                ended = (
                    self._measure(
                        None, campaign_id, cfg, revision, candidate_id, evaluator_id, eval_dir, repeats=repeats, ask=ask
                    )
                    if visible
                    else Measuring.MEASURED
                )
            except Exception as e:  # the thread's last frame: what failed goes into the span, never lost
                span.error(e)
                return
            span.set(status=ended)
        if ended is not Measuring.MEASURED:
            return  # a stop, a cancel, a new evaluator or the last infra error ended it: there is nothing new
        with self._db.session() as s:
            measured_now = s.get_one(Candidate, candidate_id)
            hold_on_failed_baseline(s, measured_now, eval_dir)
            if measured_now.origin in UNSCORED:  # a commit that no run measured enters the pool with its measurement
                measured_now.origin = Origin.RUN
            first = measured_now.origin is Origin.BASELINE
            s.commit()
        if first or self._confirm(run_id, candidate_id, eval_dir) is not Measuring.MEASURED:
            return
        if run_id is not None:
            self._scored(candidate_id)

    def measure_once(self, candidate_id: CandidateId) -> Measuring:
        """Evaluate a candidate once with its campaign's current evaluator, up every rung, and confirm nothing.

        For a campaign type that scores a version on one evaluation, such as the merges of an ``accumulating`` campaign.
        A candidate that the current evaluator answered already (``Status.measured``) is not evaluated again. Return how
        it ended, as ``_measure`` does.
        """
        with self._db.session() as s:
            candidate = s.get_one(Candidate, candidate_id)
            campaign = s.get_one(Campaign, candidate.campaign_id)
            cfg = campaign.config
            evaluator_id = current_evaluator(s, campaign, cfg).id
            s.commit()
            if status(candidate, evaluator_id).measured:
                return Measuring.MEASURED
            campaign_id, revision = campaign.id, candidate.revision
        eval_dir = campaign_dir(self._home, campaign_id) / 'measure' / f'{candidate_id}-e{evaluator_id}'
        return self._measure(None, campaign_id, cfg, revision, candidate_id, evaluator_id, eval_dir)

    def _unsubmitted(
        self, run_id: RunId, campaign_id: CampaignId, repo: Repository, versions: list[Version]
    ) -> CandidateId | None:
        """Keep each new version of the run as ``skipped``, and return its newest submission, or None.

        A version that it submitted is its submission's candidate. A tree that is a candidate already stays as it is.
        The submissions settle the run's idea, not a skipped version, which never enters the pool. ``_settle`` waits
        for their scores.
        """
        with self._db.session() as s:
            run = s.get_one(Run, run_id)
            name, candidate = run.campaign.name, run.base
            for version in versions:
                candidate, _ = resolve(s, campaign_id, version, parent=candidate, run_id=run_id, origin=Origin.SKIPPED)
            s.commit()
            mine = submitted(s, run_id)
        for i, version in enumerate(versions):
            repo.keep(version.revision, run_pin(name, run_id, i))
        return mine[-1] if mine else None

    def score_submission(self, candidate_id: CandidateId) -> None:
        """Score a submitted candidate into the pool (``_score_one``), and tell where it stands. Never raises.

        A failure is no score: the span records it, the submission waits, and the next pass of the daemon scores it
        again.
        """
        with self._tracer.span('piloop.submission', candidate=candidate_id) as span:
            try:
                with self._db.session() as s:
                    run_id = s.get_one(Candidate, candidate_id).run_id
                assert run_id is not None  # a submission is a run's candidate
                eval_dir = self._runs_dir / str(run_id) / f'submission-{candidate_id}'
                span.set(status=self._score_one(run_id, candidate_id, eval_dir))
            except Exception as e:  # the thread's last frame: what failed goes into the span, never lost
                span.error(e)

    def _score_one(
        self, run_id: RunId, candidate_id: CandidateId, eval_dir: Path
    ) -> Literal[Measuring.MEASURED] | Halted:
        """Evaluate a run's candidate once, confirm it if its mean beats the best's, and record where it stands.

        The first evaluation may stop at a rung of the ladder, against the best's values there (``EvaluationJob.gate``).
        A candidate that the current evaluator measured already is not evaluated again: the same tree again says
        nothing new about it. An evaluator that ``piloop init`` makes meanwhile measures it again, and so does one that
        replaces the evaluator before its evaluation runs (``Measuring.REPLACED``). Return how it ended: a stop of the
        daemon or a cancel records no score, and a cancel stands until someone asks again (``Status.cancelled``).
        """
        tried: set[EvaluatorId] = set()  # a climb that stopped at a rung is a measurement too: one per evaluator
        while True:  # the evaluator now: one that ``piloop init`` made while it was measured measures it again
            with self._db.session() as s:
                candidate = s.get_one(Candidate, candidate_id)
                campaign = s.get_one(Campaign, candidate.campaign_id)
                cfg = campaign.config
                evaluator_id = current_evaluator(s, campaign, cfg).id
                s.commit()
                best = Pool.of(s, campaign, track_of(cfg, candidate)).best()
                now = status(candidate, evaluator_id)
                campaign_id, revision = campaign.id, candidate.revision
            if now.cancelled:  # a cancel stands until someone asks again
                return Measuring.CANCELLED
            if now.latest is None and evaluator_id not in tried:
                match ended := self._measure(
                    run_id, campaign_id, cfg, revision, candidate_id, evaluator_id, eval_dir / 'eval', gate=best
                ):
                    case Measuring.MEASURED:
                        tried.add(evaluator_id)
                    case Measuring.REPLACED:
                        pass
                    case Measuring.CANCELLED | Measuring.STOPPED | Measuring.ABANDONED:
                        return ended
                    case never:
                        assert_never(never)
            elif (ended := self._confirm(run_id, candidate_id, eval_dir)) is not Measuring.REPLACED:
                break
        if ended is not Measuring.MEASURED:
            return ended
        self._scored(candidate_id)
        return Measuring.MEASURED

    def _confirm(self, run_id: RunId | None, candidate_id: CandidateId, eval_dir: Path) -> Measuring:
        """Evaluate a candidate until it is confirmed, when its mean beats the best's (``Pool.confirm_due``).

        The sealed split scores it meanwhile when it has not (``Pool.seal_due``), and the baseline too when the split has
        not scored it: the best rule reads both. Return how it ended: ``MEASURED`` also when nothing was due.
        """
        with self._db.session() as s:
            candidate = s.get_one(Candidate, candidate_id)
            campaign = s.get_one(Campaign, candidate.campaign_id)
            cfg = campaign.config
            pool = Pool.of(s, campaign, track_of(cfg, candidate))
            confirming, sealing_due = pool.confirm_due(candidate_id), pool.seal_due(candidate_id)
            if not confirming and not sealing_due:
                return Measuring.MEASURED
            need = pool.entries[candidate_id].status.needs(pool.confirm) if confirming else 0
            hidden = sealed_evaluator(s, campaign, cfg) if sealing_due else None
            s.commit()
            first = baseline(s, campaign)
            sides = [
                c
                for c in (candidate, first)
                if c is not None and hidden is not None and not status(c, hidden.id).measured
            ]
            seal = [(c.id, c.revision) for c in dict.fromkeys(sides)]
            campaign_id, revision, evaluator_id = campaign.id, candidate.revision, campaign.evaluator_id
            hidden_id = hidden.id if hidden is not None else None
        assert evaluator_id is not None  # a campaign that measured a candidate has an evaluator
        with ThreadPoolExecutor(1, thread_name_prefix=f'sealed-{candidate_id}') as threads:
            sealing = [
                threads.submit(
                    self._measure,
                    run_id,
                    campaign_id,
                    cfg,
                    commit,
                    c,
                    hidden_id,
                    eval_dir / f'sealed-{c}',
                    split=Split.SEALED,
                )
                for c, commit in seal
                if hidden_id is not None
            ]
            ended = (
                self._measure(
                    run_id, campaign_id, cfg, revision, candidate_id, evaluator_id, eval_dir / 'confirm', repeats=need
                )
                if need > 0
                else Measuring.MEASURED
            )
            halted = [h for f in sealing if (h := f.result()) is Measuring.STOPPED]
        return Measuring.STOPPED if halted else ended

    def _scored(self, candidate_id: CandidateId) -> None:
        """Record where a candidate stands now (``CandidateScored``): its run's worker hears it."""
        with self._db.session() as s:
            candidate = s.get_one(Candidate, candidate_id)
            campaign = s.get_one(Campaign, candidate.campaign_id)
            record_scored(s, Pool.of(s, campaign, track_of(campaign.config, candidate)), candidate_id)
            s.commit()

    def _measure(
        self,
        run_id: RunId | None,
        campaign_id: CampaignId,
        cfg: CampaignConfig,
        revision: Revision,
        candidate_id: CandidateId,
        evaluator_id: EvaluatorId,
        eval_dir: Path,
        *,
        repeats: int = 1,
        gate: CandidateId | None = None,
        split: Split = Split.VISIBLE,
        ask: int | None = None,
    ) -> Measuring:
        """Measure ``revision`` on the executor, and wait until its measurement is recorded. Return how it ended.

        ``repeats`` samples of ``revision``. With ``gate``, the candidate whose rungs the ladder's stops compare with
        (the best), the first sample may stop at a rung (``EvaluationJob.gate``): only a run's own version's first
        evaluation. Without, every sample climbs every rung. ``ask`` is the remeasure that it is for: only an
        evaluation that this request makes answers it (``Evaluation.ask``).

        An evaluation that failed for a cause that is not its candidate's records nothing (``InfraFailed``), whichever
        request ran it (``left``): it waits in the queue, where ``piloop eval queue`` lists it and ``piloop eval
        cancel`` cancels it, and runs again after ``[evaluation] infra_retry``, twice as long after each failure up to
        ``infra_retry_max``. Each failure is an event. It ends once it measures, someone cancels it, the daemon stops, a new evaluator of its split replaces
        ``evaluator_id`` (``queue.withdraw``): the caller then asks the current one (``Measuring.REPLACED``), or
        its task or campaign reached ``stop_after_tries`` consecutive infrastructure failures.
        """
        failures, retry = 0, min(self._evaluation.infra_retry, self._evaluation.infra_retry_max)
        request = partial(
            self._request,
            run_id,
            campaign_id,
            cfg,
            revision,
            candidate_id,
            evaluator_id,
            eval_dir,
            repeats,
            gate,
            split,
            ask,
        )
        ended = request()
        while True:
            match ended:
                case InfraFailed(error=error):
                    failures += 1
                    self._tracer.event(
                        ended.kind, candidate=candidate_id, error=error, failures=failures, retry=retry.total_seconds()
                    )
                    # waits in the queue meanwhile, so a cancel finds it; the lock as in ``_request``: the runs that
                    # shared the evaluation all put it back
                    with self.launcher.lock, self._db.session() as s:
                        write_transaction(s)
                        if task_holds.held(s, s.get_one(Candidate, candidate_id)):
                            return Measuring.ABANDONED
                        waiting, _ = queue.request(s, candidate_id, evaluator_id, ask)
                        s.commit()
                        row, before = waiting.id, _last_measurement(s, candidate_id, evaluator_id)
                    self.dispatch()  # as after each request: a new evaluator takes it from the queue
                    gone = self._db.wait_until(
                        lambda s, row=row: s.get(Evaluation, row) is None,
                        on=[Topic(EvaluationDeleted, target=TargetRef(CANDIDATE, candidate_id))],
                        give_up=self.launcher.cancelled.is_set,
                        timeout=retry.total_seconds(),
                    )
                    # double the last wait: ``infra_retry * 2**failures`` overflows ``timedelta`` at the 42nd failure
                    retry = min(retry * 2, self._evaluation.infra_retry_max)
                    if self.launcher.cancelled.is_set():
                        return Measuring.STOPPED
                    if not gone:
                        ended = request()
                        continue
                    # another request ran it meanwhile, someone cancelled it, or a new evaluator took it
                    with self._db.session() as s:
                        ended = left(s, row, candidate_id, evaluator_id, before)
                case Measuring():
                    return ended
                case never:
                    assert_never(never)

    def _request(
        self,
        run_id: RunId | None,
        campaign_id: CampaignId,
        cfg: CampaignConfig,
        revision: Revision,
        candidate_id: CandidateId,
        evaluator_id: EvaluatorId,
        eval_dir: Path,
        repeats: int,
        gate: CandidateId | None,
        split: Split,
        ask: int | None,
    ) -> Measuring | InfraFailed:
        """Ask for one evaluation (``_measure``), and wait until it leaves the queue. Return how it ended (``left``).

        A request for a candidate that already waits or runs under the evaluator waits for that evaluation and submits
        nothing of its own (``queue.request``). It is submitted when the queue gives it a slot (``dispatch``), and its
        result records the measurement (``CampaignResults``). An evaluation that fails is a crash, a measurement too.
        A cancelled one records nothing (``queue.cancel``), nor does an infra error, which says so (``InfraFailed``).
        Nor does a job whose campaign's inputs cannot be stored (a full disk, say): never its candidate's fault, it is
        an infra error too (``unmade``).
        """
        try:
            artifact: Source | str = self.sources.source(self._campaign(campaign_id), revision)
        except Exception as e:  # only a request that makes the job needs the inputs: a joined one waits all the same
            artifact = f'cannot store its inputs: {type(e).__name__}: {e}'
        with self.launcher.lock:
            if self.launcher.cancelled.is_set():
                return Measuring.STOPPED
            with self._db.session() as s:  # two requests for one tree must not both insert it
                write_transaction(s)
                candidate = s.get_one(Candidate, candidate_id)
                task_holds.identify(s, candidate)
                if task_holds.held(s, candidate):
                    s.commit()  # keep the task identity learned above for later scheduling passes
                    return Measuring.ABANDONED
                before = _last_measurement(s, candidate_id, evaluator_id)
                evaluation, created = queue.request(s, candidate_id, evaluator_id, ask)
                # a row that waits with no job of this daemon is one that an infra error put back (``_measure``): a
                # request for the same ask makes its job again
                mine = created or (
                    evaluation.started_at is None and evaluation.id not in self._jobs and evaluation.ask == ask
                )
                job = None
                if mine:
                    if isinstance(artifact, str):
                        failed = unmade(s, evaluation, artifact, self._evaluation.stop_after_tries)
                        s.commit()
                        return failed
                    score = Score.of(cfg.all_tracks[0].primary)
                    job = EvaluationJob(
                        stop_after_tries=self._evaluation.stop_after_tries,
                        evaluation=evaluation.id,
                        candidate=candidate_id,
                        run=run_id,
                        runner=s.get_one(Evaluator, evaluator_id).runner,
                        artifact=artifact,
                        place=eval_dir.relative_to(self._home).as_posix(),
                        command=cfg.evaluate,
                        timeout=cfg.evaluate_timeout,
                        score=score,
                        required=cfg.required,
                        known=cfg.reported,
                        max_evidence_bytes=self._evaluation.max_evidence_bytes,
                        ladder=cfg.ladder,
                        gate=gate_of(s.get_one(Candidate, gate), evaluator_id, cfg)
                        if gate is not None and cfg.ladder
                        else None,
                        stop_grace=self._evaluation.stop_grace_seconds,
                        repeats=repeats,
                    )
                    match split, cfg.evaluator.sealed:
                        case Split.SEALED, [*command]:  # the score alone, with no ladder
                            job = job.model_copy(
                                update={
                                    'command': command,
                                    'required': [score.name],
                                    'ladder': [],
                                    'gate': None,
                                    'sealed': True,
                                }
                            )
                        case _:
                            pass
                s.commit()
                evaluation_id = evaluation.id
            if job is not None:
                self._jobs[evaluation_id] = job
        self.dispatch()
        with self._tracer.span('piloop.measure', run=run_id, revision=revision, candidate=candidate_id) as span:
            span.set(evaluation=evaluation_id, joined=not mine)
            measured = self._wait(evaluation_id, candidate_id)
            self._jobs.pop(evaluation_id, None)
            self.dispatch()  # its slot is free
            if not measured or self.launcher.cancelled.is_set():  # a stop's evaluation records nothing
                span.set(status='cancelled')
                return Measuring.STOPPED
            with self._db.session() as s:
                match ended := left(s, evaluation_id, candidate_id, evaluator_id, before):
                    case InfraFailed():
                        span.set(status='infra_error')
                    case Measuring.MEASURED:
                        measurement = s.get_one(Measurement, _last_measurement(s, candidate_id, evaluator_id))
                        span.set(
                            measurement=measurement.id,
                            error=measurement.error,
                            **{f'metric.{k}': v for k, v in measurement.metrics.items()},
                        )
                    case _:
                        span.set(status=ended)
        return ended

    def _close(self, span: Span, run_id: RunId, end: RunEnd, error: str | None) -> None:
        """Record the run's end when its implementer ends, so the slot is free while the commits are scored (``scoring``)."""
        with self.launcher.lock:
            if self.launcher.cancelled.is_set():  # stop() fails the row. Do not open it again.
                self._stopped(span, run_id)
                return
            with self._db.session() as s:
                try:
                    close_run(s, s.get_one(Run, run_id), end, error).scoring = True
                except Refused as e:  # another writer closed it first, a stop's drain say: its end stands
                    span.set(status='already_closed', found=e.found, end=end)
                    return
                s.commit()
        span.set(status=end.status, end=end, error=error)

    def _settle(self, run_id: RunId, end: RunEnd, last: CandidateId | None) -> None:
        """After the run's commits are scored, end its ``scoring``, and let its campaign's type settle its idea.

        ``last`` is the newest candidate of the run's versions that has a score: it decides whether the idea is done.
        It first waits until each submission of the run is scored (``score_submission``), however the run ended: its
        idea is settled on them. A daemon that stops meanwhile leaves the run ``scoring``, for the next one.
        """

        def scored(s: Session) -> bool:
            waiting = {c.id for c in submissions.waiting(s, s.get_one(Run, run_id).campaign)}
            return waiting.isdisjoint(submitted(s, run_id))

        if not self._db.wait_until(scored, on=SETTLES, give_up=self.launcher.cancelled.is_set):
            return
        with self.launcher.lock:
            if self.launcher.cancelled.is_set():  # stop() ends the claim; the run's end stands
                return
            with self._db.session() as s:
                run = s.get_one(Run, run_id)
                run.scoring = False
                kind = CAMPAIGN_TYPES.model_of(run.campaign.type)()
                kind.settle(s, run, end, s.get(Candidate, last) if last is not None else None)
                s.commit()

    def _context(
        self, s: Session, cfg: CampaignConfig, run: Run, base: Candidate, reader: Agent, timeout: float
    ) -> list[str]:
        """Return the worker's lines of context: its time, its base, its idea's directory, its note and its escalation.

        The board is a section of its own (``queue``).
        """
        author = reader.signature
        lead = wrap_up_lead(timeout, cfg.roles.doing_runs()[run.role].wrap_up_lead)
        campaign = run.campaign
        evaluator_id = campaign.evaluator_id
        at_base = ', '.join(f'{t.primary.name}={score(base, evaluator_id, t.primary)}' for t in cfg.all_tracks)
        lines = [
            f'- Your session ends at `${DEADLINE_ENV}` (Unix time), {round(timeout / 60, 1):g} minutes after it starts.'
            f' You are told to wrap up {round(lead / 60, 1):g} minutes before: start no command that can run past it.',
            f'- Starting from candidate#{base.id}, the best: {at_base} — {base.summary}',
            *branching(s, campaign),
        ]
        lines += [
            f"- Your idea's directory in the shared workspace: `./{WORKSPACE}/idea<your idea>/`, in no version.",
            "- A note on your idea: INSERT INTO comments (target_kind, target_id, author, kind, body) VALUES ('idea',"
            f" <your idea>, '{author}', 'note', '...').",
            '- What you cannot do or decide yourself: `piloop escalate idea#<your idea> --priority 1`, its form as JSON'
            ' on stdin (`--guide` shows it).',
        ]
        return lines


def track_brief(idea: Idea) -> str | None:
    """What the track of a claimed ``idea`` asks of its run, for its worker: what the best needs, then the addendum.

    The brief is written before the claim, so the claim answers this. None with one track: the brief says it all.
    """
    cfg = idea.campaign.config
    if not cfg.multitrack:
        return None
    track = cfg.track(idea.track)
    guards = ', '.join(
        f'{m.name} ({criterion(m).limit()}' + (f', floor {m.floor:g})' if m.floor is not None else ')')
        for m in track.guardrails
    )
    rule = (
        f'Your run is on track {track.name}: its best has the best {track.primary.name} ({track.primary.direction} is'
        f' better), and holds {guards} against the baseline.'
    )
    addendum = (Path(idea.campaign.path) / track.addendum).read_text().rstrip() if track.addendum else ''
    return '\n\n'.join(p for p in (rule, addendum) if p)


def left(
    s: Session, evaluation_id: int, candidate_id: CandidateId, evaluator_id: EvaluatorId, before: MeasurementId | None
) -> Literal[Measuring.MEASURED, Measuring.CANCELLED, Measuring.REPLACED, Measuring.ABANDONED] | InfraFailed:
    """How the evaluation ``evaluation_id`` ended, once it left the queue, for a request that waited for it.

    ``before`` is the candidate's last measurement under ``evaluator_id`` when the request found the evaluation. The one
    place that says it, whichever request ran the evaluation: it failed for a cause that is not its candidate's
    (``InfraFailed``), so the request asks again; it measured the candidate (``MEASURED``); else a new evaluator took it
    from the queue (``REPLACED``), its task was held (``ABANDONED``), or someone cancelled it (``CANCELLED``).
    """
    if (infra := latest(s, InfraFailed, has(InfraFailed, evaluation=evaluation_id))) is not None:
        return infra[1]
    if _last_measurement(s, candidate_id, evaluator_id) != before:
        return Measuring.MEASURED
    if retired(s, evaluator_id):
        return Measuring.REPLACED
    return Measuring.ABANDONED if task_holds.held(s, s.get_one(Candidate, candidate_id)) else Measuring.CANCELLED


def _last_measurement(s: Session, candidate_id: CandidateId, evaluator_id: EvaluatorId) -> MeasurementId | None:
    """The newest measurement of the candidate under the evaluator, by id, or None."""
    found = select(func.max(Measurement.id)).where(
        Measurement.candidate_id == candidate_id, Measurement.evaluator_id == evaluator_id
    )
    return s.scalar(found)


def wrap_up_lead(timeout: float, most: timedelta) -> float:
    """How many seconds before its role ``timeout`` an implementer is told to wrap up: a quarter, at most ``most``."""
    return min(most.total_seconds(), timeout / 4)


def unlaunchable(s: Session, campaign: Campaign, timeout: float, most: timedelta) -> str | None:
    """Why ``campaign`` would not launch an implementer of role ``timeout`` and lead ``most`` now, or None.

    It is not active, it is paused, or its stop rule ends the session within its wrap-up lead (``wrap_up_lead``): the
    implementer would be told to wrap up as it starts.
    """
    if campaign.status is not CampaignStatus.ACTIVE:
        return f'the campaign is {campaign.status}'
    if campaign.paused is not None:
        return f'the campaign is paused: {campaign.paused_reason}'
    if seconds(agent_timeout(s, campaign, campaign.config, timeout, utcnow())) <= wrap_up_lead(timeout, most):
        return 'its session would end within its wrap-up lead'
    return None


@dataclass(frozen=True)
class Start:
    """What a new implementer starts with: its ``brief``, the seconds that it may run, and a resumed session's brief."""

    brief: Cid
    timeout: float
    resumed: Cid | None


@dataclass(frozen=True)
class RunFacts:
    """The facts that decide how a run ended (``run_outcome``), gathered when its implementer ends."""

    ending: RunEndRequested | None = None
    """How its worker ended it (``piloop run end``), if it did."""
    ended: bool = False
    """The daemon ended the implementer (``AgentEnded``)."""
    cancelled: bool = False
    """Someone cancelled the run (``run_cancel``)."""
    timed_out: bool = False
    """The implementer ran into its timeout."""
    stop_cut: bool = False
    """The campaign's stop cut its session shorter than its role timeout: a timeout then is the stop, not the run's."""
    error: str | None = None
    """How the implementer's process failed, a timeout included."""
    unsaved: str | None = None
    """Why the save of what the implementer left unsaved failed (``WorkUnsaved``)."""
    submitted: bool = False
    """It submitted a version (``piloop submit``)."""


def run_outcome(facts: RunFacts) -> tuple[RunEnd, str | None]:
    """Return how a run that ``facts`` describe ended, and the error that details it: the first case that matches."""
    match facts:
        # The worker ended its run: nothing that ends its process after it undoes that.
        case RunFacts(ending=RunEndRequested(how=RunEnding.SUBMITTED)):
            return RunEnd.FINISHED, None
        case RunFacts(ending=RunEndRequested(how=RunEnding.GAVE_UP, reason=reason)):
            return RunEnd.GAVE_UP, reason
        # A cancel, whether the daemon ended the implementer or it ended by itself first.
        case RunFacts(cancelled=True):
            return RunEnd.CANCELLED, None
        # A stop ended it, whatever the kill left in the outcome.
        case RunFacts(ended=True):
            return RunEnd.CAMPAIGN_STOPPED, None
        # It ran into the campaign's stop, which cut its session short: no attempt at its idea.
        case RunFacts(timed_out=True, stop_cut=True):
            return RunEnd.CAMPAIGN_STOPPED, None
        # It ran into its role's timeout.
        case RunFacts(timed_out=True, error=error):
            return RunEnd.TIMED_OUT, error
        # Its process failed.
        case RunFacts(error=str() as error):
            return RunEnd.FAILED, error
        # What it left unsaved is only in the CAS.
        case RunFacts(unsaved=str() as error):
            return RunEnd.FAILED, f'{UNSAVED}: {error}'
        # A clean exit ends the run: with what it submitted, or with nothing.
        case RunFacts(submitted=True):
            return RunEnd.FINISHED, None
        case _:
            return RunEnd.GAVE_UP, None


def close_run(s: Session, run: Run, end: RunEnd, error: str | None) -> Run:
    """Close ``run`` as ``end``: its status, end, error and finish time, and ``RunFinished``. The caller commits.

    Every writer of a run's end goes through here.

    Raises:
        Refused: the run is closed already. Its first end stands.
    """
    finished = RunFinished(end=end, error=error)
    transition(s, RUN_STATES, run.id, end.status, finished, System.DAEMON, end=end, error=error, finished_at=utcnow())
    return run


def _close_unstarted(s: Session, agent_session_id: AgentSessionId, error: str) -> bool:
    """Close the implementer if the launcher submitted no job for it. Return whether it closed. The caller commits.

    The launcher gives the row its runtime before it submits its job (``Launcher._prepare``): a row without one was not
    launched, and no report will close it. One with it may run already: its executor has it. A row that a stop or a
    cancel ended meanwhile (``ending``) keeps why it ended it.
    """
    agent = s.get_one(AgentSession, agent_session_id)
    if agent.runtime is not None or agent.state not in (AgentSessionState.STARTING, AgentSessionState.ENDING):
        return False
    finish(s, agent, AgentSessionState.KILLED, agent.error if agent.state is AgentSessionState.ENDING else error)
    return True


def handed_off(ending: RunEndRequested) -> tuple[RunEnd, str | None]:
    """The end and error of a run that a stop hands off because its worker ended it (``run_outcome``)."""
    return run_outcome(RunFacts(ending=ending))


def run_place(run_id: int) -> str:
    """The place of a run's implementer under the daemon's home: its log directory, ``runs/<id>``."""
    return f'{RUNS}/{run_id}'


def run_pin(campaign: str, run: RunId, index: int) -> Pin:
    """The pin of a run's ``index``-th version, oldest first: ``r<run>/<index>``."""
    return Pin(campaign, f'r{run}/{index}')
