"""The executor capability: how jobs reach the machines that run them, and how their results come back.

A **job** is one agent session, or one job of a kind that a package declares (``JobKind``), such as an evaluation,
described as data (``Job``): what to check out, what to run, what to bring back. Core decides what runs (the planner,
the roles, each kind's owner); the executor decides where and how, and keeps track of it. When a job starts, ends or is
lost, the executor calls the core operations (``Results``), which change the campaign's rows. Like Airflow's executors
(``LocalExecutor``, ``CeleryExecutor``): core queues work and reads results, and the executor owns the workers.

Host tier: an executor runs processes. ``[plugins.executor] provider`` in ``config.toml`` selects one (``workers`` by
default). The entry point names an ``ExecutorConfig`` class, whose fields are the other keys of the table, and the
daemon calls its ``open`` once to make the ``Executor``.

Core enforces the domain's invariants only: a job's row closes once, a measurement is recorded once. An executor that
can deliver a result twice, or late (a worker presumed dead), protects the work itself: ``workers`` does it with leases
and fencing tokens, checked inside the session in which it calls ``Results``. What counts as lost work, and how to
protect it, is the executor's choice.
"""

import json
import subprocess
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Annotated, ClassVar, Final, Literal, NewType, Protocol, Self

from covalence import Cid
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    GetCoreSchemaHandler,
    GetJsonSchemaHandler,
    InstanceOf,
    TypeAdapter,
    model_validator,
)
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import CoreSchema, core_schema
from sqlalchemy.orm import Session

from piloop import deprecated
from piloop.actor import Agent
from piloop.api.contract import Revealed
from piloop.db.models import AgentSession, EventModel
from piloop.db.session import Database
from piloop.ids import AgentSessionId, CampaignId, CommentId, EnvName, Ref, Revision, TargetRef
from piloop.net.tokens import mint_agent
from piloop.otel import Span, Tracer
from piloop.plugins.cas.contract import BlobAddress, Cas
from piloop.plugins.contribution import Contributes
from piloop.plugins.harness.contract import HARNESSES, Harness, Usage
from piloop.plugins.host import Capability, One, Parameters, Scope, Slot
from piloop.plugins.mcp_server.bridges import MCP_PROVIDERS
from piloop.plugins.mcp_server.contract import MCP_SERVERS, McpServer
from piloop.plugins.network.contract import Network
from piloop.plugins.runtime.contract import AgentExit, AgentStart, Handle, RuntimeFactory
from piloop.plugins.secrets.contract import SecretsProvider, values
from piloop.plugins.vcs.contract import VCS, Repository, Transfer, Unsaved, Vcs
from piloop.store.tree import Materialised


def _relative(place: str) -> str:
    if not place or PurePosixPath(place).is_absolute() or '..' in PurePosixPath(place).parts:
        raise ValueError(f'{place!r} is not a relative path inside a home')
    return place


type Place = Annotated[str, AfterValidator(_relative)]
"""Where a job's files go, relative to a home (``runs/12``): the worker makes it under its own home, and core puts
what comes back under its own. A runner's ``_work/<name>``: the job names the directory, the machine roots it."""

RepoId = NewType('RepoId', CampaignId)
"""A repo of record: the campaign whose checkout it is. Two campaigns on clones of one project have two, and so do
two tenants' forks: a vcs's own identity (git's root commit) is the same for all of them."""


class Workspace(BaseModel):
    """A campaign's shared workspace, as a job gets it: read-only copies of its files when the job was made.

    What the job writes, changes or adds there comes back in its report (``AgentFinished.workspace``), and core merges
    it into the workspace at ``place`` under its own home, path by path (``piloop.store.tree.merge``).
    """

    model_config = ConfigDict(frozen=True)

    place: Place
    manifest: BlobAddress
    """Its tree in the CAS when the job was made."""


