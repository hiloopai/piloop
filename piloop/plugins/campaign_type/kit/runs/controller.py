"""Execute one run: make a worktree, run the worker, close the run, score its commits and end its claim.

The run closes when its implementer ends, so the implementer slot is free while the daemon measures the commits. The idea stays
``claimed`` until its candidates are scored.
"""

import json
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from functools import partial
from pathlib import Path
from typing import ClassVar, Literal, assert_never

from covalence import Cid
from pydantic import ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, undefer

from piloop import deprecated
from piloop.actor import Actor, Agent, System
from piloop.agents.launcher import Launcher
from piloop.agents.results import (
    AgentEnded,
    CampaignResults,
    DaemonStopped,
    HandedOver,
    Lost,
    NotStarted,
    Ran,
    WorkUnsaved,
)
from piloop.agents.roles import briefing, compose_brief, compose_resumed, read_brief, render_sections, role
from piloop.campaign.campaigns import (
    WORKSPACE,
    Sources,
    campaign_dir,
    checkout_of,
    record_of,
    vcs_of,
    workspace_dir,
)
from piloop.campaign.stop import run_cancel, seconds, timeout as agent_timeout
from piloop.config import TRANSCRIPT, CampaignConfig
from piloop.db.events import emit, has, latest
from piloop.db.models import (
    AgentSession,
    AgentSessionState,
    Campaign,
    Comment,
    Event,
    EventModel,
    Limit,
    ReadBy,
    utcnow,
)
from piloop.db.session import Database, write_transaction
from piloop.ids import AgentSessionId, CampaignId, CommentKind, Revision, RoleName, TargetKind, TargetRef
from piloop.net.tokens import Tokens
from piloop.otel import Span, Tracer
from piloop.plugins import limits
from piloop.plugins.campaign_type.kit.backlog.tables import Idea
from piloop.plugins.campaign_type.kit.candidates.ids import CANDIDATE, CandidateId, Origin
from piloop.plugins.campaign_type.kit.candidates.references import resolve
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.evaluation import queue, submissions
from piloop.plugins.campaign_type.kit.evaluation.config import Scoring
from piloop.plugins.campaign_type.kit.evaluation.evaluate import Score
from piloop.plugins.campaign_type.kit.evaluation.ids import EvaluatorId, MeasurementId
from piloop.plugins.campaign_type.kit.evaluation.jobs import (
    EVALUATION,
    EvaluationJob,
    InfraFailed,
    InfraRetrying,
    Sample,
    record,
)
from piloop.plugins.campaign_type.kit.evaluation.measure import (
    Split,
    current_evaluator,
    gate_of,
    measured,
    remeasure_asked,
    retired,
    score,
    sealed_evaluator,
)
from piloop.plugins.campaign_type.kit.evaluation.ops import hold_on_failed_baseline, hold_on_infra_errors
from piloop.plugins.campaign_type.kit.evaluation.submissions import submitted
from piloop.plugins.campaign_type.kit.evaluation.tables import Evaluation, Evaluator, Measurement
from piloop.plugins.campaign_type.kit.pool.due import needs
from piloop.plugins.campaign_type.kit.pool.pool import UNSCORED, Pool, baseline, branching, carried, criterion, track_of
from piloop.plugins.campaign_type.kit.pool.scored import record_scored
from piloop.plugins.campaign_type.kit.runs.ids import RUN, RunEnd, RunEnding, RunId, RunStatus
from piloop.plugins.campaign_type.kit.runs.ops import RunEndRequested, end_requested
from piloop.plugins.campaign_type.kit.runs.stop import release
from piloop.plugins.campaign_type.kit.runs.tables import Run, working_on
from piloop.plugins.cas.contract import CAS, Cas
from piloop.plugins.executor.contract import (
    DEADLINE_ENV,
    EXECUTORS,
    Executor,
    ExecutorConfig,
    Host,
    JobKey,
)
from piloop.plugins.network.contract import NETWORKS, Network, NetworkHost
from piloop.plugins.role.contract import Continuity
from piloop.plugins.runtime.contract import ExitStatus, RuntimeFactory, make_runtime
from piloop.plugins.secrets.contract import SECRETS, HomeSecrets, SecretsProvider
from piloop.plugins.section.contract import Rendering
from piloop.plugins.stop_rule.contract import Stop
from piloop.plugins.vcs.contract import Pin, Repository, Transfer, Version
from piloop.settings import AgentSettings, EvaluationSettings, StorageSettings

