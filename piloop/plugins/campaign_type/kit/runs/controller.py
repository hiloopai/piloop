"""Execute one run: make a worktree, run the worker, close the run, score its commits and end its claim.

The run closes when its implementer ends, so the implementer slot is free while the daemon measures the commits. The idea stays
``claimed`` until its candidates are judged, because a dependent idea builds on the keeps of its prerequisites.
"""

import json
import re
import threading
from collections import defaultdict
from collections.abc import Callable
from contextlib import ExitStack, closing
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
from functools import partial
from pathlib import Path
from typing import ClassVar, Final, Literal, assert_never

from covalence import Cid
from pydantic import ValidationError
from sqlalchemy import delete, func, select, update
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
    Event,
    EventModel,
    Limit,
    ReadBy,
    utcnow,
)
from piloop.db.session import Database, write_transaction
from piloop.ids import AgentSessionId, CampaignId, Revision, RoleName, TargetKind, TargetRef
from piloop.net.tokens import Tokens
from piloop.otel import Span, Tracer
from piloop.plugins import limits
from piloop.plugins.campaign_type.kit.backlog.tables import Idea
from piloop.plugins.campaign_type.kit.candidates.ids import CANDIDATE, CandidateId, Cause, Origin, Outcome
from piloop.plugins.campaign_type.kit.candidates.references import resolve
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.evaluation import queue
from piloop.plugins.campaign_type.kit.evaluation.config import Metric, Scoring, Track
from piloop.plugins.campaign_type.kit.evaluation.evaluate import Score
from piloop.plugins.campaign_type.kit.evaluation.ids import EvaluatorId, MeasurementId
from piloop.plugins.campaign_type.kit.evaluation.jobs import EVALUATION, Bar, EvaluationJob, InfraFailed, Sample, record
from piloop.plugins.campaign_type.kit.evaluation.measure import (
    Split,
    current_evaluator,
    gate_of,
    measured,
    noise,
    remeasure_asked,
    score,
    sealed_evaluator,
)
from piloop.plugins.campaign_type.kit.evaluation.ops import hold_on_failed_baseline, noise_unknown
from piloop.plugins.campaign_type.kit.evaluation.submissions import submitted
from piloop.plugins.campaign_type.kit.evaluation.tables import Evaluation, Evaluator, Measurement
from piloop.plugins.campaign_type.kit.runs.ids import RUN, RunEnd, RunEnding, RunId, RunStatus
from piloop.plugins.campaign_type.kit.runs.ops import RunEndRequested, end_requested
from piloop.plugins.campaign_type.kit.runs.stop import release
from piloop.plugins.campaign_type.kit.runs.tables import Run, working_on
from piloop.plugins.campaign_type.kit.verdicts.judge import (
    MOVED,
    CandidateJudged,
    Evidence,
    OnTrack,
    RejudgeFailed,
    answer_rejudge,
    bar_of,
    baseline,
    case_of,
    criterion,
    fallback_track,
    head,
    judge_tracks,
    on_moved,
    reproposed,
    short_of_noise,
    track_of,
    unjudged,
    verdict_rule,
)
from piloop.plugins.campaign_type.kit.verdicts.verdict import (
    UNMEASURED,
    judgeable,
    label,
    note,
    pending,
    refusal,
    rejudgeable,
    write,
)
from piloop.plugins.campaign_type.kit.verdicts.verdict_rule.contract import Sealed
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

RECOVERED_REASON = 'recovered: what the implementer of run#{run} left unsaved, never declared ready; not measured'
CUT_REASON = 'recovered: where run#{run} was when a daemon stop cut it, never declared ready; not measured'
SKIPPED_REASON = 'not evaluated: its version asked to be skipped'
type Unmeasured = Literal[Origin.RECOVERED, Origin.SKIPPED]
"""A version that the daemon does not measure: its recovered work, or one that asked to be skipped."""
UNMEASURED_REASONS: Final[dict[Unmeasured, str]] = {Origin.RECOVERED: RECOVERED_REASON, Origin.SKIPPED: SKIPPED_REASON}
NOT_ENDED = 'its worker stopped without `piloop run end`'
UNSUBMITTED = 'not submitted: never measured'
"""The reason of a version of a run that its implementer did not submit (``[evaluator] scoring = "submit"``)."""
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
    """Someone cancelled it (``queue.cancel``): no measurement, and no verdict."""
    STOPPED = 'daemon_stopped'
    """The daemon stopped: the next one asks again."""