class Source(BaseModel):
    """An artifact: the files of ``revision`` in ``repo``, checked out at the job's place, with ``links`` in it."""

    model_config = ConfigDict(frozen=True)

    vcs: Annotated[Vcs, VCS.entry]
    """The campaign's vcs provider, with its parameters (``[plugins.vcs]``): the worker checks out with it."""
    repo: RepoId
    revision: Revision
    links: dict[str, BlobAddress] = Field(default_factory=dict)
    """Inputs that no version holds (data, environments), by name in the checkout: each the manifest of its tree in the
    CAS, put there read-only (``piloop.store.tree.Copies``)."""
    workspace: Workspace | None = None
    """The campaign's shared workspace, put into the checkout as ``workspace/``. None: an evaluation's, which has none."""
    history: list[Revision] = Field(default_factory=list)
    """Other versions of ``repo`` that the checkout's repository holds: the campaign's candidates, which an agent reads
    and composes."""


class MissingBlobs(Exception):
    """A report names blobs that the CAS does not hold: sending it again never applies it, so its worker loses the job.

    A worker puts every blob before it reports, so a blob missing then is in another CAS, or lost.
    """


DEADLINE_ENV = 'PILOOP_DEADLINE'
"""The variable in an agent's environment that holds the Unix time at which its ``AgentJob.timeout`` ends it. Every
executor sets it when it starts the agent."""


class JobSecrets(BaseModel):
    """What an agent's job gets at its start, on its live lease: its secrets' values, and the token that it acts with.

    The token is minted then (``piloop.net.tokens.mint_agent``) and is never in the job, which the CAS keeps.
    """

    model_config = ConfigDict(frozen=True)

    values: dict[EnvName, Revealed]
    """The values of the job's secrets (``AgentJob.secrets``) that the home has, by name."""
    token: Revealed
    """The agent's own token: its ``PILOOP_AGENT_TOKEN``."""


class Resume(BaseModel):
    """A harness session to continue on any machine: its file in the CAS, and the harness's ``resume`` argv."""

    model_config = ConfigDict(frozen=True)

    session_id: str
    file: BlobAddress
    """The session file, as ``agent_sessions.session_file`` names it: the worker writes it back where the job's
    harness says (``Harness.restore``)."""
    brief: BlobAddress
    """The brief of the resumed agent, in place of ``AgentJob.brief_cid``: only what is new to the session."""
    args: list[str]
    """The arguments that continue the session with the agent's first prompt, in place of ``AgentStart.args``."""


class Job(BaseModel):
    """The description of a job: everything a worker needs, so it reads no campaign table. A kind adds its own fields.

    ``kind`` names the job's kind: ``agent``, core's own, or a ``JobKind`` that a package declares.
    """

    model_config = ConfigDict(frozen=True)

    kind: str
    artifact: Source
    place: Place
    """Its log directory under the home of the machine that runs it, with its worktree in it."""