type Unmeasured = Literal[Origin.RECOVERED, Origin.SKIPPED]
"""A version that the daemon does not measure: its recovered work, or one that asked to be skipped."""
UNSAVED = 'what the implementer left unsaved could not be saved, agent_session.work_unsaved has it in the CAS'
RUNS = 'runs'
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
    and its campaign's measurements wait for a human (``hold_on_infra_errors``)."""


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
    ) -> None:
        self._db = db
        self._tracer = tracer
        self._runs_dir = home / RUNS
        self._home = home
        self._units_dir = home / 'units'
        self._evaluation = evaluation if evaluation is not None else EvaluationSettings()
        self._agents = agents if agents is not None else AgentSettings()
        self.cas = cas if cas is not None else CAS.selected({}).open(home)
        self.sources = Sources(self.cas, home, db)
        """The artifacts of jobs: a campaign's repo of record, and its linked inputs in the CAS."""
        self._config = executor
        """The executor's ``[plugins.executor]``, as ``config.toml`` has it."""
        self._limits: tuple[tuple[str, str, datetime], ...] = ()
        """The limits that the executor has, as read: ``configure`` hands it the parameters when they change."""
        secrets = secrets if secrets is not None else SECRETS.selected({})
        results = CampaignResults(
            db,
            tracer,
            home,
            self.cas,
            lambda: self.launcher.cancelled.is_set(),
            secrets=secrets,
            exit_output_chars=self._agents.exit_output_chars,
            startup_seconds=self._agents.startup_seconds,
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
        )
        self.executor: Executor[ExecutorConfig] = self._config.open(host)
        self.launcher = Launcher(db, tracer, home, self.executor)
        self.configure()
        self._jobs: dict[int, EvaluationJob] = {}
        """The job of each evaluation that this daemon asked for, by its queue row, until the row goes: a lost one is
        submitted again. A row without one is another daemon's: a reap drops it, and what needs it asks again."""

    def configure(self) -> None:
        """Hand the executor its config with the live limits applied (``Executor.configure``), when one changed.

        The daemon calls it on each pass, so a limit holds from the next one. Limits that its parameters refuse (a
        ``config.toml`` that changed under them) leave the parameters as they were, and the event says why.
        """
        with self._db.session() as s:
            rows = s.scalars(select(Limit).order_by(Limit.key)).all()
        seen = tuple((r.key, json.dumps(r.value), r.at) for r in rows)
        if seen == self._limits:
            return
        self._limits = seen
        try:
            config = limits.applied(self._config, rows, EXECUTORS.name)
        except ValidationError as e:
            self._tracer.event('limits.refused', error=str(e))
            return
        self.executor.configure(config)
        self._tracer.event('limits.applied', limits=len(rows))

    def _wait(self, evaluation_id: int) -> bool:
        """Wait until the evaluation leaves the queue, and return True; False once the daemon stops.

        While it waits for a slot, and one is free, it submits the next ones (``dispatch``): a lost evaluation waits
        again, and nothing else may end to free a slot for it. It reads the free slots after its session: the executor
        reads in a session of its own, and one thread never holds two connections (``Database``).
        """
        while not self.launcher.cancelled.is_set():
            with self._db.session() as s:
                if (evaluation := s.get(Evaluation, evaluation_id)) is None:
                    return True
                waiting = evaluation.started_at is None
            if waiting and ((free := self.executor.capacity().free(EVALUATION)) is None or free > 0):
                self.dispatch()
            elif not self._next_wake():
                break
        return False

    def _next_wake(self) -> bool:
        """Wait for the database's next wake, a commit say: True, or False once the daemon stops."""
        woken = iter((False,))  # the first read is now: it waits for the one after
        return self._db.wait_until(lambda _: next(woken, True), give_up=self.launcher.cancelled.is_set)

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
                    if (job := self._jobs.get(evaluation.id)) is not None:
                        evaluation.started_at = utcnow()
                        starting.append(job)
                s.commit()
            for job in starting:
                self.executor.submit(job)

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
        """Run ``body``. A failure fails the run, and closes its implementer if the failure came before the launch.

        A failure before the launch is no attempt at the idea (``RunEnd.NOT_STARTED``): its agent never ran.
        """
        with self._tracer.span('piloop.run', run=run_id) as span:
            try:
                body(span)
            except Exception as e:
                span.error(e)
                error = f'{type(e).__name__}: {e}'
                with self._db.session() as s:
                    unstarted = (AgentSession.id == agent_session_id) & (
                        AgentSession.state == AgentSessionState.STARTING
                    )
                    closed = s.scalar(
                        update(AgentSession)
                        .where(unstarted)
                        .values(state=AgentSessionState.KILLED, ended_at=utcnow(), error=error)
                        .returning(AgentSession.id)
                    )
                    s.commit()
                end = RunEnd.FAILED if closed is None else RunEnd.NOT_STARTED
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
                    s.get_one(AgentSession, agent_session_id).state = AgentSessionState.REQUESTED
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
                run.status = RunStatus.RUNNING
                # A relaunch keeps the start time, so comments since then are sent to the new implementer again.
                run.started_at = run.started_at or utcnow()
                run.log_dir = str(self._runs_dir / str(run_id))
                emit(
                    s,
                    RunStarted(harness=run.harness, model=run.model),
                    TargetRef(RUN, run_id),
                    System.DAEMON,
                )
                s.commit()
        self._finish(run_id, agent_session_id, brief, span, resumed)

    def _finish(
        self, run_id: RunId, agent_session_id: AgentSessionId, brief: Cid | None, span: Span, resumed: Cid | None = None
    ) -> None:
        """Run the agent, then record the outcome, score the commits and settle the idea.

        With a ``brief``, a new agent starts in a new worktree. With None, the daemon re-attaches to the agent that is
        already in the worktree. A handed-over agent leaves the worktree and the rows to the next daemon.
        """
        with self._db.session() as s:
            run = s.get_one(Run, run_id)
            campaign = run.campaign
            cfg = campaign.config
            base_revision = s.get_one(Candidate, run.base_candidate_id).revision
            role_timeout = cfg.role(run.role).timeout
            timeout = seconds(agent_timeout(s, campaign, cfg, role_timeout, utcnow()))
            stop_cut = timeout < role_timeout  # only the stop shortens a session: a timeout then is the stop's
            harness, model, campaign_id = run.harness, run.model, campaign.id
            name, vcs, campaign_path = campaign.name, vcs_of(campaign), Path(campaign.path)
        log_dir = self._runs_dir / str(run_id)
        with closing(vcs.open(campaign_path)) as repo:
            span.set(agent_session=agent_session_id, harness=harness, model=model)
            if brief is None:
                ended = self.launcher.reattach(agent_session_id)
            else:
                ended = self.launcher.launch(
                    agent_session_id,
                    brief=brief,
                    place=run_place(run_id),
                    artifact=self.sources.agent(self._campaign(campaign_id), base_revision),
                    timeout=timeout,
                    capture=TargetRef(RUN, run_id),
                    resumed_brief=resumed,
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
                        span, run_id, campaign_id, agent_session_id, name, repo, base_revision, work, log_dir
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
                        log_dir,
                        stop_cut=stop_cut,
                    )
                case _:
                    assert_never(ended)

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
        log_dir: Path,
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
        last = self._score(run_id, campaign_id, repo, versions, log_dir) if ending is not None and versions else None
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
        log_dir: Path,
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
            if s.get_one(Run, run_id).campaign.config.evaluator.scoring is Scoring.SUBMIT:
                sent = bool(submitted(s, run_id))
            else:
                sent = any(not v.recovered for v in versions)
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
        # Score all commits, even if the implementer then timed out or crashed.
        last = self._score(run_id, campaign_id, repo, versions, log_dir) if versions else None
        self._settle(run_id, end, last)

    def _relaunch(self, span: Span, run_id: RunId, agent_session_id: AgentSessionId, log_dir: Path) -> bool:
        """Queue the run again with a new implementer, up to its role's ``startup_relaunches``, after it failed to start.

        The run stays open, so it keeps its slot, attempt number and brief. The ``implementer#<id>`` in the brief becomes the
        new implementer's id. The new implementer signs its notes with its own id. The scheduler launches the new agent session row like any other. The old implementer's transcript is
        renamed so that the new one does not overwrite it. A request made while the daemon stops waits for the next
        daemon, as every request does. A cancelled run (``run_cancel``) is not queued again.
        """
        with self.launcher.lock:
            with self._db.session() as s:
                run = s.get_one(Run, run_id)
                role = run.campaign.config.roles.doing_runs().get(run.role)
                launches = s.scalar(select(func.count()).select_from(AgentSession).where(working_on(run_id))) or 0
                if role is None or launches > role.startup_relaunches or run_cancel(s, run_id) is not None:
                    return False
                run.status = RunStatus.QUEUED
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
                relaunched = RunRelaunched(agent_session=agent_session_id, launches=launches)
                emit(s, relaunched, TargetRef(RUN, run_id), System.DAEMON)
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

        They are recorded (``RunReceived``), for ``resume``. The worker saved the unsaved work first, as a version of its own, so its candidate is ``recovered``
        (``_score``).
        """
        versions = repo.receive(self.cas, work, base_revision) if work is not None else []
        with self._db.session() as s:
            received = RunReceived(revisions=[v.revision for v in versions])
            emit(s, received, TargetRef(RUN, run_id), System.DAEMON)
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
                    last = self._score(run_id, campaign_id, repo, versions, self._runs_dir / str(run_id))
            except Exception as e:
                span.error(e)
                end = RunEnd.FAILED
            assert end is not None  # a run whose commits are scored is closed
            self._settle(run_id, end, last)

    def remeasure(self, candidate_id: CandidateId) -> None:
        """Evaluate a candidate with the campaign's current evaluator, as ``pool.due`` asks, then confirm it if due.

        Never raises. If the evaluation cannot start, for example because the commit is gone, a crashed measurement is
        recorded. It is a result like any other, so the candidate is not tried again with this evaluator unless someone
        asks (``request_remeasure``). What someone asked for gets the evaluations that they asked for, and the baseline
        and the carried best the ones that confirm them (``pool.needs``); anything else gets one. A candidate whose mean now beats the best's,
        such as a new reference, is confirmed (``_confirm``), and a run's worker hears where its candidate stands
        (``CandidateScored``). A confirmed candidate that the sealed split is due to score (``Pool.seal_due``) is only
        scored there. A baseline
        that fails and never measured pauses its campaign (``hold_on_failed_baseline``).
        """
        with self._db.session() as s:
            candidate = s.get_one(Candidate, candidate_id)
            campaign = s.get_one(Campaign, candidate.campaign_id)
            cfg = campaign.config
            evaluator_id = current_evaluator(s, campaign, cfg).id
            s.commit()
            asked = remeasure_asked(s, candidate)
            firsts = {c.id for c in (baseline(s, campaign), carried(s, campaign)) if c is not None}
            confirming = candidate.id in firsts
            repeats = asked.repeats if asked is not None else max(needs(s, campaign, candidate), 1) if confirming else 1
            visible = asked is not None or confirming or not measured(candidate, evaluator_id)
            campaign_id, revision, run_id = campaign.id, candidate.revision, candidate.run_id
            vcs, campaign_path = vcs_of(campaign), Path(campaign.path)
        eval_dir = campaign_dir(self._home, campaign_id) / 'measure' / f'{candidate_id}-e{evaluator_id}'
        try:
            with closing(vcs.open(campaign_path)):  # a repo that is gone is the candidate's crash, not a job's
                pass
            ended = (
                self._measure(None, campaign_id, cfg, revision, candidate_id, evaluator_id, eval_dir, repeats=repeats)
                if visible
                else Measuring.MEASURED
            )
        except Exception as e:  # the thread's last frame: the failure is the candidate's measurement, never lost
            with self._db.session() as s:
                error = Sample(metrics={}, seconds=0.0, error=f'{type(e).__name__}: {e}')
                record(s, self._units_dir, None, candidate_id, evaluator_id, error)
                hold_on_failed_baseline(s, s.get_one(Candidate, candidate_id), None)
                s.commit()
            return
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
        daemon or a cancel records no score.
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
                known = any(m.evaluator_id == evaluator_id for m in candidate.measurements)
                campaign_id, revision = campaign.id, candidate.revision
            if not known and evaluator_id not in tried:
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
            need = pool.confirm - pool.entries[candidate_id].measured if confirming else 0
            hidden = sealed_evaluator(s, campaign, cfg) if sealing_due else None
            s.commit()
            first = baseline(s, campaign)
            sides = [
                c for c in (candidate, first) if c is not None and hidden is not None and not measured(c, hidden.id)
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

    def _score(
        self, run_id: RunId, campaign_id: CampaignId, repo: Repository, versions: list[Version], log_dir: Path
    ) -> CandidateId | None:
        """Score the versions of a run that ended, as ``[evaluator] scoring`` says. Return the candidate of its idea.

        That candidate settles the run's idea (``release``). ``run_end``: each version, now (``_score_each``).
        ``submit``: its submissions, which ``score_submission`` scores as they come; ``_unsubmitted`` keeps the other
        versions as ``skipped``, and ``_settle`` waits for their scores.
        """
        with self._db.session() as s:
            scoring = s.get_one(Run, run_id).campaign.config.evaluator.scoring
        match scoring:
            case Scoring.SUBMIT:
                return self._unsubmitted(run_id, campaign_id, repo, versions)
            case Scoring.RUN_END:
                deprecated.until(
                    '0.7.0',
                    "0.5's scoring: delete Scoring.RUN_END, _score_each, the trailers that only it reads, the Measure"
                    " section's run_end.md and the vcs part that names them (Part.MARK), and a revision that sets"
                    ' scoring = "submit" where 0078 pinned run_end',
                )
                return self._score_each(run_id, repo, versions, log_dir)
            case never:
                assert_never(never)

    def _score_each(
        self, run_id: RunId, repo: Repository, versions: list[Version], log_dir: Path
    ) -> CandidateId | None:
        """Turn each new version into a candidate, or find its existing candidate, then score it (``_score_one``).

        Return the id of the newest candidate of the run's versions that has a score, or None: none has, or a stop cut
        the scoring. Each version is a child of the one before, the first of the run's base. A version that asks to be
        skipped (``Version.skip``) is not measured: a new candidate is ``skipped``, and never enters the pool. So a
        later version that does not ask, with the same tree, is measured like a new one. The version the worker saved
        of the implementer's unsaved work (``Version.recovered``) is not measured either: it is ``recovered``. All
        commits are candidates and pinned first, so after a stop, ``resume`` finds all of them. A version's
        ``Piloop-Note`` trailers become notes on its candidate, signed by the run's implementer.
        """
        with self._db.session() as s:
            run = s.get_one(Run, run_id)
            name, candidate, campaign_id = run.campaign.name, run.base, run.campaign_id
            ids: list[CandidateId] = []
            for version in versions:
                candidate, _ = resolve(s, campaign_id, version, parent=candidate, run_id=run_id, origin=Origin.RUN)
                ids.append(candidate.id)
            s.commit()
        for i, version in enumerate(versions):
            repo.keep(version.revision, run_pin(name, run_id, i))
        last: CandidateId | None = None
        for i, (version, candidate_id) in enumerate(zip(versions, ids, strict=True)):
            with self._tracer.span(
                'piloop.candidate', run=run_id, revision=version.revision, candidate=candidate_id
            ) as span:
                with self._db.session() as s:
                    candidate = s.get_one(Candidate, candidate_id)
                    match unmeasured_of(version):
                        case Origin() as unmeasured if candidate.run_id == run_id and not candidate.measurements:
                            candidate.origin = unmeasured
                            s.commit()
                            span.set(status=unmeasured)
                            continue
                        case None if candidate.origin in UNSCORED and candidate.revision == version.revision:
                            span.set(status=candidate.origin)  # this very version: its mark, or a cancel, holds
                            continue
                        case _:
                            if candidate.origin in UNSCORED:
                                candidate.origin = Origin.RUN  # a tree that was skipped or recovered is measured now
                            s.commit()
                match ended := self._score_one(run_id, candidate_id, log_dir / f'eval-{i}'):
                    case Measuring.STOPPED:
                        span.set(status='cancelled')
                        return None
                    case Measuring.CANCELLED:  # no score: ``queue.cancel`` made it ``skipped``
                        span.set(status='skipped')
                    case Measuring.ABANDONED:  # no score: the campaign's measurements wait for a human
                        span.set(status=ended)
                    case Measuring.MEASURED:
                        span.set(status=ended)
                        last = candidate_id
                    case never:
                        assert_never(never)
            if version.notes:
                with self._db.session() as s:
                    by = implementer_of(s, run_id).signature
                    for why in version.notes:
                        s.add(
                            Comment(
                                target=TargetRef(CANDIDATE, candidate_id), author=by, kind=CommentKind.NOTE, body=why
                            )
                        )
                    s.commit()
        return last

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
    ) -> Measuring:
        """Measure ``revision`` on the executor, and wait until its measurement is recorded. Return how it ended.

        ``repeats`` samples of ``revision``. With ``gate``, the candidate whose rungs the ladder's stops compare with
        (the best), the first sample may stop at a rung (``EvaluationJob.gate``): only a run's own version's first
        evaluation. Without, every sample climbs every rung.

        An evaluation that failed for a cause that is not its candidate's records nothing (``InfraFailed``): it waits in
        the queue, where ``piloop eval queue`` lists it and ``piloop eval cancel`` cancels it, and runs again after
        ``[evaluation] infra_retry``, twice as long after each failure up to ``infra_retry_max``. Each failure is an
        event. It ends once it measures, someone cancels it, the daemon stops, a new evaluator of its split replaces
        ``evaluator_id`` (``queue.withdraw``): the caller then asks the current one (``Measuring.REPLACED``), or it
        failed ``stop_after_tries`` times in a row: then the campaign's measurements pause, and piloop escalates
        (``hold_on_infra_errors``).
        """
        failures, retry = 0, self._evaluation.infra_retry
        while True:
            match ended := self._request(
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
            ):
                case InfraFailed(error=error):
                    failures += 1
                    if failures >= self._evaluation.stop_after_tries:
                        self._tracer.event(
                            'evaluation.abandoned', candidate=candidate_id, evaluation=ended.evaluation, error=error
                        )
                        with self._db.session() as s:
                            hold_on_infra_errors(s, candidate_id, ended.evaluation, evaluator_id, error, failures)
                            s.commit()
                        return Measuring.ABANDONED
                    if failures == self._evaluation.wake_after_tries:
                        with self._db.session() as s:
                            write_transaction(s)
                            if latest(s, InfraRetrying, has(InfraRetrying, evaluation=ended.evaluation)) is None:
                                emit(
                                    s,
                                    InfraRetrying(evaluation=ended.evaluation, failures=failures, error=error),
                                    TargetRef(CANDIDATE, candidate_id),
                                    System.DAEMON,
                                )
                            s.commit()
                    self._tracer.event(
                        ended.kind, candidate=candidate_id, error=error, failures=failures, retry=retry.total_seconds()
                    )
                    # waits in the queue meanwhile, so a cancel finds it; the lock as in ``_request``: the runs that
                    # shared the evaluation all put it back
                    with self.launcher.lock, self._db.session() as s:
                        waiting, _ = queue.request(s, candidate_id, evaluator_id)
                        s.commit()
                        row, before = waiting.id, _last_measurement(s, candidate_id, evaluator_id)
                    self.dispatch()  # as after each request: a new evaluator takes it from the queue
                    gone = self._db.wait_until(
                        lambda s, row=row: s.get(Evaluation, row) is None,
                        give_up=self.launcher.cancelled.is_set,
                        timeout=retry.total_seconds(),
                    )
                    retry = min(retry * 2, self._evaluation.infra_retry_max)
                    if self.launcher.cancelled.is_set():
                        return Measuring.STOPPED
                    if gone:  # another request ran it meanwhile, someone cancelled it, or a new evaluator took it
                        with self._db.session() as s:
                            after = _last_measurement(s, candidate_id, evaluator_id)
                            return Measuring.MEASURED if after != before else _unmeasured(s, evaluator_id)
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
    ) -> Measuring | InfraFailed:
        """Ask for one evaluation (``_measure``), and wait until it leaves the queue. Return how it ended.

        A request for a candidate that already waits or runs under the evaluator waits for that evaluation and submits
        nothing of its own (``queue.request``). It is submitted when the queue gives it a slot (``dispatch``), and its
        result records the measurement (``CampaignResults``). An evaluation that fails is a crash, a measurement too.
        A cancelled one records nothing (``queue.cancel``), nor does an infra error, which says so (``InfraFailed``).
        """
        artifact = self.sources.source(self._campaign(campaign_id), revision)
        with self.launcher.lock:
            if self.launcher.cancelled.is_set():
                return Measuring.STOPPED
            with self._db.session() as s:  # two requests for one tree must not both insert it
                before = _last_measurement(s, candidate_id, evaluator_id)
                evaluation, created = queue.request(s, candidate_id, evaluator_id)
                # a row that waits with no job of this daemon is one that an infra error put back (``_measure``)
                mine = created or (evaluation.started_at is None and evaluation.id not in self._jobs)
                job = None
                if mine:
                    score = Score.of(cfg.all_tracks[0].primary)
                    job = EvaluationJob(
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
            measured = self._wait(evaluation_id)
            self._jobs.pop(evaluation_id, None)
            self.dispatch()  # its slot is free
            if not measured or self.launcher.cancelled.is_set():  # a stop's evaluation records nothing
                span.set(status='cancelled')
                return Measuring.STOPPED
            with self._db.session() as s:
                measurement = s.scalars(
                    select(Measurement)
                    .where(
                        Measurement.candidate_id == candidate_id,
                        Measurement.evaluator_id == evaluator_id,
                        Measurement.id > (before or 0),
                    )
                    .order_by(Measurement.id.desc())
                    .limit(1)
                ).first()
                if measurement is None:
                    if (infra := latest(s, InfraFailed, has(InfraFailed, evaluation=evaluation_id))) is not None:
                        span.set(status='infra_error')
                        return infra[1]
                    span.set(status=(unmeasured := _unmeasured(s, evaluator_id)))
                    return unmeasured
                span.set(
                    measurement=measurement.id,
                    error=measurement.error,
                    **{f'metric.{k}': v for k, v in measurement.metrics.items()},
                )
        return Measuring.MEASURED

    def _close(self, span: Span, run_id: RunId, end: RunEnd, error: str | None) -> None:
        """Record the run's end when its implementer ends, so the slot is free while the commits are scored (``scoring``)."""
        with self.launcher.lock:
            if self.launcher.cancelled.is_set():  # stop() fails the row. Do not open it again.
                self._stopped(span, run_id)
                return
            with self._db.session() as s:
                close_run(s, s.get_one(Run, run_id), end, error).scoring = True
                s.commit()
        span.set(status=end.status, end=end, error=error)

    def _settle(self, run_id: RunId, end: RunEnd, last: CandidateId | None) -> None:
        """After the run's commits are scored, end its ``scoring`` and its claim (``release``).

        ``last`` is the newest candidate of the run's versions that has a score: it decides whether the idea is done.
        It first waits until each submission of the run is scored (``score_submission``), however the run ended: its
        idea is settled on them. A daemon that stops meanwhile leaves the run ``scoring``, for the next one.
        """

        def scored(s: Session) -> bool:
            waiting = {c.id for c in submissions.waiting(s, s.get_one(Run, run_id).campaign)}
            return waiting.isdisjoint(submitted(s, run_id))

        if not self._db.wait_until(scored, give_up=self.launcher.cancelled.is_set):
            return
        with self.launcher.lock:
            if self.launcher.cancelled.is_set():  # stop() ends the claim; the run's end stands
                return
            with self._db.session() as s:
                run = s.get_one(Run, run_id)
                run.scoring = False
                release(s, run, end, s.get(Candidate, last) if last is not None else None)
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