type Halted = Literal[Measuring.CANCELLED, Measuring.STOPPED]
"""Why a run's candidate has no measurement to judge it on."""


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
        self._heads: defaultdict[CampaignId, threading.Lock] = defaultdict(threading.Lock)
        """One lock per campaign, around the judgement of a submission against its head: two submissions that beat the
        head at once are judged one after the other, the second against the first."""

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
        again, and nothing else may end to free a slot for it.
        """

        def done_or_dispatchable(s: Session) -> bool:
            evaluation = s.get(Evaluation, evaluation_id)
            if evaluation is None or evaluation.started_at is not None:
                return evaluation is None
            return (free := self.executor.capacity().free(EVALUATION)) is None or free > 0

        while self._db.wait_until(done_or_dispatchable, give_up=self.launcher.cancelled.is_set):
            with self._db.session() as s:
                if s.get(Evaluation, evaluation_id) is None:
                    return True
            self.dispatch()
        return False

    def dispatch(self) -> None:
        """Submit the next evaluations of the queue while the executor has free slots, in the queue's order.

        The order and the fairness are the queue's (``queue.waiting``), whichever the executor. Called after each
        request, after each evaluation that ends, and on each pass.
        """
        with self.launcher.lock:
            if self.launcher.cancelled.is_set():
                return
            free = self.executor.capacity().free(EVALUATION)
            with self._db.session() as s:
                write_transaction(s)
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
                        reason='a free slot',
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
            timeout = seconds(agent_timeout(s, campaign, cfg, cfg.role(run.role).timeout, utcnow()))
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
                    self._ran(span, run_id, campaign_id, agent_session_id, repo, base_revision, ended, log_dir)
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
    ) -> None:
        """Close the run of an agent that worked (``run_outcome``), score its versions and settle its idea."""
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
            scoring = s.get_one(Run, run_id).campaign.config.evaluator.scoring
        facts = RunFacts(
            ending=ending,
            ended=stop_ended is not None,
            cancelled=cancelled,
            timed_out=outcome.status is ExitStatus.TIMED_OUT,
            error=outcome.error,
            unsaved=unsaved[1].error if unsaved is not None else None,
            scoring=scoring,
            committed=any(not v.recovered for v in versions),
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
        commits are not measured: each new one is ``recovered`` (``CUT_REASON``). A run whose implementer had left its
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
                candidate, created = resolve(
                    s, run.campaign_id, version, parent=candidate, run_id=run_id, origin=Origin.RECOVERED
                )
                if created:
                    candidate.verdict_reason = CUT_REASON.format(run=run_id)
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
        """Measure a candidate with the campaign's current evaluator, then judge it again if it is ``rejudgeable``.

        Never raises. If the evaluation cannot start, for example because the commit is gone, a crashed measurement is
        recorded. It is a result like any other, so the candidate is not tried again with this evaluator unless someone
        asks (``ledger.request_remeasure``). What someone asked for is a paired evaluation: ``repeats`` samples, each
        next to one of its bar's (``bar_of`` its parent). Then a ``crash``, ``discard`` or ``interesting`` that the
        daemon judged is judged again on the pairs (``_rejudge``), as after ``piloop measurement add``. What runs need,
        the head and the bars, gets one sample. A baseline that fails and never measured pauses its campaign
        (``hold_on_failed_baseline``).
        """
        with self._db.session() as s:
            candidate = s.get_one(Candidate, candidate_id)
            campaign = s.get_one(Campaign, candidate.campaign_id)
            cfg = campaign.config
            evaluator_id = current_evaluator(s, campaign, cfg).id
            s.commit()
            asked = remeasure_asked(s, candidate)
            parent = candidate.parent
            # a merge that no run judges is measured as its run would: paired with its head when the track repeats
            paired = (
                fallback_track(cfg, candidate.run.idea.track if candidate.run.idea else None).repeats
                if unjudged(candidate) and candidate.run
                else None
            )
            bar = bar_of(s, parent).id if (asked is not None or paired) and parent is not None else None
            repeats = (
                asked.repeats if asked is not None else paired or short_of_noise(candidate, evaluator_id, campaign) or 1
            )
            campaign_id, revision = campaign.id, candidate.revision
            vcs, campaign_path = vcs_of(campaign), Path(campaign.path)
        eval_dir = campaign_dir(self._home, campaign_id) / 'measure' / f'{candidate_id}-e{evaluator_id}'
        with ExitStack() as opened:
            try:
                repo = opened.enter_context(closing(vcs.open(campaign_path)))
                ended = self._measure(
                    None, campaign_id, cfg, revision, candidate_id, evaluator_id, eval_dir, bar=bar, repeats=repeats
                )
                if ended is not Measuring.MEASURED:
                    return  # a stop killed the evaluator, or someone cancelled it: there is nothing new to judge
            except Exception as e:
                with self._db.session() as s:
                    error = Sample(metrics={}, seconds=0.0, error=f'{type(e).__name__}: {e}')
                    record(s, self._units_dir, None, candidate_id, evaluator_id, error)
                    hold_on_failed_baseline(s, s.get_one(Candidate, candidate_id), None)
                    s.commit()
                return
            with self._db.session() as s:
                hold_on_failed_baseline(s, s.get_one(Candidate, candidate_id), eval_dir)
                s.commit()
            self._rejudge(candidate_id, repo, eval_dir)

    def _unsubmitted(
        self, run_id: RunId, campaign_id: CampaignId, repo: Repository, versions: list[Version]
    ) -> CandidateId | None:
        """Keep each new version of the run as ``skipped``, and return its newest submission, or None.

        A version that it submitted is its submission's candidate. A tree that is a candidate already stays as it is.
        The submissions settle the run's idea, not a skipped version, which has no verdict. ``_settle`` waits for their
        verdicts.
        """
        with self._db.session() as s:
            run = s.get_one(Run, run_id)
            name, candidate = run.campaign.name, run.base
            for version in versions:
                candidate, created = resolve(
                    s, campaign_id, version, parent=candidate, run_id=run_id, origin=Origin.SKIPPED
                )
                if created:
                    candidate.verdict_reason = UNSUBMITTED
            s.commit()
            mine = submitted(s, run_id)
        for i, version in enumerate(versions):
            repo.keep(version.revision, run_pin(name, run_id, i))
        return mine[-1] if mine else None

    def judge_submission(self, candidate_id: CandidateId) -> None:
        """Measure a submitted candidate and what the verdict rule needs, and judge it against the head. Never raises.

        The head is the bar: a submission becomes the head only when it beats the head. One that the head moved past
        while it was measured is measured against the new head too. A failure is no verdict: the span records it, the
        candidate stays pending, and the next pass of the daemon judges it again.
        """
        with self._tracer.span('piloop.submission', candidate=candidate_id) as span:
            try:
                self._judge_submission(candidate_id, span)
            except Exception as e:  # the thread's last frame: what failed goes into the span, never lost
                span.error(e)

    def _judge_submission(self, candidate_id: CandidateId, span: Span) -> None:
        with self._db.session() as s:
            candidate = s.get_one(Candidate, candidate_id)
            run = candidate.run
            assert run is not None  # a submission is a run's candidate
            campaign = s.get_one(Campaign, candidate.campaign_id)
            run_id, campaign_id, vcs, path = run.id, campaign.id, vcs_of(campaign), Path(campaign.path)
        eval_dir = self._runs_dir / str(run_id) / f'submission-{candidate_id}'
        with closing(vcs.open(path)) as repo:
            while True:
                with self._db.session() as s:
                    campaign = s.get_one(Campaign, campaign_id)
                    candidate = s.get_one(Candidate, candidate_id)
                    best = head(s, campaign, excluding=candidate) or baseline(s, campaign)
                    assert best is not None  # a campaign with a run has a baseline
                    bar_id, judge_now = best.id, pending(candidate)
                    gate = bar_id if judge_now else None
                match self._ready(run_id, repo, candidate_id, eval_dir, bar=bar_id, gate=gate, judge_now=judge_now):
                    case Measuring.STOPPED | Measuring.CANCELLED as halted:
                        span.set(status=halted)
                        return
                    case Scope() as scope:
                        pass
                    case never:
                        assert_never(never)
                with self._heads[campaign_id]:
                    with self._db.session() as s:
                        now = head(s, s.get_one(Campaign, campaign_id), excluding=s.get_one(Candidate, candidate_id))
                    if now is not None and now.id != bar_id:
                        continue  # the head moved while it was measured: beat the new one
                    self._judge(scope, candidate_id, bar_id, span, judge_now=judge_now, unmeasured=None, notes=[])
                    return

    def rejudge(self, candidate_id: CandidateId) -> None:
        """Judge again a candidate that someone measured by hand (``ledger.rejudges``), with all its measurements.

        Never raises. When the judgement fails, for example because the repo is gone, a ``RejudgeFailed`` event records
        the error, and the request is answered, so it is not tried on every pass.
        """
        with self._db.session() as s:
            candidate = s.get_one(Candidate, candidate_id)
            campaign = s.get_one(Campaign, candidate.campaign_id)
            campaign_id, vcs, campaign_path = campaign.id, vcs_of(campaign), Path(campaign.path)
        try:
            eval_dir = campaign_dir(self._home, campaign_id) / 'measure' / f'{candidate_id}-rejudge'
            with closing(vcs.open(campaign_path)) as repo:
                self._rejudge(candidate_id, repo, eval_dir)
        except Exception as e:
            with self._db.session() as s:
                failed = RejudgeFailed(error=f'{type(e).__name__}: {e}')
                emit(s, failed, TargetRef(CANDIDATE, candidate_id), System.DAEMON)
                answer_rejudge(s, candidate_id)
                s.commit()
            self._tracer.event(failed.kind, candidate=candidate_id, error=str(e))

    def _rejudge(self, candidate_id: CandidateId, repo: Repository, log_dir: Path) -> None:
        """Judge again a run's candidate that is ``rejudgeable``, now that it has a new measurement.

        It is judged as its run would have judged it (``_judge``): against the bar of its parent, a keep moves the head
        only while that bar is the head, and else it is rebased onto the head (``_follow``). A crash that crashes again
        stays a crash.
        """
        with self._db.session() as s:
            candidate = s.get_one(Candidate, candidate_id)
            run = candidate.run
            if run is None or not (rejudgeable(candidate) or unjudged(candidate)):
                answer_rejudge(s, candidate_id)  # its verdict stays: nothing to judge
                s.commit()
                return
            assert candidate.parent is not None  # a run's candidate descends from the run's base
            scope = Scope.now(s, run, repo)
            s.commit()
            bar_id, revision = bar_of(s, candidate.parent).id, candidate.revision
        with self._tracer.span('piloop.candidate', run=scope.run_id, revision=revision, candidate=candidate_id) as span:
            notes = list(repo.version(revision).notes)
            judged = self._judge(scope, candidate_id, bar_id, span, judge_now=True, unmeasured=None, notes=notes)
        if judged.merged is not None:
            self._follow(scope, judged.merged, log_dir)

    def _score(
        self, run_id: RunId, campaign_id: CampaignId, repo: Repository, versions: list[Version], log_dir: Path
    ) -> CandidateId | None:
        """Score the versions of a run that ended, as ``[evaluator] scoring`` says. Return the candidate of its idea.

        That candidate settles the run's idea (``release``). ``run_end``: each version, now (``_score_each``).
        ``submit``: its submissions, which ``judge_submission`` judges as they come; ``_unsubmitted`` keeps the other
        versions as ``skipped``, and ``_settle`` waits for the verdicts.
        """
        with self._db.session() as s:
            scoring = s.get_one(Run, run_id).campaign.config.evaluator.scoring
        match scoring:
            case Scoring.SUBMIT:
                return self._unsubmitted(run_id, campaign_id, repo, versions)
            case Scoring.RUN_END:
                deprecated.until(
                    '0.7.0',
                    "0.5's scoring: delete Scoring.RUN_END, _score_each and the trailers that only it reads, and a"
                    ' revision that sets scoring = "submit" where 0078 pinned run_end',
                )
                return self._score_each(run_id, campaign_id, repo, versions, log_dir)
            case never:
                assert_never(never)

    def _score_each(
        self, run_id: RunId, campaign_id: CampaignId, repo: Repository, versions: list[Version], log_dir: Path
    ) -> CandidateId | None:
        """Turn each new version into a candidate, or find its existing candidate, then measure and judge it.

        Return the id of the newest candidate of the run's versions that got a verdict, or None: none did, or scoring
        was cancelled. A tree that is already a candidate keeps that candidate, and its verdict unless another run
        proposes it again (``ledger.reproposed``).

        The daemon judges each candidate with the campaign's config and evaluator at that moment, not at launch, against
        a bar that the same evaluator measured (``_measure_with_bar``). A run that outlives a config change must not move the campaign back to the old evaluator. All commits are candidates and pinned first, so a
        commit that waits for the evaluations before it is a ``pending`` candidate, and after a stop, ``resume`` finds
        all of them. It judges the candidates that the stop left pending, including rebased ones
        (``_follow``).

        Each candidate is judged on every track, the run's first (``judge_tracks``), against a bar. The bar starts at
        the bar of the run's base (``ledger.bar_of``). Each keep becomes the new bar. A run keeps on the
        trunk only while its bar is the head. If the head has moved past the bar, a
        keep is rebased onto the head and the merge is judged there (``_judge``, ``_follow``). A commit whose tree is
        already a candidate is that candidate. It is measured only if the current evaluator has not measured it, and it
        keeps its verdict, unless another run reached it (``ledger.reproposed``): then it is judged again on this
        run's bar.

        A version that asks to be skipped (``Version.skip``) is not measured: a new candidate is ``skipped``, with no
        verdict, and moves no bar. So a later version that does not ask, with the same tree, is measured and judged like
        a new one. Its notes are applied when it is judged (``_judge``). The version the worker saved of the
        implementer's unsaved work (``Version.recovered``) is not measured either: it is ``recovered``, and the same tree
        saved later is measured and judged like a new one.
        """
        with self._db.session() as s:
            run = s.get_one(Run, run_id)
            name = run.campaign.name
            scope = Scope.now(s, run, repo)
            bar_id = bar_of(s, run.base).id
            candidate = run.base
            ids: list[CandidateId] = []
            for version in versions:
                candidate, _ = resolve(s, campaign_id, version, parent=candidate, run_id=run_id, origin=Origin.RUN)
                ids.append(candidate.id)
            s.commit()
        for i, version in enumerate(versions):
            repo.keep(version.revision, run_pin(name, run_id, i))
        for i, (version, candidate_id) in enumerate(zip(versions, ids, strict=True)):
            revision, unmeasured = version.revision, unmeasured_of(version)
            with self._db.session() as s:
                scope = Scope.now(s, s.get_one(Run, run_id), repo)
                s.commit()
                candidate = s.get_one(Candidate, candidate_id)
                match candidate.origin:
                    case Origin.RECOVERED | Origin.SKIPPED as never if candidate.revision == revision:
                        unmeasured = never  # this very version: its mark, or a cancel of its evaluation, holds
                    case _:
                        pass
                again = candidate.origin in UNMEASURED or reproposed(candidate, s.get_one(Run, run_id))
                judge_now = pending(candidate) or (unmeasured is None and again)
                known = measured(candidate, scope.evaluator_id)
                gate = bar_id if judgeable(candidate) else None
            with self._tracer.span('piloop.candidate', run=run_id, revision=revision, candidate=candidate_id) as span:
                if unmeasured is not None:
                    span.set(status=unmeasured)
                else:
                    if known:
                        span.set(status='known')
                        self._tracer.event('candidate.known', run=run_id, revision=revision, candidate=candidate_id)
                    match self._ready(
                        run_id, repo, candidate_id, log_dir / f'eval-{i}', bar=bar_id, gate=gate, judge_now=judge_now
                    ):
                        case Measuring.STOPPED:
                            span.set(status='cancelled')
                            return None
                        case Measuring.CANCELLED:  # no verdict: ``queue.cancel`` made it ``skipped``
                            span.set(status='skipped')
                            continue
                        case Scope() as measured_in:
                            scope = measured_in
                        case never:
                            assert_never(never)
                judged = self._judge(
                    scope,
                    candidate_id,
                    bar_id,
                    span,
                    judge_now=judge_now,
                    unmeasured=unmeasured,
                    notes=list(version.notes),
                )
            if judged.merged is not None and not self._follow(scope, judged.merged, log_dir):
                return None
            if judged.advanced:
                bar_id = candidate_id
        with self._db.session() as s:
            shas = [v.revision for v in versions]
            mine = (
                Candidate.run_id == run_id,
                Candidate.origin == Origin.RUN,
                Candidate.verdict.is_(None),
                Candidate.revision.not_in(shas),
            )
            left = s.scalars(select(Candidate.id).where(*mine).order_by(Candidate.id)).all()
        for candidate_id in left:  # Rebased candidates that a daemon stop left unjudged.
            if not self._follow(scope, candidate_id, log_dir):
                return None
        with self._db.session() as s:
            judged = s.scalars(select(Candidate.id).where(Candidate.id.in_(ids), Candidate.verdict.is_not(None)))
            return max(judged, default=None)

    def _follow(self, scope: Scope, candidate_id: CandidateId, log_dir: Path) -> bool:
        """Measure a rebased candidate and judge it against its parent, the head it was merged onto.

        If the head moved again and the merge keeps against its parent, the merge is rebased in turn (``_judge``). This
        repeats until a merge is judged on the head or a merge conflicts. Returns False if the run was cancelled.
        """
        current: CandidateId | None = candidate_id
        while current is not None:
            with self._db.session() as s:
                candidate = s.get_one(Candidate, current)
                revision, parent_id = candidate.revision, candidate.parent_id
                judge_now = pending(candidate)
                gate = parent_id if judgeable(candidate) else None
            assert parent_id is not None  # A rebased candidate's parent is the head it was merged onto.
            with self._tracer.span('piloop.candidate', run=scope.run_id, revision=revision, candidate=current) as span:
                span.set(rebased=True)
                eval_dir = log_dir / f'eval-rebased-{current}'
                ready = self._ready(
                    scope.run_id, scope.repo, current, eval_dir, bar=parent_id, gate=gate, judge_now=judge_now
                )
                match ready:
                    case Measuring.STOPPED:
                        span.set(status='cancelled')
                        return False
                    case Measuring.CANCELLED:  # no verdict: ``queue.cancel`` made it ``skipped``
                        span.set(status='skipped')
                        return True
                    case Scope() as measured_in:
                        scope = measured_in
                    case never:
                        assert_never(never)
                judged = self._judge(scope, current, parent_id, span, judge_now=judge_now, unmeasured=None, notes=[])
            current = judged.merged
        return True

    def _measure_with_bar(
        self,
        run_id: RunId,
        repo: Repository,
        candidate_id: CandidateId,
        eval_dir: Path,
        *,
        bar: CandidateId,
        gate: CandidateId | None,
    ) -> Scope | Halted:
        """Measure a run's candidate and its ``bar``, then return the scope to judge the candidate in.

        Returns how it halted instead when the daemon stopped, or someone cancelled the candidate's evaluation. Each is
        measured once under the scope's evaluator unless that evaluator
        measured it already (``measured``), and the scope is read again after each measurement. So when ``piloop init``
        registers an evaluator meanwhile, both are measured under the new one: a candidate is never judged under an old
        evaluator, nor against a bar that its evaluator has not measured. The bar is measured as the scheduler measures
        the bars of open runs (``remeasure``, ``judge.due``), and a run joins that evaluation when the scheduler queued
        it first.
        """
        tried = set[tuple[CandidateId, EvaluatorId]]()  # a climb that stopped at a rung is not ``measured``
        while True:
            with self._db.session() as s:
                scope = Scope.now(s, s.get_one(Run, run_id), repo)
                s.commit()
                both = (s.get_one(Candidate, bar), s.get_one(Candidate, candidate_id))
                ev = scope.evaluator_id
                todo = [c.id for c in both if (c.id, ev) not in tried and not measured(c, ev)]
                revision = both[1].revision
            match todo:
                case []:
                    return scope
                case [first, *_] if first == bar:
                    tried.add((bar, scope.evaluator_id))
                    self.remeasure(bar)
                    if self.launcher.cancelled.is_set():
                        return Measuring.STOPPED
                case _:
                    tried.add((candidate_id, scope.evaluator_id))
                    ended = self._measure(
                        run_id,
                        scope.campaign_id,
                        scope.cfg,
                        revision,
                        candidate_id,
                        scope.evaluator_id,
                        eval_dir,
                        bar=bar if scope.track.repeats else None,
                        repeats=scope.track.repeats or 1,
                        gate=gate,
                    )
                    if ended is not Measuring.MEASURED:
                        return ended

    def _ready(
        self,
        run_id: RunId,
        repo: Repository,
        candidate_id: CandidateId,
        eval_dir: Path,
        *,
        bar: CandidateId,
        gate: CandidateId | None,
        judge_now: bool,
    ) -> Scope | Halted:
        """Measure a run's candidate and its ``bar``, and what the verdict rule needs when it is to be judged now.

        Return the scope to judge it in (``_measure_with_bar``, ``_gather``), or how it halted.
        """
        match measured := self._measure_with_bar(run_id, repo, candidate_id, eval_dir, bar=bar, gate=gate):
            case Scope() if judge_now:
                return self._gather(measured, candidate_id, bar, eval_dir) or measured
            case _:
                return measured

    def _evidence(self, s: Session, scope: Scope, candidate: Candidate, bar: Candidate) -> Evidence:
        """What the verdict on ``candidate`` against ``bar`` reads beyond them.

        That is the noise of each track's primary over the baseline, and the sealed split's scores once both sides
        have one (``_gather``). With ``noise_runs`` below 2 the noise is 0: any gain counts. A noise that the baseline's
        evaluations cannot give, because too few succeeded, is left out, and escalated once (``noise_unknown``).
        """
        campaign = s.get_one(Campaign, scope.campaign_id)
        first = baseline(s, campaign)
        runs = scope.cfg.evaluator.noise_runs
        found: dict[str, float] = {}
        for t in scope.cfg.all_tracks:
            name = t.primary.name
            if runs < 2 or first is None:
                found[name] = 0.0
            elif (sigma := noise(first, scope.evaluator_id, name)) is not None:
                found[name] = sigma
            else:
                noise_unknown(s, campaign, first, name)
        hidden = sealed_evaluator(s, campaign, scope.cfg)
        if hidden is None or not all(measured(c, hidden.id) for c in (candidate, bar)):
            return Evidence(found)
        primary = scope.cfg.all_tracks[0].primary
        return Evidence(found, Sealed(score(candidate, hidden.id, primary), score(bar, hidden.id, primary)))

    def _gather(self, scope: Scope, candidate_id: CandidateId, bar_id: CandidateId, eval_dir: Path) -> Halted | None:
        """Measure what the verdict rule needs before it judges ``candidate_id`` against ``bar_id``. None when done.

        First the measurements that it asks for (``VerdictRule.samples``), once. Then, for a would-be keep of a
        campaign with a sealed split, the split's score of the candidate and of the bar that it would replace: the bar
        is scored there once, and once more when that crashed, a fault of the split, not of the candidate.
        """
        rule, resampled, resealed = verdict_rule(scope.cfg), False, False
        while True:
            with self._db.session() as s:
                candidate, bar = s.get_one(Candidate, candidate_id), s.get_one(Candidate, bar_id)
                evidence = self._evidence(s, scope, candidate, bar)
                case = case_of(candidate, bar, scope.evaluator_id, scope.cfg, scope.track, self._units_dir, evidence)
                more = 0 if resampled else rule.samples(case) - case.candidate.measured
                hidden = sealed_evaluator(s, s.get_one(Campaign, scope.campaign_id), scope.cfg)
                s.commit()
                sealing = [
                    c
                    for c in (candidate, bar)
                    if hidden is not None and evidence.sealed is None and not measured(c, hidden.id)
                ]
                if evidence.sealed is not None and evidence.sealed.bar is None and not resealed:
                    sealing, resealed = [bar], True
                revisions = {c.id: c.revision for c in (candidate, bar)}
            if more > 0:
                resampled = True
                paired = bar_id if scope.track.repeats else None
                ended = self._measure(
                    scope.run_id,
                    scope.campaign_id,
                    scope.cfg,
                    revisions[candidate_id],
                    candidate_id,
                    scope.evaluator_id,
                    eval_dir,
                    bar=paired,
                    repeats=more,
                )
                if ended is not Measuring.MEASURED:
                    return ended
                continue
            if not sealing or hidden is None or rule.decide(replace(case, sealed=None)).verdict is not Outcome.KEEP:
                return None
            for c in sealing:
                ended = self._measure(
                    scope.run_id,
                    scope.campaign_id,
                    scope.cfg,
                    revisions[c.id],
                    c.id,
                    hidden.id,
                    eval_dir / f'sealed-{c.id}',
                    split=Split.SEALED,
                )
                if ended is not Measuring.MEASURED:
                    return ended

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
        bar: CandidateId | None = None,
        repeats: int = 1,
        gate: CandidateId | None = None,
        split: Split = Split.VISIBLE,
    ) -> Measuring:
        """Measure ``revision`` on the executor, and wait until its measurement is recorded. Return how it ended.

        With ``bar``, it is a paired evaluation: ``repeats`` samples of ``revision``, each next to one of the bar's
        (``EvaluationJob.bar``). Without, ``repeats`` samples of ``revision`` alone. With ``gate``, the candidate whose
        rungs the ladder's stops compare with, the first sample may stop at a rung (``EvaluationJob.gate``): only a run's
        own version that the daemon may judge. Without, every sample climbs every rung, as what may become a bar must.

        An evaluation that failed for a cause that is not its candidate's records nothing (``InfraFailed``): it waits in
        the queue, where ``piloop eval queue`` lists it and ``piloop eval cancel`` cancels it, and runs again after
        ``[evaluation] infra_retry``, twice as long after each failure up to ``infra_retry_max``. Each failure is an
        event. It ends once it measures, someone cancels it, the daemon stops, or, on the sealed split, a new
        ``[evaluator] sealed`` replaces its command (``_replaced``): the candidate's next judgement uses the new one.
        """
        failures = 0
        while True:
            match ended := self._request(
                run_id, campaign_id, cfg, revision, candidate_id, evaluator_id, eval_dir, bar, repeats, gate, split
            ):
                case InfraFailed(error=error):
                    retry = min(self._evaluation.infra_retry * 2**failures, self._evaluation.infra_retry_max)
                    failures += 1
                    self._tracer.event(
                        ended.kind, candidate=candidate_id, error=error, failures=failures, retry=retry.total_seconds()
                    )
                    # waits in the queue meanwhile, so a cancel finds it; the lock as in ``_request``: the runs that
                    # shared the evaluation all put it back
                    with self.launcher.lock, self._db.session() as s:
                        waiting, _ = queue.request(s, candidate_id, evaluator_id)
                        s.commit()
                        row, before = waiting.id, _last_measurement(s, candidate_id, evaluator_id)
                    gone = self._db.wait_until(
                        lambda s, row=row: s.get(Evaluation, row) is None,
                        give_up=self.launcher.cancelled.is_set,
                        timeout=retry.total_seconds(),
                    )
                    if self.launcher.cancelled.is_set():
                        return Measuring.STOPPED
                    if gone:  # another request ran it meanwhile, or someone cancelled it
                        with self._db.session() as s:
                            after = _last_measurement(s, candidate_id, evaluator_id)
                        return Measuring.MEASURED if after != before else Measuring.CANCELLED
                    if split is Split.SEALED and self._replaced(campaign_id, evaluator_id, row):
                        return Measuring.CANCELLED
                case Measuring():
                    return ended
                case never:
                    assert_never(never)

    def _replaced(self, campaign_id: CampaignId, evaluator_id: EvaluatorId, row: int) -> bool:
        """Whether the campaign's sealed command is no longer ``evaluator_id``'s.

        Then ``row``, its evaluation that waits to run again, leaves the queue.
        """
        with self.launcher.lock, self._db.session() as s:
            campaign = s.get_one(Campaign, campaign_id)
            current = sealed_evaluator(s, campaign, campaign.config)
            if current is not None and current.id == evaluator_id:
                return False
            s.execute(delete(Evaluation).where(Evaluation.id == row, Evaluation.started_at.is_(None)))
            s.commit()
            return True

    def _request(
        self,
        run_id: RunId | None,
        campaign_id: CampaignId,
        cfg: CampaignConfig,
        revision: Revision,
        candidate_id: CandidateId,
        evaluator_id: EvaluatorId,
        eval_dir: Path,
        bar: CandidateId | None,
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
                    paired = None
                    if bar is not None:
                        bar_commit = s.get_one(Candidate, bar).revision
                        paired = Bar(candidate=bar, artifact=artifact.model_copy(update={'revision': bar_commit}))
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
                        bar=paired,
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
                    infra = latest(s, InfraFailed, has(InfraFailed, evaluation=evaluation_id))
                    span.set(status='infra_error' if infra is not None else 'cancelled')
                    return Measuring.CANCELLED if infra is None else infra[1]
                span.set(
                    measurement=measurement.id,
                    error=measurement.error,
                    **{f'metric.{k}': v for k, v in measurement.metrics.items()},
                )
        return Measuring.MEASURED

    def _judge(
        self,
        scope: Scope,
        candidate_id: CandidateId,
        bar_id: CandidateId,
        span: Span,
        *,
        judge_now: bool,
        unmeasured: Unmeasured | None,
        notes: list[str],
    ) -> Judged:
        """Set the daemon's verdict on a candidate while it is ``judgeable``, if ``judge_now``. Return the result.

        A known candidate keeps its verdict, because the same tree again (a revert) says nothing new about it, unless
        another run proposes it again (``_score``). ``judgeable`` is read in the transaction that judges, so a keep
        that another run made meanwhile, of the same tree, stays. An ``unmeasured`` commit gets no verdict: its origin
        is ``recovered`` or ``skipped`` instead, with the reason of ``UNMEASURED_REASONS``.

        A keep against a bar that is no longer the head stays off the trunk. The head
        moved while the run worked or while the daemon measured. The candidate becomes ``interesting``
        (``Cause.MOVED``), and what the verdict rule says follows is done in the same transaction (``ledger.on_moved``):
        by default its merge onto the head, which is judged next (``Judged.merged``). See ``_verdict``.

        Otherwise the commit's ``Piloop-Note`` ``notes`` are added after the verdict, signed by the run. They turn a
        discard or an interesting candidate into a noted ``interesting`` (``Cause.NOTE``). A keep or a crash keeps
        its verdict (``verdict.refusal``).
        """
        merged: CandidateId | None = None
        moved = False
        with self._db.session() as s:
            candidate = s.get_one(Candidate, candidate_id)
            if judge_now and judgeable(candidate):
                match unmeasured:
                    case Origin() as origin:
                        candidate.origin = origin
                        candidate.verdict_reason = UNMEASURED_REASONS[origin].format(run=scope.run_id)
                    case None:
                        leader = head(s, s.get_one(Campaign, candidate.campaign_id), excluding=candidate)
                        judged, onto = self._verdict(s, scope, candidate, bar_id)
                        *others, best = judged
                        cause = Cause.MOVED if onto is not None else None
                        write(candidate, best.verdict, cause=cause, reason=best.reason, by=System.DAEMON)
                        candidate.track = best.track.name
                        if onto is not None:
                            moved = True
                            rebased = on_moved(s, scope.cfg, scope.repo, candidate, onto)
                            merged = rebased.rebased if rebased is not None else None
                        target = TargetRef(CANDIDATE, candidate_id)
                        for j in (*others, replace(best, reason=candidate.verdict_reason or best.reason)):
                            primary = j.track.primary
                            mine = score(candidate, scope.evaluator_id, primary)
                            on = CandidateJudged(
                                verdict=j.verdict,
                                score=mine,
                                reason=j.reason,
                                track=j.track.name,
                                parent_delta=delta(mine, candidate.parent, scope.evaluator_id, primary),
                                best_delta=delta(mine, leader, scope.evaluator_id, primary),
                            )
                            emit(s, on, target, System.DAEMON)
                        answer_rejudge(s, candidate_id)
                        why = '; '.join(notes)
                        if why and not moved and refusal(candidate, Outcome.INTERESTING, Cause.NOTE) is None:
                            by = implementer_of(s, scope.run_id)
                            note(s, candidate, why, by=by)
                    case _:
                        assert_never(unmeasured)
                s.commit()
            advanced = candidate.verdict is Outcome.KEEP or moved
            span.set(
                verdict=label(candidate),
                summary=candidate.summary,
                score=score(candidate, scope.evaluator_id, track_of(scope.cfg, candidate).primary),
            )
        return Judged(advanced=advanced, merged=merged)

    def _verdict(
        self, s: Session, scope: Scope, candidate: Candidate, bar_id: CandidateId
    ) -> tuple[list[OnTrack], Candidate | None]:
        """Return the verdict on each track against the run's bar, the candidate's own last, and the head to rebase onto.

        Each track judges it (``judge_tracks``), and the candidate has the best verdict. On the trunk, a candidate
        whose history contains a head that is not the run's bar is judged against the head instead, whatever it scores
        against the bar: the head may have moved below the bar, by a keep by hand. Else a rebase is needed for a keep
        against a bar that is no longer the head.
        """
        trunk = head(s, s.get_one(Campaign, candidate.campaign_id), excluding=candidate)
        match trunk:
            case Candidate() as moved if moved.id != bar_id and scope.repo.contains(candidate.revision, moved.revision):
                judged = judge_tracks(
                    candidate,
                    moved,
                    scope.evaluator_id,
                    scope.cfg,
                    scope.track,
                    self._units_dir,
                    self._evidence(s, scope, candidate, moved),
                )
                why = f'the head candidate#{moved.id} is in its history: '
                return [replace(j, reason=why + j.reason) for j in judged], None
            case _:
                pass
        bar = s.get_one(Candidate, bar_id)
        judged = judge_tracks(
            candidate,
            bar,
            scope.evaluator_id,
            scope.cfg,
            scope.track,
            self._units_dir,
            self._evidence(s, scope, candidate, bar),
        )
        *others, best = judged
        match best.verdict, trunk:
            case Outcome.KEEP, Candidate() as moved if moved.id != bar_id:
                reason = MOVED.format(head=moved.id) + best.reason
                return [*others, replace(best, verdict=Outcome.INTERESTING, reason=reason)], moved
            case _:
                return judged, None

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
        """After the run's commits are judged, end its ``scoring`` and its claim (``release``).

        ``last`` is the newest candidate of the run's versions that got a verdict: it decides whether the idea is done.
        It first waits until each submission of the run has its verdict (``judge_submission``), however the run ended:
        its idea is settled on them. A daemon that stops meanwhile leaves the run ``scoring``, for the next one.
        """

        def judged(s: Session) -> bool:
            return not any(pending(s.get_one(Candidate, c)) for c in submitted(s, run_id))

        if not self._db.wait_until(judged, give_up=self.launcher.cancelled.is_set):
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
        """Return the worker's lines of context: its worktree, its time, its base, and how it takes an idea.

        The board is a section of its own (``queue``).
        """
        author = reader.signature
        lead = wrap_up_lead(timeout, cfg.roles.doing_runs()[run.role].wrap_up_lead)
        campaign = run.campaign
        evaluator_id = campaign.evaluator_id
        at_base = ', '.join(f'{t.primary.name}={score(base, evaluator_id, t.primary)}' for t in cfg.all_tracks)
        lines = [
            '- Your cwd is your checkout: edit, run and save every version there. Only its versions are scored.'
            f' The campaign repo, {campaign.path}, is read-only for you.',
            f'- Your session ends {round(timeout / 60, 1):g} minutes after it starts, at `${DEADLINE_ENV}` (Unix time).'
            f' The daemon tells you to wrap up {round(lead / 60, 1):g} minutes before, between two tool calls: start'
            ' no command that can run past the deadline.',
            f'- Starting from candidate#{base.id}, the head: {at_base} — {base.summary}',
        ]
        if (bar := bar_of(s, base)) is not base:
            at_bar = ', '.join(f'{t.primary.name}={score(bar, evaluator_id, t.primary)}' for t in cfg.all_tracks)
            lines.append(
                f'- candidate#{base.id} is not your bar: your versions are judged against candidate#{bar.id}'
                f' ({at_bar}).'
            )
        lines += [
            f"- Your idea's directory in the shared workspace: `./{WORKSPACE}/idea<your idea>/` (in no version)."
            f" `piloop restore {campaign.name} <idea id>` writes back a done idea's files.",
            '- Leave a note on your idea when you are done, piped into the SQL tool: INSERT INTO comments'
            f" (target_kind, target_id, author, kind, body) VALUES ('idea', <your idea>, '{author}', 'note', '...')."
            ' Then end your run: `piloop run end submitted` when your work is done, or'
            " `piloop run end gave_up --reason '…'`. Nothing else ends it before your deadline.",
            '- Ask for what you cannot do or decide yourself with an escalation, never a note:'
            ' `piloop escalate idea#<your idea> --priority 1`, its form as JSON on stdin (`--guide` shows it).',
            '- The history below is what was tried in this campaign: do not repeat it; combine or go elsewhere.',
        ]
        return lines


def track_brief(idea: Idea) -> str | None:
    """What the track of a claimed ``idea`` asks of its run, for its worker: the keep rule, then the track's addendum.

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
        f'Your run is on track {track.name}: a keep must improve {track.primary.name} ({track.primary.direction} is'
        f' better) with confidence and hold {guards}.'
    )
    addendum = (Path(idea.campaign.path) / track.addendum).read_text().rstrip() if track.addendum else ''
    return '\n\n'.join(p for p in (rule, addendum) if p)