class AgentJob(Job):
    """The description of an agent's job: everything a worker needs, so it reads no campaign table.

    It names no path of the machine that made it. The worker makes the job's ``place`` under its own home: the log
    directory, with the worktree in it. Secrets go by name (``secrets``): the worker asks for their values when it
    starts the job (``Control.secrets``), and renders the MCP ``servers`` into the argv (``{mcp}``).
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal['agent'] = 'agent'
    agent_session: AgentSessionId
    runtime: str
    """The runtime that starts the agent on the worker's host (``runtime`` in ``piloop.toml``)."""
    timeout: float
    """The seconds the agent may run, counted from its start, also across a restart of its worker. Its extensions add
    to it (``Executor.extend``)."""
    brief_cid: BlobAddress
    """The agent's brief, a text blob in the CAS, which the worker writes to its log directory before the start: its
    first prompt names the file."""
    spec: AgentStart
    """What the runtime starts, once the worker fills in its places."""
    harness: Annotated[Harness, HARNESSES.entry] | None = None
    """The agent's harness, which renders its MCP servers (``{mcp}``) and reads its session file on the worker. None
    for a harness of no provider (``Custom``), which does neither."""
    servers: dict[str, Annotated[McpServer, MCP_SERVERS.entry]] = Field(default_factory=dict)
    """The MCP servers of the agent's role, which name their variables, never their values."""
    secrets: dict[EnvName, EnvName] = Field(default_factory=dict)
    """The variables of the agent's environment that take a secret, with the secret's name: the harness's references
    (``Harness.secrets``), and each variable of the ``servers``, the secret of its own name."""
    session_id: str | None = None
    """The session id the launcher made for the argv (``{session_id}``), or None when the runtime reports it."""
    capture: Ref | None = None
    """Bring the agent's work back, with what it left unsaved as a version recovered for this subject (``run#12``,
    ``Repository.capture``). None for a sweep: nothing that it saves comes back."""
    resume: Resume | None = None
    """Continue this session instead of starting a new one. When the worker cannot write its file back, it starts the
    agent with ``spec`` and says why (``AgentFinished.unrestored``)."""

    @property
    def needs_session_id(self) -> bool:
        """Whether the session id is needed when the agent ends, for the usage: the worker asks the runtime for it."""
        return self.harness is not None and self.harness.needs_session_id

    @model_validator(mode='before')
    @classmethod
    def _harness(cls, data: object) -> object:
        """A job that a 0.3 daemon queued names its MCP renderer and its usage tally, each the name of a harness."""
        if not isinstance(data, dict) or 'harness' in data or not ({'mcp', 'usage'} & data.keys()):
            return data
        deprecated.until('0.5.0', 'a job of a 0.3 daemon names mcp and usage: delete AgentJob._harness')
        usage = data.get('usage')
        named = (usage.get('tally') if isinstance(usage, dict) else None) or data.get('mcp')
        rest = {k: v for k, v in data.items() if k not in ('mcp', 'usage')}
        return {**rest, 'harness': {'provider': named}} if named is not None else rest

    @model_validator(mode='before')
    @classmethod
    def _capture(cls, data: object) -> object:
        """A job that a daemon of 0.4 or older queued captures for a run by its id: its agent may still run."""
        if not isinstance(data, dict) or not isinstance(run := data.get('capture'), int):
            return data
        deprecated.until('0.5.0', 'a job of 0.4 captures for a run id: delete AgentJob._capture')
        return {**data, 'capture': f'run#{run}'}

    @model_validator(mode='before')
    @classmethod
    def _servers(cls, data: object) -> object:
        """A job that a daemon of protocol 13 queued carries its servers without their provider."""
        if not isinstance(data, dict) or not isinstance(servers := data.get('servers'), dict):
            return data
        if all(isinstance(t, dict) and 'provider' in t for t in servers.values()):
            return data
        deprecated.until(
            '0.5.0',
            'the servers of a job of protocol 13 or older (0.3.1 is 7) have no provider: delete AgentJob._servers',
        )
        tables = {name: dict(t) if isinstance(t, dict) else t for name, t in servers.items()}
        for step in MCP_PROVIDERS:
            step.apply({'plugins': {'mcp_server': tables}})
        return {**data, 'servers': tables}


def job_secrets(s: Session, secrets: SecretsProvider, job: AgentJob) -> JobSecrets:
    """What ``job`` gets at its start: the values of its secrets that the home has, and a token minted for its agent.

    The token replaces any that the agent had. The caller commits.
    """
    found = values(s, secrets, job.agent_session, job.secrets.values())
    agent = Agent(role=s.get_one(AgentSession, job.agent_session).kind, session=job.agent_session)
    return JobSecrets(values=found, token=mint_agent(s, agent))


AGENT: Final = 'agent'
"""The kind of an agent's job (``AgentJob``): core's own, which no ``JobKind`` declares."""


class Report(BaseModel):
    """How a job ended, as its executor saw it: of the kind of its job. Each kind adds its own fields."""

    model_config = ConfigDict(frozen=True)

    kind: str
    files: dict[str, BlobAddress] = Field(default_factory=dict)
    """Its log directory's files by relative path: core puts them in the job's place under its own home."""

    def blobs(self) -> list[Cid]:
        """Every blob that it names: what the CAS holds before core applies it."""
        return list(self.files.values())