def _unmeasured(s: Session, evaluator_id: EvaluatorId) -> Literal[Measuring.CANCELLED, Measuring.REPLACED]:
    """Why an evaluation under ``evaluator_id`` left the queue with no measurement and no infra error.

    A new evaluator took it from the queue (``queue.withdraw``), or someone cancelled it (``queue.cancel``).
    """
    return Measuring.REPLACED if retired(s, evaluator_id) else Measuring.CANCELLED


def _last_measurement(s: Session, candidate_id: CandidateId, evaluator_id: EvaluatorId) -> MeasurementId | None:
    """The newest measurement of the candidate under the evaluator, by id, or None."""
    found = select(func.max(Measurement.id)).where(
        Measurement.candidate_id == candidate_id, Measurement.evaluator_id == evaluator_id
    )
    return s.scalar(found)


def wrap_up_lead(timeout: float, most: timedelta) -> float:
    """How many seconds before its role ``timeout`` an implementer is told to wrap up: a quarter, at most ``most``."""
    return min(most.total_seconds(), timeout / 4)


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
    """It submitted a version (``piloop submit``), or, in a campaign that scores every version after the run, it saved
    one of its own: not only what it left unsaved (``Version.recovered``)."""


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
    """
    run.status, run.end, run.error, run.finished_at = end.status, end, error, utcnow()
    emit(s, RunFinished(end=end, error=error), TargetRef(RUN, run.id), System.DAEMON)
    return run


def handed_off(ending: RunEndRequested) -> tuple[RunEnd, str | None]:
    """The end and error of a run that a stop hands off because its worker ended it (``run_outcome``)."""
    return run_outcome(RunFacts(ending=ending))


def unmeasured_of(version: Version) -> Unmeasured | None:
    """Return why the daemon does not measure ``version``: it is the saved unsaved work, or it asks to be skipped."""
    if version.recovered:
        return Origin.RECOVERED
    if version.skip:
        return Origin.SKIPPED
    return None


def run_place(run_id: int) -> str:
    """The place of a run's implementer under the daemon's home: its log directory, ``runs/<id>``."""
    return f'{RUNS}/{run_id}'


def implementer_of(s: Session, run_id: RunId) -> Actor:
    """The run's newest implementer, which a note on the run is by; the daemon for a run that has none."""
    w = s.scalars(select(AgentSession).where(working_on(run_id)).order_by(AgentSession.id.desc()).limit(1)).first()
    return Agent(role=w.kind, session=w.id) if w is not None else System.DAEMON


def run_pin(campaign: str, run: RunId, index: int) -> Pin:
    """The pin of a run's ``index``-th version, oldest first: ``r<run>/<index>``."""
    return Pin(campaign, f'r{run}/{index}')
