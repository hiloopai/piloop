"""The core operations that an executor calls on what its jobs do (``Results``): they change the campaign's rows.

Each checks the domain only: a job's row closes once (``Settled``). Each holds the write lock from its check to the
commit, in a write transaction of its own, so two calls for one job never both pass the check. Fencing a result that comes late, or twice, is
the executor's (``piloop.plugins.executor.contract``). An agent's end is a ``JobEnded`` event on its row: what waits for
the agent reads it there (``Launcher.launch``), whichever process applied it.
"""

import fcntl
import os
import tempfile
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import timedelta
from enum import StrEnum
from pathlib import Path
from typing import Annotated, ClassVar, Literal, assert_never

from covalence import Cid
from pydantic import Field, SecretStr, TypeAdapter, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from piloop.actor import Actor, Agent, SignedAgent, System
from piloop.agents.roles import harness_of
from piloop.config import TRANSCRIPT
from piloop.db import terms
from piloop.db.events import emit, has, in_order, latest
from piloop.db.machine import Machine, Refused, transition
from piloop.db.models import (
    ENDED,
    LIVE,
    OPEN,
    AgentSession,
    AgentSessionState,
    Campaign,
    Comment,
    Event,
    EventModel,
    ReadBy,
    utcnow,
)
from piloop.db.session import Database, write_transaction
from piloop.ids import AgentSessionId, CommentId, CommentKind, EnvName, Ref, RoleName, TargetKind, TargetRef
from piloop.net.tokens import end_agent_token
from piloop.otel import Tracer
from piloop.plugins.campaign_type.kit.backlog.ids import IDEA
from piloop.plugins.campaign_type.kit.runs.ids import RunEnd
from piloop.plugins.campaign_type.kit.runs.tables import Run, run_of
from piloop.plugins.cas.contract import BlobAddress, Cas
from piloop.plugins.executor.contract import (
    AgentFinished,
    AgentJob,
    AgentKey,
    AgentNotStarted,
    AgentReport,
    Applying,
    Guard,
    HarnessSession,
    Job,
    JobKey,
    Key,
    MissingBlobs,
    Report,
    Settled,
    Workspace,
    job_kinds,
    kind_of,
)
from piloop.plugins.runtime.contract import AgentExit, ExitStatus
from piloop.plugins.secrets.contract import SECRETS, SecretsProvider, hidden, values
from piloop.plugins.vcs.contract import Transfer
from piloop.settings import TMP_DIR, AgentSettings
from piloop.store.tree import Manifest, Staged, merge, stage, write_files


def never_worked(startup_seconds: float) -> str:
    """The error of an agent that ended within ``startup_seconds`` (``[agents]``) and left nothing.

    It did not start to work: a crash, broken tools, no quota.
    """
    return f'it ended within {startup_seconds:g}s and left nothing'


class AgentSessionStarted(EventModel):
    """The launcher started an agent: its row is ``starting``."""

    kind: Literal['agent_session.started'] = 'agent_session.started'
    role: RoleName
    harness: str
    model: str | None
    reason: str | None
    """The row's ``reason``: why it was requested."""
    resumes: AgentSessionId | None = None
    """The earlier agent whose harness session this one continues (``continuity = "resume"``), or None."""


class AgentSessionRunning(EventModel):
    """Its executor started the agent: its row is ``running``, so it gets its comments and its wrap-up."""

    kind: Literal['agent_session.running'] = 'agent_session.running'
    machine: str | None
    """Where it runs: None in ``piloop serve``."""


class AgentSessionFinished(EventModel):
    """An agent's row closed: its process ended, or its job never started or was lost."""

    kind: Literal['agent_session.finished'] = 'agent_session.finished'
    role: RoleName
    reason: str | None
    """The row's ``reason``: why it was requested."""
    state: AgentSessionState
    """The row's ``state``: ``exited``, or ``killed`` when the daemon or a timeout ended it, or no process ran. How the
    process ended is in ``agent_session.job_ended``."""
    error: str | None


class StartupAnomaly(EventModel):
    """An agent did not start to work (``NotStarted``): an environment fault, not bad work."""

    kind: Literal['anomaly.startup'] = 'anomaly.startup'
    role: RoleName
    harness: str | None
    subject: Ref | None
    """What the agent was to work on, such as ``run#12``. None for an agent of a role that does no runs."""
    seconds: float
    error: str | None
    """Why it did not start. None only in an event of 0.1.0, which did not always know."""


class WrapUp(EventModel):
    """The daemon told an agent to save its work and stop, with the ``comment``: the note that carries the prompt.

    An implementer of a stopped campaign, or an agent cancelled with its wrap-up, is ended ``STOP_GRACE_SECONDS`` later.
    An implementer whose role timeout is near is ended by its timeout. A note that does not reach the agent has its
    ``comment.undelivered``; the grace period starts anyway.
    """

    READ_BY: ClassVar[ReadBy] = ReadBy.READERS
    kind: Literal['agent_session.wrap_up'] = 'agent_session.wrap_up'
    reason: str
    comment: CommentId | None = None
    """None when the agent no longer ran, or for a wrap-up from before the note carried the prompt."""