class Control(Protocol):
    """What running one job asks of the executor that runs it."""

    def begin(self) -> bool:
        """The agent is about to start. False when the executor stops: start nothing."""
        ...

    def started(self, handle: Handle, session_id: str | None) -> bool:
        """The start went through: record it. False when the job is no longer the executor's: it is killed."""
        ...

    def running(self, kill: Callable[[], None] | None) -> None:
        """The start is over: this is how to stop the job now. None when there is nothing to kill (any more)."""
        ...

    def process(self, proc: subprocess.Popen[bytes]) -> bool:
        """An evaluator started. Return False to kill it at once: a stop, or a job that is no longer the executor's."""
        ...

    def session(self, session_id: str) -> None:
        """The runtime reported the agent's session id."""
        ...

    def saved(self, session_file: Cid) -> None:
        """A new snapshot of the agent's session file is in the CAS (``SessionFile``)."""
        ...

    def asked(self) -> bool:
        """Whether the executor asked the job to stop: an evaluator that it killed is not run again."""
        ...

    def stopping(self) -> bool:
        """Whether the executor stops: an evaluation runs no more of its repeats then."""
        ...

    def handing_over(self) -> bool:
        """Whether the daemon restarts in place: the agent keeps running, with its worktree, for the next one."""
        ...

    def queued(self) -> list[list[Prompt]]:
        """The prompts that the agent's runtime took, one list for each ``Runtime.prompt``, oldest first."""
        ...

    def secrets(self) -> JobSecrets:
        """The values of the agent's secrets (``AgentJob.secrets``) that the home has, and its token, asked at its start."""
        ...

    def extended(self) -> float:
        """The seconds that the agent may run past its job's ``timeout``: its extensions in all (``Executor.extend``)."""
        ...


class Site(Protocol):
    """The machine that runs a job, as its kind's ``JobKind.run`` sees it: the job's place on it, and its checkout.

    The executor's kit makes one for the machine of the daemon and for each worker (``local_site``, ``remote_site``).
    """

    def log_dir(self, job: Job) -> Path:
        """The job's place on this machine: its log files, and its worktree."""
        ...

    def cwd(self, job: Job) -> Path:
        """Where the job runs: its checkout."""
        ...

    def checkout(self, job: Job) -> Materialised:
        """Make the job's checkout of its artifact at ``cwd``, with its links."""
        ...

    def remove(self, job: Job) -> None:
        """Remove the job's checkout, if it made one."""
        ...

    def files(self, job: Job) -> dict[str, Cid]:
        """Put the files of the job's log directory, its checkout aside, into the CAS, and return them by path."""
        ...


@dataclass(frozen=True)
class Applying:
    """What a job kind's ``apply`` reads of the daemon that applies a report."""

    home: Path
    stopping: bool
    """The daemon stops: what its job measured may be cut short, and the next daemon asks again."""


class HarnessSession(BaseModel):
    """An agent's harness session as its executor knows it while the agent runs: what of it the agent's row gets."""

    model_config = ConfigDict(frozen=True)

    id: str | None = None
    """The harness's session id, once the runtime reported it."""
    file: BlobAddress | None = None
    """The latest snapshot of its session file in the CAS."""