@dataclass(frozen=True)
class Scope:
    """The scope for scoring one candidate of a run: the run, the campaign's config and evaluator now, and the track.

    The track is the claimed idea's, else the first: a run that claimed nothing is judged as on one track.
    """

    run_id: RunId
    campaign_id: CampaignId
    cfg: CampaignConfig
    track: Track
    repo: Repository
    evaluator_id: EvaluatorId

    @classmethod
    def now(cls, s: Session, run: Run, repo: Repository) -> Scope:
        """The scope of ``run``'s next candidate, so a candidate scored after ``piloop init`` uses its evaluator."""
        campaign = run.campaign
        cfg = campaign.config
        evaluator = current_evaluator(s, campaign, cfg)
        return cls(
            run.id,
            campaign.id,
            cfg,
            fallback_track(cfg, run.idea.track if run.idea is not None else None),
            repo,
            evaluator.id,
        )


@dataclass(frozen=True)
class Judged:
    """The result of ``_judge``: whether the run's bar moves to the candidate, and the merge to judge next."""

    advanced: bool
    """It kept against the run's bar, on the trunk or for a rebase. The run's next commit is judged against it."""
    merged: CandidateId | None
    """The candidate of its merge onto the head, if it was rebased and the merge is a new tree."""


def _last_measurement(s: Session, candidate_id: CandidateId, evaluator_id: EvaluatorId) -> MeasurementId | None:
    """The newest measurement of the candidate under the evaluator, by id, or None."""
    found = select(func.max(Measurement.id)).where(
        Measurement.candidate_id == candidate_id, Measurement.evaluator_id == evaluator_id
    )
    return s.scalar(found)