class AgentEnded(EventModel):
    """The daemon ended an agent, with the ``reason`` and the run's ``end``: a stop, a cancel, or its run's end.

    An implementer of a stopped campaign is ended when the grace period after its wrap-up prompt is over, or at once for
    a stop that ends it now. Most harnesses take a message only when the turn ends, so the implementer may never have
    read the prompt. The scheduler sets the row ``ending`` and commits it with the event before the kill, so
    a run end that the agent asks for after it is refused. The run ends ``campaign_stopped`` or ``cancelled`` unless
    its worker ended it before (``piloop run end``).
    """

    READ_BY: ClassVar[ReadBy] = ReadBy.READERS
    kind: Literal['agent_session.stop_ended'] = 'agent_session.stop_ended'
    reason: str
    end: RunEnd


AGENT_SESSION_STATES: Machine[AgentSessionState] = Machine(
    AgentSession.state,
    {
        AgentSessionState.STARTING: frozenset({AgentSessionState.REQUESTED}),
        AgentSessionState.REQUESTED: frozenset({AgentSessionState.STARTING}),
        AgentSessionState.RUNNING: frozenset({AgentSessionState.STARTING}),
        AgentSessionState.ENDING: frozenset({AgentSessionState.STARTING, AgentSessionState.RUNNING}),
        AgentSessionState.EXITED: frozenset(LIVE),
        AgentSessionState.NEVER_WORKED: frozenset(LIVE),
        AgentSessionState.KILLED: frozenset(OPEN),
    },
    TargetKind.AGENT_SESSION,
)
"""An agent's lifecycle. The pass takes a requested row for a free slot (``starting``), or puts it back when a stop is
due as it launches; its executor reports it ``running``; the daemon ends it (``ending``, ``end_agent``); and its row
closes once (``finish``): ``exited``, ``never_worked``, or ``killed``, which a requested row can be too. The take has no
event of its own: the launcher's ``agent_session.started`` follows it, or the put-back or the close emits its own."""


def end_agent(s: Session, w: AgentSession, reason: str, end: RunEnd, tracer: Tracer) -> None:
    """Set a starting or running agent ``ending``, with ``AgentEnded``, before its executor stops it. The caller commits.

    An agent that is ``ending`` already keeps the end that was first given: it is why the agent stops. The row is
    checked as the database has it, not as ``w`` was read: the agent's report can close it in between, and a closed
    row stays closed.
    """
    try:
        transition(
            s,
            AGENT_SESSION_STATES,
            w.id,
            AgentSessionState.ENDING,
            AgentEnded(reason=reason, end=end),
            System.DAEMON,
            error=reason,
        )
    except Refused:
        s.expire(w, ['state', 'error'])
        return
    tracer.event('agent_session.ended', agent_session=w.id, reason=reason)


def finish(
    s: Session,
    w: AgentSession,
    to: AgentSessionState,
    error: str | None,
    *,
    from_: frozenset[AgentSessionState] | None = None,
) -> None:
    """Close the agent's row as ``to``, with ``error``, and emit ``AgentSessionFinished``. The caller commits.

    Every close of an agent's row goes through here. Its token goes with it (``end_agent_token``). ``from_``: only from
    these states.

    Raises:
        Refused: the row is closed already, so its first end stands, or it is in no state of ``from_``.
    """
    assert to in ENDED, f'{to} does not close a row'
    finished = AgentSessionFinished(role=w.kind, reason=w.reason, state=to, error=error)
    transition(s, AGENT_SESSION_STATES, w.id, to, finished, System.DAEMON, from_=from_, ended_at=utcnow(), error=error)
    end_agent_token(s, w.id)


class WorkspaceConflict(EventModel):
    """What an agent wrote in its workspace that another agent wrote too, meanwhile: the first one's stays."""

    READ_BY: ClassVar[ReadBy] = ReadBy.READERS
    kind: Literal['agent_session.workspace_conflict'] = 'agent_session.workspace_conflict'
    beside: dict[str, str] = Field(default_factory=dict)
    """Workspace path → where this agent's file went instead: beside it, named after the agent."""
    blocked: dict[str, BlobAddress] = Field(default_factory=dict)
    """Workspace path → this agent's file in the CAS, where a directory or a symlink stands now: it is only there."""
    kept: list[str] = Field(default_factory=list)
    """The paths that this agent removed and another one changed meanwhile: they stay."""


class WorkUnsaved(EventModel):
    """What the agent left unsaved could not be saved as a version (``Unsaved``): only the CAS has it."""

    READ_BY: ClassVar[ReadBy] = ReadBy.READERS
    kind: Literal['agent_session.work_unsaved'] = 'agent_session.work_unsaved'
    blob: BlobAddress
    """The changes, in the vcs provider's own form: under ``git``, a patch onto the agent's last commit."""
    error: str
    """Why the save failed."""