class AgentFinished(Report):
    """An agent's process ended, as its executor saw it. Core decides the rest (``Results.finished``)."""

    kind: Literal['agent_finished'] = 'agent_finished'
    process: AgentExit
    session_id: str | None = None
    session_file: BlobAddress | None = None
    """The harness's session file in the CAS, as it was when the agent ended: the last snapshot (``SessionFile``)."""
    usage: Usage | None = None
    """The tokens of this agent. Of a resumed session, only those after the resume."""
    unrestored: str | None = None
    """Why the worker started a new session in place of the job's ``resume``: the file could not be written back."""
    changed: bool = True
    """Whether the agent changed its checkout: a saved version, or an unsaved change outside the links."""
    work: Transfer | None = None
    """Its work: its versions, when the job asked for them back (``AgentJob.capture``) and there are some."""
    unsaved: Unsaved | None = None
    """What it left unsaved, when the job asked for its work back and the save of it failed."""
    read: list[CommentId] = Field(default_factory=list)
    """The prompts that the agent read: it took them from its runtime's queue."""
    unread: list[CommentId] = Field(default_factory=list)
    """The prompts that still waited in its runtime's queue when it ended."""
    workspace: dict[str, BlobAddress | None] = Field(default_factory=dict)
    """The files of its ``Workspace`` that it wrote, changed, added or removed (None), by relative path."""

    def blobs(self) -> list[Cid]:
        """Every blob that it names: what the CAS holds before core applies it."""
        work = [self.work.blob] if self.work is not None else []
        unsaved = [self.unsaved.blob] if self.unsaved is not None else []
        session = [self.session_file] if self.session_file is not None else []
        workspace = [blob for blob in self.workspace.values() if blob is not None]
        return [*self.files.values(), *workspace, *work, *unsaved, *session]


class AgentNotStarted(Report):
    """An agent's job did not start, so no process ran: its worktree, its secrets or its start failed, or a stop came."""

    kind: Literal['agent_not_started'] = 'agent_not_started'
    reason: str
    seconds: float = 0
    """How long it tried."""


type AgentReport = AgentFinished | AgentNotStarted
"""How an agent's job ended, as its executor saw it."""


class JobKind(ABC):
    """A kind of job besides an agent's, which a package declares in ``piloop.job_kinds``: ``<name> = <module>:<obj>``.

    Temporal's activity type. A worker runs the job (``run``) and the daemon applies its report under the lease's fence
    (``apply``), so the executor and its workers know nothing of what the job does. The ``evaluation`` kind measures a
    candidate.
    """

    JOB: ClassVar[type[Job]]
    """Its description, whose ``kind`` is a ``Literal`` of the entry point's name."""
    REPORT: ClassVar[type[Report]]
    """Its report, whose ``kind`` is a ``Literal`` of its own."""
    SLOTS: ClassVar[int] = 1
    """How many of its jobs one worker runs at the same time when its pool names none
    (``[plugins.executor.pools.<p>] slots``)."""

    @abstractmethod
    def id(self, job: Job) -> int:
        """The row of ``job``, which its lease names (``leases.job_id``): one job of the kind has one."""

    @abstractmethod
    def runner(self, job: Job) -> str:
        """What a worker must have to run ``job``: one of its ``runners``."""

    @abstractmethod
    def runners(self) -> list[str]:
        """What this machine has to run the kind's jobs: the worker says so when it registers."""

    @abstractmethod
    def target(self, job: Job) -> TargetRef:
        """The row that the events of ``job``'s lease go on."""

    @abstractmethod
    def run(self, job: Job, control: Control, site: Site, span: Span) -> Report:
        """Run ``job`` on this machine, and return its report. On a worker: the job's files are under ``site``."""

    @abstractmethod
    def apply(self, s: Session, job: Job, report: Report, daemon: Applying) -> None:
        """Apply ``report`` in the transaction of the fence. Raises ``Settled`` when ``job``'s row is settled already."""

    @abstractmethod
    def lost(self, s: Session, job_id: int) -> None:
        """Nothing came back from the job of row ``job_id``: it waits again. Raises ``Settled`` when it is settled."""

    @abstractmethod
    def waiting(self, s: Session, runners: Sequence[str]) -> bool:
        """Whether a job of the kind that a worker with ``runners`` could run waits in core for a slot, with no lease."""


def _run_already(name: str, kind: JobKind) -> str | None:
    if name == AGENT:
        return f'piloop runs jobs of the kind {name!r} already'
    if (declared := kind.JOB.model_fields['kind'].default) != name:
        return f'its job is of the kind {declared!r}, not {name!r}'
    return None