def delta(mine: float | None, other: Candidate | None, evaluator_id: EvaluatorId, primary: Metric) -> float | None:
    """How much better ``mine`` is than ``other``'s ``primary``: positive when better. None when either lacks one."""
    theirs = None if other is None else score(other, evaluator_id, primary)
    return None if mine is None or theirs is None else primary.gain(mine, theirs)


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
    """The implementer ran into its role's timeout."""
    error: str | None = None
    """How the implementer's process failed, a timeout included."""
    unsaved: str | None = None
    """Why the save of what the implementer left unsaved failed (``WorkUnsaved``)."""
    scoring: Scoring = Scoring.SUBMIT
    """How its campaign scores its runs (``[evaluator] scoring``): only a run of ``submit`` ends with the verb."""
    committed: bool = False
    """It saved a version of its own, not only what it left unsaved (``Version.recovered``)."""


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
        # It ran into its role's timeout.
        case RunFacts(timed_out=True, error=error):
            return RunEnd.TIMED_OUT, error
        # Its process failed.
        case RunFacts(error=str() as error):
            return RunEnd.FAILED, error
        # What it left unsaved is only in the CAS.
        case RunFacts(unsaved=str() as error):
            return RunEnd.FAILED, f'{UNSAVED}: {error}'
        # A campaign that scores every version after the run, as 0.5 did: a clean exit after a commit ends the run, as it
        # did then.
        case RunFacts(scoring=Scoring.RUN_END, committed=True):
            return RunEnd.FINISHED, None
        # It stopped without ending its run.
        case _:
            return RunEnd.FAILED, NOT_ENDED


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