class PromptOutcome(EventModel):
    """What came of a prompt to an agent, on the agent.

    ``comment.queued`` once the agent's runtime holds the prompt, then ``comment.delivered`` once the agent read it.
    ``comment.undelivered`` when it never did: the runtime refused it, or the agent ended before it read it.
    """

    READ_BY: ClassVar[ReadBy] = ReadBy.READERS
    kind: Literal['comment.queued', 'comment.delivered', 'comment.undelivered']
    comment: CommentId
    error: str | None = None
    """Why it did not reach the agent: only on ``comment.undelivered``."""


FINAL_OUTCOMES = ('comment.delivered', 'comment.undelivered')
"""The outcomes of a prompt after which nothing more comes of it."""
UNREAD = 'the agent ended before it read it'
"""The error of the ``comment.undelivered`` of a prompt that still waited in the agent's queue when it ended."""


def queue_prompt(s: Session, agent_session_id: AgentSessionId, text: str, author: str) -> CommentId | None:
    """Write ``text`` as a message to a running agent, which the daemon delivers (``Executor.prompt``). The caller commits.

    Return the message's id, or None when the agent is not running.
    """
    agent = s.get(AgentSession, agent_session_id)
    if agent is None or agent.state is not AgentSessionState.RUNNING:
        return None
    note = Comment(
        target_kind=TargetKind.AGENT_SESSION,
        target_id=agent_session_id,
        author=author,
        kind=CommentKind.MESSAGE,
        body=text,
    )
    s.add(note)
    s.flush()
    return note.id


class AgentSessionExtended(EventModel):
    """Someone gave a running agent more time: ``seconds`` past its timeout, on top of its earlier extensions."""

    kind: Literal['agent_session.extended'] = 'agent_session.extended'
    seconds: float


EXTENDED = (
    '[piloop] {actor} gave you {by} more time: your deadline is now {total:g} s after $PILOOP_DEADLINE, which still'
    ' holds the old one.'
)
"""The note that tells an extended agent its new deadline."""


def extension(s: Session, agent_session_id: AgentSessionId) -> float:
    """The seconds that the agent may run past its timeout: its extensions (``AgentSessionExtended``) in all."""
    on = Event.target == TargetRef(TargetKind.AGENT_SESSION, agent_session_id)
    return sum(e.seconds for _, e in in_order(s, AgentSessionExtended, on))


def extend(s: Session, agent: AgentSession, by: timedelta, actor: Actor) -> float:
    """Give a running agent ``by`` more time, and tell it its new deadline in a note. Return its extensions in all.

    The daemon hands the total to the agent's executor (``Executor.extend``). The caller commits.
    """
    emit(s, AgentSessionExtended(seconds=by.total_seconds()), TargetRef(TargetKind.AGENT_SESSION, agent.id), actor)
    total = extension(s, agent.id)
    queue_prompt(s, agent.id, EXTENDED.format(actor=actor.signature, by=by, total=total), actor.signature)
    return total


@dataclass(frozen=True)
class Ran:
    """The agent started to work, and its process ended."""

    process: AgentExit
    work: Transfer | None = None
    """Its versions in the CAS, when it was asked for them and made some."""
    kind: Literal['ran'] = 'ran'


@dataclass(frozen=True)
class NotStarted:
    """The agent did not start to work: a fault of the host or the harness, not an attempt.

    Its job did not start (``AgentNotStarted``), or it ended within ``startup_seconds`` (``[agents]``) and left nothing
    (``_never_worked``).
    """

    reason: str
    kind: Literal['not_started'] = 'not_started'


@dataclass(frozen=True)
class Lost:
    """Nothing came back from the agent (``Results.lost``): its executor lost it."""

    reason: str
    kind: Literal['lost'] = 'lost'


@dataclass(frozen=True)
class DaemonStopped:
    """The daemon stopped before the agent started or while it ran. The stop writes the rows."""

    work: Transfer | None = None
    """Its versions in the CAS, when it ran, was asked for them and made some."""
    kind: Literal['daemon_stopped'] = 'daemon_stopped'


@dataclass(frozen=True)
class HandedOver:
    """An in-place restart left the agent running for the next daemon process, which waits for it again."""

    kind: Literal['handed_over'] = 'handed_over'


type Ended = Annotated[Ran | NotStarted | Lost | DaemonStopped | HandedOver, Field(discriminator='kind')]
"""How an agent's job ended, for what waits for it. Only ``Ran`` has a process exit: only then did a process end."""


class JobEnded(EventModel):
    """How an agent's job ended, on its row: what waits for the agent reads it there (``Launcher.launch``)."""

    READ_BY: ClassVar[ReadBy] = ReadBy.READERS
    kind: Literal['agent_session.job_ended'] = 'agent_session.job_ended'
    ended: Ended