JOB_KINDS: Slot[JobKind] = Slot('job_kinds', TypeAdapter(InstanceOf[JobKind]), 'job kind', _run_already)
"""The kinds of job that packages add, by name. The daemon checks them when it starts."""


def job_kinds() -> dict[str, JobKind]:
    """Every kind of job besides an agent's, by name, in the order of their entry points."""
    return JOB_KINDS.once()


def kind_of(job: Job) -> JobKind:
    """The ``JobKind`` of ``job``, which is not an agent's. Raises ``KeyError`` for a kind that no package declares."""
    return job_kinds()[job.kind]


@dataclass(frozen=True)
class ByKind[M: BaseModel]:
    """``Annotated[Job, JOBS]`` and ``Annotated[Report, REPORTS]``: a model of the kind that its ``kind`` names.

    The models are an agent's, then each ``JobKind``'s, read when a value is validated: a kind that a package declares
    validates with no change here, as a provider's table does (``Capability.entry``).
    """

    own: tuple[type[M], ...]
    side: Callable[[JobKind], type[M]]

    def models(self) -> dict[str, type[M]]:
        """Each model by the value of its ``kind``."""
        found = [*self.own, *(self.side(kind) for kind in job_kinds().values())]
        return {m.model_fields['kind'].default: m for m in found}

    def validate(self, value: object) -> M:
        """``value`` as the model of its kind: a model, a dict, or JSON. Raises ``ValueError`` for an unknown kind."""
        data = json.loads(value) if isinstance(value, str | bytes) else value
        if isinstance(data, BaseModel):
            data = data.model_dump(mode='json')
        kind = data.get('kind') if isinstance(data, dict) else None
        model = self.models().get(kind) if isinstance(kind, str) else None
        if model is None:
            raise ValueError(f'no installed job kind has the kind {kind!r}')
        return model.model_validate(data)

    def __get_pydantic_core_schema__(self, source: type[object], handler: GetCoreSchemaHandler) -> CoreSchema:
        return core_schema.no_info_plain_validator_function(
            self.validate, serialization=core_schema.plain_serializer_function_ser_schema(_dumped, info_arg=True)
        )

    def __get_pydantic_json_schema__(self, schema: CoreSchema, handler: GetJsonSchemaHandler) -> JsonSchemaValue:
        return {'type': 'object'}


def _dumped(model: BaseModel, info: core_schema.SerializationInfo) -> dict[str, object]:
    return model.model_dump(mode=info.mode)


JOBS: ByKind[Job] = ByKind((AgentJob,), lambda kind: kind.JOB)
REPORTS: ByKind[Report] = ByKind((AgentFinished, AgentNotStarted), lambda kind: kind.REPORT)
JOB: TypeAdapter[Job] = TypeAdapter(Annotated[Job, JOBS])
"""A job's description in the CAS, of any installed kind."""
REPORT: TypeAdapter[Report] = TypeAdapter(Annotated[Report, REPORTS])
"""A report on the wire, of any installed kind."""


@dataclass(frozen=True)
class AgentKey:
    """The key of an agent's job: its ``agent_sessions`` row."""

    agent_session: AgentSessionId


@dataclass(frozen=True)
class JobKey:
    """The key of a job of a ``JobKind``: its kind, and its row (``JobKind.id``)."""

    kind: str
    id: int


type Key = AgentKey | JobKey
"""The key of any job."""


@deprecated.until('0.5.0', "EvaluationKey(n) of 0.3.1 is JobKey('evaluation', n): delete EvaluationKey")
def EvaluationKey(evaluation: int) -> JobKey:
    """The key of an evaluation's job, as 0.3.1 named it: ``JobKey('evaluation', evaluation)``."""
    return JobKey('evaluation', evaluation)


def key_of(job: Job) -> Key:
    """Return the key of ``job``."""
    return AgentKey(job.agent_session) if isinstance(job, AgentJob) else JobKey(job.kind, kind_of(job).id(job))


@dataclass(frozen=True)
class LeftJob:
    """A job that a previous daemon process left in flight, for ``Executor.adopt``: its key. Its executor finds it."""

    key: Key