@dataclass(frozen=True)
class Proposer:
    """The agent that proposed an implementer's idea, and its harness session."""

    actor: SignedAgent
    harness: str | None
    """Its ``[plugins.harness.<name>]``. None when its row is gone."""
    executable: str | None
    """Its harness's ``executable``. None when its row is gone, or ``piloop.toml`` no longer names the harness."""
    session_id: str | None
    """Its harness's session id. None until piloop knows it, or when its row is gone."""


class Identified(EventModel):
    """An agent's harness session is new to its row (``learn``), with what the agent does in the campaign."""

    READ_BY: ClassVar[ReadBy] = ReadBy.READERS
    kind: Literal['agent_session.identified'] = 'agent_session.identified'
    session_id: str
    """The harness's own session id."""
    harness: str
    """The ``[plugins.harness.<name>]`` that runs the agent."""
    executable: str | None
    """The harness's ``executable``. None when ``piloop.toml`` no longer names the harness."""
    role: RoleName
    model: str | None
    campaign: str | None
    """The campaign's name."""
    subject: Ref | None
    """What the agent works on, such as ``run#12``. None for an agent of a role that does no runs."""
    groups: tuple[Ref, ...]
    """What its work belongs to, such as the run's ``idea#4``."""
    parent: Proposer | None
    """The agent whose work this one carries out: the agent that proposed the run's idea (``ideas.created_by``). None
    when a person or the orchestrator did, or without a run."""


class Unresumed(StrEnum):
    """Why an agent of a role that resumes its sessions (``continuity = "resume"``) started a new one."""

    NO_SESSION = 'no_session'
    """No earlier agent of the role left a session in the CAS on the harness that this one runs."""
    HARNESS = 'harness'
    """The harness cannot resume a session from the CAS: it has no ``resume`` argv, or its provider restores no session
    file (``Harness.restore``)."""
    FAILED = 'failed'
    """The agent before resumed the same session and did not end well: its session may be what broke it."""
    UNRESTORED = 'unrestored'
    """The worker could not write the session file back: a missing or broken blob, a full disk."""


class SessionNotResumed(EventModel):
    """An agent of a role that resumes its sessions started a new session from its whole brief, and why."""

    READ_BY: ClassVar[ReadBy] = ReadBy.READERS
    kind: Literal['agent_session.not_resumed'] = 'agent_session.not_resumed'
    reason: Unresumed
    detail: str
    """What the reason was about: the session, the harness, or the worker's error."""


class SessionSaved(EventModel):
    """A snapshot of an agent's harness session file is in the CAS (``saved``): while the agent runs, and when it ends.

    A snapshot holds the whole lines of the file. A harness appends to its file, so each snapshot starts with the bytes
    of the one before.
    """

    READ_BY: ClassVar[ReadBy] = ReadBy.READERS
    kind: Literal['agent_session.session_saved'] = 'agent_session.session_saved'
    session_file: BlobAddress
    """The snapshot: the row's ``session_file`` now."""
    session_id: str
    """The harness's own session id, whose file it is."""


SIGNED: TypeAdapter[SignedAgent] = TypeAdapter(SignedAgent)
"""Reads an agent from its signature, such as an ``ideas.created_by``."""


OUTPUT_SEPARATOR = ' | '
"""What joins the last lines of an agent's output in its error, which stays one line."""


def explained(process: AgentExit, output: str, found: Mapping[EnvName, SecretStr], chars: int) -> AgentExit:
    """``process`` with the last ``chars`` characters of the lines of its ``output`` after its error.

    Each value of the agent's secrets (``found``) reads as its name. Its token needs no hiding: it ends with the agent.
    """
    lines = OUTPUT_SEPARATOR.join(stripped for line in output.splitlines() if (stripped := line.strip()))
    said = hidden(lines, found)
    if not said or chars == 0:
        return process
    tail = said if len(said) <= chars else f'…{said[-chars:]}'
    return replace(process, error=tail if process.error is None else f'{process.error}: {tail}')


MERGE_LOCK = 'merge.lock'
"""The file under the home whose lock a workspace merge holds (``merging``)."""


@contextmanager
def merging(home: Path) -> Generator[None]:
    """Hold the home's merge lock: each path's compare-and-swap must not interleave with another merge's.

    Every process that applies reports merges, the daemon's and each one that serves its API, so the lock is the file's
    (``flock``), not one of a process: the kernel releases it when its holder dies. Each holder opens the file itself,
    so two threads of one process exclude each other too.
    """
    fd = os.open(home / MERGE_LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


class CampaignResults:
    """``Results``: what the end of an agent's job, or of another kind's (``JobKind.apply``), changes in the rows.

    A job's files come back in ``cas``, and go into its place under ``home``, where the readers of logs look.
    """

    def __init__(
        self,
        db: Database,
        tracer: Tracer,
        home: Path,
        cas: Cas,
        *,
        secrets: SecretsProvider | None = None,
        exit_output_chars: int = AgentSettings().exit_output_chars,
        startup_seconds: float = AgentSettings().startup_seconds,
        startup_turns: int = AgentSettings().startup_turns,
    ) -> None:
        self._db = db
        self._tracer = tracer
        self._home = home
        self._cas = cas
        self._secrets = secrets if secrets is not None else SECRETS.selected({})
        self._exit_output_chars = exit_output_chars
        """``[agents] exit_output_chars``: how much of its last output a failed or early end says."""
        self._startup_seconds = startup_seconds
        """``[agents] startup_seconds``: an agent that ends sooner and left nothing never worked."""
        self._startup_turns = startup_turns
        """``[agents] startup_turns``: an agent that took more turns worked, however soon it ended."""

    def _apply(self, guard: Guard | None, change: Callable[[Session], None]) -> None:
        """Run ``guard``, then ``change``, in one write transaction, and commit. Nothing changes when one raises."""
        with self._db.session() as s:
            write_transaction(s)
            if guard is not None:
                guard(s)
            change(s)
            s.commit()

    def started(
        self,
        key: AgentKey,
        handle: dict[str, object],
        session_id: str | None,
        *,
        machine: str | None = None,
        guard: Guard | None = None,
    ) -> None:
        """The agent runs: its row is ``running``, with its handle in its runtime on ``machine``: ``piloop attach``."""

        def change(s: Session) -> None:
            w = _open(s, key)
            w.handle, w.machine = handle, machine
            if w.state is AgentSessionState.STARTING:  # a row that the daemon ended meanwhile stays ending
                running = AgentSessionRunning(machine=machine)
                transition(s, AGENT_SESSION_STATES, w.id, AgentSessionState.RUNNING, running, System.DAEMON)
            learn(s, w, session_id)

        self._apply(guard, change)

    def session(self, key: AgentKey, session: HarnessSession, *, guard: Guard | None = None) -> None:
        def change(s: Session) -> None:
            w = _open(s, key)
            learn(s, w, session.id)
            saved(s, w, session.file)

        self._apply(guard, change)

    def finished(self, job: Job, report: Report, *, guard: Guard | None = None) -> None:
        """Apply the report once the CAS holds every blob that it names, else raise ``MissingBlobs``.

        Its files go into the job's place in the transaction that applies it: what reads the row closed finds them. An
        agent's workspace merges before that transaction, as the write lock would wait on every file of it
        (``_workspace``).
        """
        if missing := self._cas.missing(report.blobs()):
            raise MissingBlobs(f'its report names blobs that the CAS lacks: {", ".join(map(str, missing))}')
        workspace = job.artifact.workspace if isinstance(job, AgentJob) else None
        changed = report.workspace if isinstance(report, AgentFinished) and workspace is not None else {}
        # read from the CAS before the write lock: a workspace file can be large, and a remote CAS slow
        base = Manifest.get(self._cas, workspace.manifest) if workspace is not None and changed else None
        output = self._last_output(report)
        with tempfile.TemporaryDirectory(dir=self._scratch()) as scratch:
            staged = stage(self._cas, changed, Path(scratch))
            conflict = (
                self._workspace(job, workspace, base, staged, changed)
                if isinstance(job, AgentJob) and workspace is not None and base is not None
                else None
            )

            def change(s: Session) -> None:
                if isinstance(job, AgentJob):
                    assert isinstance(report, AgentFinished | AgentNotStarted)  # a report is of its job's kind
                    self._agent(s, job, report, output)
                    if conflict is not None:
                        emit(s, conflict, TargetRef(TargetKind.AGENT_SESSION, job.agent_session), System.DAEMON)
                else:
                    kind_of(job).apply(s, job, report, Applying(self._home, terms.stopping(s)))
                write_files(self._cas, report.files, self._home / job.place)

            self._apply(guard, change)
        if isinstance(job, AgentJob) and isinstance(report, AgentFinished):
            for comment in report.read:
                self._tracer.event('comment.delivered', comment=comment, agent_session=job.agent_session)
            for comment in report.unread:
                self._tracer.event(
                    'comment.undelivered', comment=comment, agent_session=job.agent_session, error=UNREAD
                )

    def lost(self, key: Key, reason: str, *, guard: Guard | None = None) -> None:
        def change(s: Session) -> None:
            if isinstance(key, JobKey):
                job_kinds()[key.kind].lost(s, key.id)
                return
            w = _open(s, key)
            finish(s, w, AgentSessionState.KILLED, reason)
            _ended(s, w.id, Lost(reason))

        self._apply(guard, change)

    def queued(self, key: AgentKey, prompt: CommentId, error: str | None) -> None:
        """Record ``comment.queued``, or ``comment.undelivered`` with ``error``, unless the comment has an outcome."""
        outcome = PromptOutcome(
            kind='comment.queued' if error is None else 'comment.undelivered', comment=prompt, error=error
        )
        self._apply(None, lambda s: _record(s, key.agent_session, outcome))
        self._tracer.event(outcome.kind, comment=prompt, agent_session=key.agent_session, error=error)

    def _last_output(self, report: Report) -> str | None:
        """The transcript of an agent that exited non-zero or within ``startup_seconds`` (``[agents]``), from the CAS, or None.

        Read it before the write lock. Whether an early end did any work is known only in the transaction.
        """
        match report:
            case AgentFinished(process=AgentExit(exit_code=int(code), seconds=seconds), files=files) if (
                code != 0 or seconds < self._startup_seconds
            ) and TRANSCRIPT in files:
                data = self._cas.get(files[TRANSCRIPT])
                assert data is not None  # finished checked that the CAS holds every file of the report
                return data.decode(errors='replace')
            case _:
                return None

    def _agent(self, s: Session, job: AgentJob, report: AgentReport, output: str | None = None) -> None:
        """Close the row from the report, and emit ``AgentSessionFinished``, ``JobEnded`` and ``StartupAnomaly``.

        An agent that did not start to work closes ``never_worked`` (``NotStarted``): its job did not start, or it ended
        within ``startup_seconds`` (``[agents]``) and left nothing (``_never_worked``), unless the daemon ended it. A
        wrap-up does not make it work: one told to wrap up as it starts, that leaves nothing, never worked either. The
        anomaly, which the startup breaker counts as a fault of the host or the harness, comes only when nothing told
        the agent to stop (``WrapUp``): a cancel or a stop that it obeyed is no such fault. An agent that a daemon stop
        ended (``stopped_by_daemon``) closes as killed by the stop
        (``RunEnd.DAEMON_STOPPED``), with the usage that was read, whichever daemon process reads its report.
        The ``output`` of an agent that exited non-zero ends its error (``explained``), and the reason of one that exited 0
        and did not start to work.
        """
        w = _open(s, AgentKey(job.agent_session))
        stopped = stopped_by_daemon(s, w.id)
        ending = w.state is AgentSessionState.ENDING  # its error says why the daemon ended it
        match report:
            case AgentNotStarted(reason=reason, seconds=seconds):
                if stopped:
                    self._stopped(s, w, None)
                    return
                ended: Ran | NotStarted = NotStarted(reason)
            case AgentFinished(process=process):
                found = {} if output is None else values(s, self._secrets, job.agent_session, job.secrets.values())
                if output is not None and process.exit_code != 0:
                    process = explained(process, output, found, self._exit_output_chars)
                if report.unrestored is not None:
                    unrestored = SessionNotResumed(reason=Unresumed.UNRESTORED, detail=report.unrestored)
                    emit(s, unrestored, TargetRef(TargetKind.AGENT_SESSION, w.id), System.DAEMON)
                learn(s, w, report.session_id)
                saved(s, w, report.session_file)
                for comment in report.read:
                    _record(s, w.id, PromptOutcome(kind='comment.delivered', comment=comment))
                for comment in report.unread:
                    _record(s, w.id, PromptOutcome(kind='comment.undelivered', comment=comment, error=UNREAD))
                w.usage = report.usage
                if (unsaved := report.unsaved) is not None:
                    event = WorkUnsaved(blob=unsaved.blob, error=unsaved.error)
                    emit(s, event, TargetRef(TargetKind.AGENT_SESSION, w.id), System.DAEMON)
                    self._tracer.event(event.kind, agent_session=w.id, blob=str(event.blob), error=event.error)
                if stopped:
                    self._stopped(s, w, report.work)
                    return
                seconds = process.seconds
                worked = _ended_by_daemon(s, w.id) or not _never_worked(
                    s, w, report, self._startup_seconds, self._startup_turns
                )
                match worked, process.error:
                    case True, _:
                        ended = Ran(process, report.work)
                    case False, str() as error:
                        ended = NotStarted(error)
                    case _:  # it exited 0, so what it said last is all that says why
                        said = explained(
                            replace(process, error=never_worked(self._startup_seconds)),
                            output or '',
                            found,
                            self._exit_output_chars,
                        )
                        ended = NotStarted(said.error or never_worked(self._startup_seconds))
            case _:
                assert_never(report)
        _close(s, w, ended, ending=ending)
        if isinstance(ended, NotStarted) and not ending and not _told_to_wrap_up(s, w.id):
            anomaly = StartupAnomaly(
                role=w.kind,
                harness=w.harness,
                subject=w.subject,
                seconds=round(seconds, 1),
                error=ended.reason,
            )
            emit(s, anomaly, TargetRef(TargetKind.AGENT_SESSION, w.id), System.DAEMON)
            self._tracer.event(anomaly.kind, agent_session=w.id, harness=w.harness, error=ended.reason)
        _ended(s, w.id, ended)

    def _workspace(
        self,
        job: AgentJob,
        workspace: Workspace,
        base: Manifest,
        staged: dict[str, Staged | None],
        changed: dict[str, Cid | None],
    ) -> WorkspaceConflict | None:
        """Merge what the agent wrote or removed in its workspace into the campaign's, path by path (``merge``).

        A path that another agent wrote meanwhile keeps that agent's file: this one's goes beside it, named after the
        agent, and this one's removal is not done. The ``WorkspaceConflict`` returned says which.

        One merge at a time on the home, in any of its processes (``merging``), outside the write lock. A merge is
        idempotent, so a report sent again after a crash merges to the same files; one that the fence then refuses has
        merged all the same, which keeps what its agent made.
        """
        with self._db.session() as s:
            w = s.get_one(AgentSession, job.agent_session)
            suffix = f'{w.kind}-{w.id}'
        with merging(self._home):
            merged = merge(self._home / workspace.place, base, staged, suffix)
        if not (merged.beside or merged.blocked or merged.kept):
            return None
        blocked = {rel: blob for rel in merged.blocked if (blob := changed[rel]) is not None}
        conflict = WorkspaceConflict(beside=merged.beside, blocked=blocked, kept=merged.kept)
        self._tracer.event(
            conflict.kind,
            agent_session=job.agent_session,
            beside=len(merged.beside),
            blocked=len(merged.blocked),
            kept=len(merged.kept),
        )
        return conflict

    def _scratch(self) -> Path:
        """``<home>/tmp``, where the blobs of a merge wait: on the disk of the workspace, so that a move is a rename."""
        scratch = self._home / TMP_DIR
        scratch.mkdir(parents=True, exist_ok=True)
        return scratch

    @staticmethod
    def _stopped(s: Session, w: AgentSession, work: Transfer | None) -> None:
        finish(s, w, AgentSessionState.KILLED, RunEnd.DAEMON_STOPPED)
        _ended(s, w.id, DaemonStopped(work))


def _close(s: Session, w: AgentSession, ended: Ran | NotStarted, *, ending: bool) -> None:
    """Close the row of an agent whose job ended as ``ended``. ``ending``: the daemon ended it, and its error says why.

    One that the daemon ended before it started is ``killed``, not ``never_worked``: no fault of the host or the harness.
    """
    match ended:
        case NotStarted() if ending:
            finish(s, w, AgentSessionState.KILLED, w.error)
        case NotStarted(reason=reason):
            finish(s, w, AgentSessionState.NEVER_WORKED, reason)
        case Ran(process=process):
            timed_out = process.status is ExitStatus.TIMED_OUT
            to = AgentSessionState.KILLED if ending or timed_out else AgentSessionState.EXITED
            finish(s, w, to, w.error if ending else process.error)
        case _:
            assert_never(ended)


def _open(s: Session, key: AgentKey) -> AgentSession:
    """Return the agent's row while it is starting, running or ending. Raises ``Settled`` once it is closed."""
    w = s.get(AgentSession, key.agent_session)
    if w is None or w.state not in LIVE:
        raise Settled(f'agent_session#{key.agent_session} is closed')
    return w


def learn(s: Session, w: AgentSession, session_id: str | None) -> None:
    """Put ``session_id`` on the row. When the row did not have it, emit ``Identified``. The caller commits.

    Each write of an agent's session id goes through here: the launcher's minted id, and the ids that its executor
    reports at the start, in each heartbeat and in the final report.
    """
    if session_id is None or session_id == w.session_id:
        return
    w.session_id = session_id
    assert w.harness is not None  # the launcher picks the harness before it launches the agent
    run = None if (run_id := run_of(w)) is None else s.get(Run, run_id)
    idea = None if run is None else run.idea
    definition = harness_of(s, w)
    identified = Identified(
        session_id=session_id,
        harness=w.harness,
        executable=None if definition is None else definition.executable,
        role=w.kind,
        model=w.model,
        campaign=None if w.campaign_id is None else s.get_one(Campaign, w.campaign_id).name,
        subject=w.subject,
        groups=() if idea is None else (TargetRef(IDEA, idea.id),),
        parent=None if idea is None else _proposer(s, idea.created_by),
    )
    emit(s, identified, TargetRef(TargetKind.AGENT_SESSION, w.id), System.DAEMON)


def saved(s: Session, w: AgentSession, session_file: Cid | None) -> None:
    """Put ``session_file`` on the row. When the row did not have it, emit ``SessionSaved``. The caller commits.

    Each write of an agent's session file goes through here: the snapshots in its heartbeats, and the last one in its
    report.
    """
    if session_file is None or session_file == w.session_file:
        return
    assert w.session_id is not None  # the worker finds the file by the session id, which ``learn`` put on the row
    w.session_file = session_file
    event = SessionSaved(session_file=session_file, session_id=w.session_id)
    emit(s, event, TargetRef(TargetKind.AGENT_SESSION, w.id), System.DAEMON)


def _proposer(s: Session, created_by: str) -> Proposer | None:
    """The agent whose signature ``created_by`` is, with its harness session. None for any other author."""
    try:
        agent = SIGNED.validate_python(created_by)
    except ValidationError:  # created_by is text: a person, the orchestrator, or a name that no agent signs
        return None
    row = s.get(AgentSession, agent.session)
    if row is None:
        return Proposer(agent, None, None, None)
    definition = harness_of(s, row)
    return Proposer(agent, row.harness, None if definition is None else definition.executable, row.session_id)


def _record(s: Session, agent_session_id: AgentSessionId, outcome: PromptOutcome) -> None:
    """Emit ``outcome`` on the agent, unless the comment has one: any before a queue, a ``FINAL_OUTCOMES`` one else."""
    on_agent = TargetRef(TargetKind.AGENT_SESSION, agent_session_id)
    before = PromptOutcome.kinds() if outcome.kind == 'comment.queued' else FINAL_OUTCOMES
    found = select(Event.id).where(Event.target == on_agent, has(PromptOutcome, comment=outcome.comment))
    if s.scalar(found.where(Event.kind.in_(before)).limit(1)) is None:
        emit(s, outcome, on_agent, System.DAEMON)


def _ended(s: Session, agent_session_id: AgentSessionId, ended: Ended) -> None:
    emit(
        s,
        JobEnded(ended=ended),
        TargetRef(TargetKind.AGENT_SESSION, agent_session_id),
        System.DAEMON,
    )


def ended_of(s: Session, agent_session_id: AgentSessionId) -> Ended | None:
    """Return how the agent's job ended (``JobEnded``), or None when no result closed its row: a stop did."""
    found = latest(s, JobEnded, Event.target == TargetRef(TargetKind.AGENT_SESSION, agent_session_id))
    return None if found is None else found[1].ended


def stopped_by_daemon(s: Session, agent_session_id: AgentSessionId) -> bool:
    """Whether a daemon stop ended the agent (``AgentEnded`` with ``RunEnd.DAEMON_STOPPED``, ``Launcher.stop``)."""
    found = latest(s, AgentEnded, Event.target == TargetRef(TargetKind.AGENT_SESSION, agent_session_id))
    return found is not None and found[1].end is RunEnd.DAEMON_STOPPED


def _told_to_wrap_up(s: Session, agent_session_id: AgentSessionId) -> bool:
    """Whether the daemon told the agent to wrap up (``WrapUp``): for a cancel, a stop or its time."""
    on_agent_session = Event.target == TargetRef(TargetKind.AGENT_SESSION, agent_session_id)
    return s.scalar(select(Event.id).where(on_agent_session, has(WrapUp)).limit(1)) is not None


def _ended_by_daemon(s: Session, agent_session_id: AgentSessionId) -> bool:
    """Whether the daemon ended the agent (``AgentEnded``): its end is the daemon's then, not a failed start."""
    on_agent_session = Event.target == TargetRef(TargetKind.AGENT_SESSION, agent_session_id)
    ended = select(Event.id).where(on_agent_session, has(AgentEnded))
    return s.scalar(ended.limit(1)) is not None


def _never_worked(
    s: Session, w: AgentSession, report: AgentFinished, startup_seconds: float, startup_turns: int
) -> bool:
    """Return whether a started agent ended within ``startup_seconds`` (``[agents]``) and left nothing behind.

    An agent whose harness counted more than ``startup_turns`` model turns worked, whatever it left: it tried, and
    gave up or ran out of ideas. That is an attempt, not a fault of the host or the harness.

    Nothing behind means no change to its checkout (``AgentFinished.changed``: no commit, no uncommitted change outside
    the links), no comment signed by it, and no event on its subject signed by it, such as its run's end
    (``piloop run end``).

    Otherwise a run's work is a change to its worktree, a comment or a write that it signs, so that decides for an
    agent with a run, whatever its tokens: an agent whose tools fail at launch still reads its brief and says that it
    is blocked, which costs tokens but few turns.
    A sweep's work can be rows that no comment signs, so a sweep that spent tokens worked, and only a sweep whose
    harness's usage is read is judged. The tokens can be unknown for a sweep that worked too, when its session id
    was not found.
    """
    if report.process.status is ExitStatus.TIMED_OUT or report.process.seconds >= startup_seconds or report.changed:
        return False
    if report.usage is not None and report.usage.turns > startup_turns:
        return False
    if run_of(w) is None:
        definition = harness_of(s, w)
        if definition is None or definition.usage() is None or (report.usage is not None and report.usage.total > 0):
            return False
    signature = Agent(role=w.kind, session=w.id).signature
    commented = select(Comment.id).where(Comment.author == signature).limit(1)
    if s.scalar(commented) is not None:
        return False
    if (subject := w.subject) is None:
        return True
    return s.scalar(select(Event.id).where(Event.target == subject, Event.actor == signature).limit(1)) is None