@dataclass(frozen=True)
class Prompt:
    """A message for a running agent's next turn: a comment, by its id, and the text that the agent gets."""

    id: CommentId
    text: str


class Settled(Exception):
    """A result for a job whose row is settled already: closed, measured, or gone. Core refused it; nothing changed."""


type Guard = Callable[[Session], None]
"""An executor's own check and write, which a ``Results`` operation runs in its transaction before its own writes.

The leases kit passes one: its fence, and the lease's change. It raises to refuse, and then nothing changes. Any other
executor passes none.
"""


class Results(Protocol):
    """The core operations that an executor calls on what its jobs do. Each commits in a write transaction of its own.

    Core checks only the domain: a job's row closes once (``Settled``).
    """

    def started(
        self,
        key: AgentKey,
        handle: dict[str, object],
        session_id: str | None,
        *,
        machine: str | None = None,
        guard: Guard | None = None,
    ) -> None:
        """An agent started: record where it runs (``handle``), which steers it. Raises ``Settled``.

        ``machine`` names another machine that runs it, as the executor knows it (a worker); None: this daemon's.
        """
        ...

    def session(self, key: AgentKey, session: HarnessSession, *, guard: Guard | None = None) -> None:
        """The agent's harness session as the executor knows it now: its id and its file's latest snapshot.

        The usage needs the id. Raises ``Settled``.
        """
        ...

    def finished(self, job: Job, report: Report, *, guard: Guard | None = None) -> None:
        """Apply the result of a job that ended: close the agent's row, or its kind's ``apply``. Raises ``Settled``.

        ``report`` is of the job's kind.
        """
        ...

    def lost(self, key: Key, reason: str, *, guard: Guard | None = None) -> None:
        """Nothing came back from a job. Raises ``Settled``.

        An agent's run ends ``worker_lost``, and a job of another kind waits again (``JobKind.lost``).
        """
        ...

    def queued(self, key: AgentKey, prompt: CommentId, error: str | None) -> None:
        """A prompt waits for its agent in the agent's runtime, or ``error`` says why it does not.

        Whether the agent read it comes in the agent's report (``AgentFinished.read``).
        """
        ...


@dataclass(frozen=True)
class Host:
    """What piloop gives an executor."""

    db: Database
    results: Results
    tracer: Tracer
    home: Path
    cas: Cas
    """The CAS that jobs' inputs and results go through: a job names blobs, never paths."""
    repos: Callable[[RepoId], Path]
    """The checkout of a repo of record (``Source.repo``). Raises ``KeyError`` for a campaign that is gone."""
    records: Callable[[RepoId], Repository]
    """A repo of record, opened with its campaign's vcs provider: what a worker that lacks versions is sent from. Raises
    ``KeyError`` for a campaign that is gone."""
    runtimes: RuntimeFactory
    """Makes the runtime of a job (``AgentJob.runtime``) on this machine."""
    reserve: int
    """Bytes kept free on the disk of ``home`` (``[storage] min_free_gb``): below it, a job's end evicts the linked
    inputs that no job uses (``Site.evict_inputs``)."""
    network: Network
    """The server's network: what gives a machine that the executor starts its credential (``Network.enroll``)."""
    secrets: SecretsProvider
    """The home's secrets: an agent's job gets the values of its ``AgentJob.secrets`` at its start (``values``)."""


@dataclass(frozen=True, init=False)
class Capacity:
    """How many more jobs of each kind an executor can take now: its free slots. None: no bound."""

    agents: int | None
    slots: Mapping[str, int | None]
    """The free slots of each ``JobKind`` by name. A kind that it does not name has none."""

    def __init__(self, agents: int | None, slots: Mapping[str, int | None] | None = None, **old: int | None) -> None:
        found = dict(slots or {})
        if unknown := sorted(set(old) - {'evaluations'}):
            raise TypeError(f'Capacity takes agents and slots, not {", ".join(unknown)}')
        if 'evaluations' in old:
            deprecated.until('0.5.0', "Capacity(evaluations=n) of 0.3.1 is Capacity(slots={'evaluation': n})")
            found.setdefault('evaluation', old['evaluations'])
        object.__setattr__(self, 'agents', agents)
        object.__setattr__(self, 'slots', MappingProxyType(found))

    def free(self, kind: str) -> int | None:
        """The free slots of jobs of ``kind``: ``AGENT``, or a ``JobKind``'s name. None: no bound."""
        return self.agents if kind == AGENT else self.slots.get(kind, 0)

    @property
    def evaluations(self) -> int | None:
        """The free slots of evaluations, as 0.3.1 named them."""
        deprecated.until('0.5.0', "Capacity.evaluations of 0.3.1 is Capacity.free('evaluation')")
        return self.free('evaluation')


class Executor[C: ExecutorConfig](Contributes, Protocol):
    """Runs the jobs that piloop submits, and reports their results through ``Host.results``.

    Its housekeeping is a consumer of its ``contribution``, and what its workers on other machines call is its router
    (``piloop.net.server.executor_router``).

    The daemon calls it from its threads; an executor makes its methods safe for that.
    """

    def capacity(self) -> Capacity:
        """How many more jobs of each kind it can take now: its free slots, less the jobs it has and has not started.

        Core submits no more than that, in its own order: it launches agents across campaigns, and the jobs of each other
        kind in its owner's order (evaluations: the evaluation queue's). The order and the fairness are not the executor's: the home's launch policy picks
        the campaign of each agent slot (``LAUNCH_POLICIES``).
        """
        ...

    def capacity_events(self) -> frozenset[type[EventModel]]:
        """The events of this executor that say its capacity grew: core then launches what waits for a slot.

        A job that ends needs none, since core's own events say it. Empty for an executor whose slots change only so.
        """
        ...

    def configure(self, config: C) -> None:
        """Take ``config``, the one that opened it with the live limits applied. The daemon calls it when one changes."""
        ...

    def submit(self, job: Job) -> None:
        """Run ``job``, now or when a slot is free. Every job ends with ``Results.finished`` or ``Results.lost``."""
        ...

    def prompt(self, key: AgentKey, prompt: Prompt) -> None:
        """Give ``prompt`` to the running agent's runtime at least once, then call ``Results.queued``.

        Core calls it once for each prompt in each daemon process, and again after a restart when no outcome was
        recorded: an executor that may deliver it twice skips the ids that it has seen.
        """
        ...

    def extend(self, key: AgentKey, seconds: float) -> None:
        """Let a running agent run ``seconds`` past its job's ``timeout``: its extensions in all.

        Core calls it once an extension is recorded, and again after a restart, with the same or a larger total.
        """
        ...

    def cancel(self, key: Key) -> None:
        """Stop a job: a kill or a cancel. It still reports how it ended. Called again, it tries again."""
        ...

    def adopt(self, left: Sequence[LeftJob]) -> list[Key]:
        """Take over the jobs that a previous daemon process left, and return the keys of those it cannot.

        Core fails the rows of an agent that it cannot adopt, and asks again for a job of another kind.
        """
        ...

    def stop(self, timeout: float) -> None:
        """Stop every job, and wait up to ``timeout`` for them to report. Submit no more."""
        ...

    def hand_over(self) -> None:
        """Leave the jobs running for the next daemon process (an in-place restart), which adopts them."""
        ...

    def close(self) -> None:
        """Release what the executor holds. The daemon calls it last."""
        ...


class ExecutorConfig(Parameters, ABC):
    """An executor's ``[plugins.executor]``: its fields are the keys of the table besides ``provider``."""

    @abstractmethod
    def open(self, host: Host) -> Executor[Self]:
        """Make the executor. The daemon calls it once, when it starts."""


EXECUTORS = Capability('executor', ExecutorConfig, table=One(Scope.HOME, default='workers'))
"""Every executor by name. ``workers``, built in and the default, runs every job on a lease
(``piloop.plugins.executor.kit.leases``)."""
