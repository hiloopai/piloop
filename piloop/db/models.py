"""Storage models. The database is the contract that every agent reads and writes through ``piloop sql``.

Every NOT NULL column with a default also has a server default, so the raw-SQL inserts in the docs work without
timestamps or statuses. ``docs/reference/schema.mdx`` (the DDL) is generated from these models.
"""

import sys
import warnings
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, ClassVar, Literal, get_args, get_origin

from covalence import Cid
from pydantic import (
    BaseModel,
    ConfigDict,
    JsonValue,
    PlainSerializer,
    PlainValidator,
    TypeAdapter,
    model_serializer,
)
from sqlalchemy import (
    JSON,
    CheckConstraint,
    Column,
    Computed,
    DateTime,
    Enum,
    FetchedValue,
    ForeignKey,
    Index,
    Integer,
    Select,
    String,
    event,
    func,
    inspect,
    select,
    text,
)
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, composite, mapped_column, object_session
from sqlalchemy.orm.attributes import set_committed_value
from sqlalchemy.types import TypeDecorator

from piloop import deprecated
from piloop.api.contract import Access
from piloop.config import CampaignConfig
from piloop.deprecated import PiloopDeprecationWarning
from piloop.ids import (
    CORE_KINDS,
    AgentSessionId,
    CampaignId,
    CampaignStatus,
    CommentId,
    CommentKind,
    Revision,
    RoleName,
    TargetKind,
    TargetRef,
)
from piloop.plugins.campaign_type.kit.evaluation.ids import EvaluatorId
from piloop.plugins.campaign_type.kit.runs.ids import (
    RunId,
)
from piloop.plugins.host import Slot
from piloop.usage import Usage

if TYPE_CHECKING:  # the components' tables import this module
    from piloop.plugins.campaign_type.kit.backlog.tables import Idea
    from piloop.plugins.campaign_type.kit.evaluation.tables import Evaluator


def utcnow() -> datetime:
    """The current time, with the UTC time zone."""
    return datetime.now(UTC)


class PydanticJSON[T](TypeDecorator[T]):
    """A JSON column that validates on load and on save, through one ``TypeAdapter``.

    The values are frozen models, so a change in place cannot be lost: assign a new value. None is SQL NULL.
    """

    impl = JSON(none_as_null=True)
    cache_ok = True

    def __init__(self, adapter: TypeAdapter[T]) -> None:
        super().__init__()
        self.adapter = adapter

    def process_bind_param(self, value: T | None, dialect: Dialect) -> object:
        return None if value is None else self.adapter.dump_python(self.adapter.validate_python(value), mode='json')

    def process_result_value(self, value: object, dialect: Dialect) -> T | None:
        return None if value is None else self.adapter.validate_python(value)


class UTCDateTime(TypeDecorator[datetime]):
    """A datetime that is always UTC. SQLite drops the time zone when it stores a value, so this type adds it back."""

    impl = DateTime
    cache_ok = True

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        return value.replace(tzinfo=UTC) if value is not None and value.tzinfo is None else value


class CidText(TypeDecorator[Cid]):
    """A covalence ``Cid``, stored as its text."""

    impl = String
    cache_ok = True

    def process_bind_param(self, value: Cid | None, dialect: Dialect) -> str | None:
        return None if value is None else str(value)

    def process_result_value(self, value: str | None, dialect: Dialect) -> Cid | None:
        return None if value is None else Cid(value)


class Pause(StrEnum):
    """What a paused campaign holds (``piloop pause``). Running work goes on either way, and nothing is escalated.

    ``launches``: nothing launches, no run is queued and no role is requested. The daemon still measures again what a
    new evaluator or a request needs. ``all``: those measurements wait too.
    """

    LAUNCHES = 'launches'
    ALL = 'all'


class AgentSessionState(StrEnum):
    """The process state of an agent, kept by the daemon. ``requested`` is a request to launch, not a process.

    ``ending``: the daemon ended the agent, for a stop or a cancel, and its process is not gone yet. From then on, a
    finding that the agent writes is refused.
    """

    REQUESTED = 'requested'
    STARTING = 'starting'
    RUNNING = 'running'
    ENDING = 'ending'
    EXITED = 'exited'
    KILLED = 'killed'


LIVE = (AgentSessionState.STARTING, AgentSessionState.RUNNING, AgentSessionState.ENDING)
OPEN = (AgentSessionState.REQUESTED, *LIVE)
"""An agent that is requested or live: its row is not closed yet."""
ENDED = (AgentSessionState.EXITED, AgentSessionState.KILLED)


class LeaseKind(StrEnum):
    """What a lease's job is: an agent session, or an evaluation of the evaluation queue."""

    AGENT = 'agent'
    EVALUATION = 'evaluation'


class LeaseState(StrEnum):
    """The lifecycle of a lease (``piloop.plugins.executor.kit.leases``).

    ``queued`` waits for a worker. ``acquired`` is taken, ``running`` is started. Then ``reported`` (the worker's report
    applied), ``expired`` (the worker went silent past ``toleration_seconds``, or reported its job gone), or
    ``cancelled`` (the job ended before a worker reported it, at a daemon stop for example).
    """

    QUEUED = 'queued'
    ACQUIRED = 'acquired'
    RUNNING = 'running'
    REPORTED = 'reported'
    EXPIRED = 'expired'
    CANCELLED = 'cancelled'


LIVE_LEASES = (LeaseState.ACQUIRED, LeaseState.RUNNING)
OPEN_LEASES = (LeaseState.QUEUED, *LIVE_LEASES)


def by_value[E: StrEnum](enum: type[E]) -> Enum:
    """Store enum values, not member names, with a CHECK constraint, so that raw SQL cannot write other values."""
    return Enum(
        enum, values_callable=lambda e: [m.value for m in e], native_enum=False, length=16, create_constraint=True
    )


class KindText(TypeDecorator[TargetKind]):
    """A ``TargetKind``, stored as its name. ``target_kinds`` says which names there are."""

    impl = String(16)
    cache_ok = True

    def process_bind_param(self, value: TargetKind | None, dialect: Dialect) -> str | None:
        return None if value is None else str(value)

    def process_result_value(self, value: str | None, dialect: Dialect) -> TargetKind | None:
        return None if value is None else TargetKind(value)


NOW = func.now()
OPTIMIZATION = 'optimization'
"""The built-in campaign type: improve the code against its evaluate command, candidate by candidate."""
PROCESS = 'process'
"""The built-in evaluator runner: the evaluate command, run as a process."""
EMPTY_JSON = text("'{}'")


def text_checks(table: str, *columns: str, nullable: tuple[str, ...] = ()) -> tuple[CheckConstraint, ...]:
    """One CHECK for each column that agents write with raw SQL: the value must be text (or NULL, for ``nullable``).

    SQLite stores bound bytes as a BLOB even in a VARCHAR column, and every reader that expects ``str`` then fails.
    The name of the constraint says what it requires, so the error message of every client says it too.
    """
    return tuple(
        CheckConstraint(
            f"typeof({c}) IN ('text', 'null')" if c in nullable else f"typeof({c}) IN ('text')",
            name=f'{table}.{c} must be a text value',
        )
        for c in (*columns, *nullable)
    )


class ReadBy(StrEnum):
    """Who reads the events of a kind."""

    CONSUMERS = 'consumers'
    """A consumer of the daemon's loop wakes on it: it says that something changed that a consumer acts on."""
    READERS = 'readers'
    """Only readers read it: status, history, an agent's SQL. It records what was done, and wakes nothing."""


class EventModel(BaseModel):
    """The payload of one kind of event, with its ``kind``: one model for each kind, or for a few that share a shape.

    ``kind`` is a ``Literal`` of the kinds that the model is for. Defining the model registers each of them, and a kind
    that two models register fails at import. Each layer declares the events that it writes, next to the code that
    writes them, and a plugin declares its own the same way. Readers ``match`` on the classes that they import.

    ``kind`` is only the event's own. A field that holds the kind of a row is named after its type, such as
    ``comment_kind: CommentKind`` or ``link_kind: LinkKind``. An agent's kind is its ``role: RoleName``.

    The stored rows keep their shape, and SQL reads them as stored, so a model changes in these ways only:

    - A new field has a default, so the rows before it still load.
    - A field whose meaning changes is a new kind, such as ``run.finished.v2``.
    - A field that goes away, or a rename, is a migration that rewrites the rows, and a line in the upgrade guide.

    A kind that nothing writes any more is retired (``retire``), so that no model takes its name again.

    A consumer of the daemon wakes on each kind, unless its model says that only readers read it (``READ_BY``).
    """

    model_config = ConfigDict(frozen=True, extra='forbid', serialize_by_alias=True, validate_by_name=True)

    READ_BY: ClassVar[ReadBy] = ReadBy.CONSUMERS
    """Who reads its events. A test fails on a kind that wakes no consumer and is not ``ReadBy.READERS``, and on one
    that is and wakes one."""

    kind: str

    _registry: ClassVar[dict[str, type[EventModel]]] = {}
    _retired_kinds: ClassVar[set[str]] = set()

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs: object) -> None:
        super().__pydantic_init_subclass__(**kwargs)
        for kind in cls.kinds():
            if kind in EventModel._retired_kinds:
                raise TypeError(f'{cls.__qualname__}: event kind {kind!r} is retired')
            known = EventModel._registry.setdefault(kind, cls)
            if known is not cls:
                raise TypeError(f'{cls.__qualname__}: event kind {kind!r} is {known.__module__}.{known.__qualname__}')

    @classmethod
    def retire(cls, kind: str) -> None:
        """Retire ``kind``: a release wrote it, and nothing writes it now. Its rows load as an ``UnknownEvent``.

        Raises:
            TypeError: a model registers it.
        """
        if (known := EventModel._registry.get(kind)) is not None:
            raise TypeError(f'event kind {kind!r} is {known.__module__}.{known.__qualname__}: it cannot be retired')
        EventModel._retired_kinds.add(kind)

    @classmethod
    def registered(cls) -> dict[str, type[EventModel]]:
        """Every kind that a loaded module registers, with its model."""
        return dict(EventModel._registry)

    @classmethod
    def retired_kinds(cls) -> frozenset[str]:
        """The kinds that are retired."""
        return frozenset(EventModel._retired_kinds)

    @classmethod
    def kinds(cls) -> tuple[str, ...]:
        """The kinds that the model is for: the values of its ``kind`` literal. Empty for a base model."""
        annotation = cls.model_fields['kind'].annotation
        return get_args(annotation) if get_origin(annotation) is Literal else ()


class UnknownEvent(EventModel):
    """An event of a kind that no loaded module registers, such as the event of a plugin that is not installed.

    Its payload is the stored JSON without ``kind``. No reader matches it.
    """

    payload: dict[str, JsonValue]

    @model_serializer
    def _flat(self) -> dict[str, JsonValue]:
        return {'kind': self.kind, **self.payload}


def _event(value: object) -> EventModel:
    match value:
        case EventModel():
            return value
        case {'kind': str(kind)}:
            model = EventModel._registry.get(kind)
            if model is not None:
                return model.model_validate(value)
            return UnknownEvent.model_validate(
                {'kind': kind, 'payload': {k: v for k, v in value.items() if k != 'kind'}}
            )
        case _:
            raise ValueError(f'an event is a JSON object with its kind in "kind", not {value!r}')


def _dumped(event: EventModel) -> dict[str, object]:
    return event.model_dump(mode='json')


EVENT: TypeAdapter[EventModel] = TypeAdapter(Annotated[EventModel, PlainValidator(_event), PlainSerializer(_dumped)])
"""Load the payload of an ``events`` row as the model that registers its kind, or as an ``UnknownEvent``.

A registered kind validates strictly: a payload that does not fit its model raises."""


class Base(DeclarativeBase):
    """The declarative base, with the mappings for JSON, UTC datetimes and enums stored by value."""

    type_annotation_map = {
        datetime: UTCDateTime,
        CampaignId: Integer,
        RunId: Integer,
        EvaluatorId: Integer,
        AgentSessionId: Integer,
        CommentId: Integer,
        RoleName: String,
        Revision: String,
        dict[str, float]: JSON,
        dict[str, object]: JSON,
        Usage: PydanticJSON(TypeAdapter(Usage)),
        CampaignConfig: PydanticJSON(TypeAdapter(CampaignConfig)),
        EventModel: PydanticJSON(EVENT),
        list[str]: JSON,
        CampaignStatus: by_value(CampaignStatus),
        Pause: by_value(Pause),
        TargetKind: KindText(),
        CommentKind: by_value(CommentKind),
        AgentSessionState: by_value(AgentSessionState),
        LeaseKind: by_value(LeaseKind),
        LeaseState: by_value(LeaseState),
        Access: by_value(Access),
    }


def _pointed_at_already(name: str, _rows: type[Base]) -> str | None:
    return f'comments and events point at {name!r} of piloop already' if name in CORE_KINDS else None


TARGET_KINDS: Slot[type[Base]] = Slot('target_kinds', TypeAdapter(type[Base]), 'target kind', _pointed_at_already)
"""What else comments and events may point at, by name: the table of its rows. The daemon checks them when it
starts, and ``migrate`` writes each into ``target_kinds``."""


def known_kinds() -> frozenset[TargetKind]:
    """Core's kinds and the installed plugins' (``TARGET_KINDS``), by their entry points: nothing is imported."""
    return CORE_KINDS | {TargetKind(name) for name in TARGET_KINDS.paths()}


class Tracked:
    """Who updated a row last, and when. Triggers keep these columns on every table whose rows change.

    ``server_onupdate=FetchedValue()`` makes the ORM expire both attributes after it sends an UPDATE. An instance then
    reads what the triggers wrote, and does not keep its old value (sessions run with ``expire_on_commit=False``).
    """

    updated_at: Mapped[datetime] = mapped_column(
        default=utcnow, server_default=NOW, server_onupdate=FetchedValue(), sort_order=1
    )
    """Set by a persistent trigger on every UPDATE, so an UPDATE in raw SQL sets it too."""
    updated_by: Mapped[str | None] = mapped_column(server_onupdate=FetchedValue(), sort_order=1)
    """The actor of the connection that made the last UPDATE (``human``, ``daemon``, ``<kind>#<agent session id>``). A TEMP
    trigger of each connection sets it (``piloop.db.sql.install_actor``, which also logs decisions in ``events``).
    NULL until the first UPDATE of the row."""


PAUSE_CHECK = 'campaigns.paused and campaigns.paused_reason go together'


class Campaign(Tracked, Base):
    """A git repository that piloop optimizes. ``piloop init`` creates it."""

    __tablename__ = 'campaigns'
    __table_args__ = (
        CheckConstraint('(paused IS NULL) = (paused_reason IS NULL)', name=PAUSE_CHECK),
        *text_checks('campaigns', nullable=('paused_reason',)),
        Index('ix_campaigns_name', 'name', unique=True, sqlite_where=text(f"status != '{CampaignStatus.RETIRED}'")),
    )

    id: Mapped[CampaignId] = mapped_column(primary_key=True)
    name: Mapped[str]
    """Unique among the campaigns that are not retired: a retired campaign's name is free again."""
    path: Mapped[str]
    """The absolute path of the repository. ``piloop.toml`` is at its root."""
    config: Mapped[CampaignConfig] = mapped_column(deferred=True)
    """The ``piloop.toml`` as the last ``piloop init`` stored it: the config that the daemon schedules from. It loads
    when it is first read, so a copy that no longer validates (a trigger of a package that is not installed) fails
    only that read."""
    evaluator_id: Mapped[EvaluatorId | None]
    """The ``evaluators.id`` that the config describes now. Measurements under older evaluators stay, but no longer
    decide verdicts. A plain integer, not a foreign key, because evaluators reference campaigns."""
    status: Mapped[CampaignStatus] = mapped_column(
        default=CampaignStatus.ACTIVE, server_default=CampaignStatus.ACTIVE.value
    )
    """``stopped`` after a ``[stop]`` condition held: running work finishes and nothing new is requested. To resume,
    set it back to ``active`` and clear ``stopped_reason``. ``piloop init`` does this when the ``[stop]`` table
    changed. ``retired`` after ``piloop campaign retire``, for good."""
    stopped_reason: Mapped[str | None]
    """The condition that ended the campaign, for example ``target: accuracy 0.8531 ≥ 0.85``."""
    paused: Mapped[Pause | None]
    """What ``piloop pause`` holds, NULL while the campaign is not paused. ``piloop resume`` clears it. Each change is a
    ``campaign.paused`` event with ``paused_reason``."""
    paused_reason: Mapped[str | None]
    """Why the campaign is paused, as ``piloop pause --reason`` said it. NULL while it is not paused."""
    repo: Mapped[str | None]
    """The repository that the campaign works on, the same for every clone of it: the URL of its ``origin`` remote
    (else of its first remote) as ``host/owner/name``, else ``path``. ``piloop init`` sets it. Past work on the same
    code is ``WHERE repo = (SELECT repo FROM campaigns WHERE id = <p>)``."""
    type: Mapped[str] = mapped_column(default=OPTIMIZATION, server_default=OPTIMIZATION)
    """What the campaign does: a campaign type, by its name. Every campaign is ``optimization``. Each change is a
    ``campaign.type`` event."""
    parent_id: Mapped[CampaignId | None] = mapped_column(ForeignKey('campaigns.id'))
    """The campaign that this one is under, for example the one that spawned it, NULL for a root. It makes a tree of
    the campaigns of one piloop home. ``piloop.campaign.ops.set_parent`` sets it, and each change is a
    ``campaign.parent_id`` event. The ``campaigns_parent`` triggers refuse a cycle. A raw DELETE of a campaign that has
    children fails on the foreign key. Nothing else reads it yet: each campaign runs on its own."""
    created_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=NOW)

    @property
    @deprecated.until('0.5.0', "Campaign.ideas of 0.3: delete it, a core table's link to the backlog's ideas")
    def ideas(self) -> list[Idea]:
        """Its ideas by id, as in 0.3, with a warning: select the backlog's ``Idea`` by ``campaign_id`` instead."""
        from piloop.plugins.campaign_type.kit.backlog.tables import Idea  # here: tables imports this module

        _moved('Campaign.ideas', 'select(Idea).where(Idea.campaign_id == campaign.id)')
        return list(_session_of(self).scalars(select(Idea).where(Idea.campaign_id == self.id).order_by(Idea.id)))

    @property
    @deprecated.until('0.5.0', "Campaign.evaluator of 0.3: delete it, a core table's link to the evaluation's table")
    def evaluator(self) -> Evaluator | None:
        """Its current evaluator, as in 0.3, with a warning: the evaluation component's ``evaluator_of``."""
        from piloop.plugins.campaign_type.kit.evaluation.tables import evaluator_of  # here: tables imports this module

        _moved('Campaign.evaluator', 'evaluator_of(s, campaign)')
        return evaluator_of(_session_of(self), self)


def _session_of(row: Base) -> Session:
    """The session that ``row`` is in. Raises ``ValueError`` for a row in none."""
    if (s := object_session(row)) is None:
        raise ValueError(f'{row!r} is in no session')
    return s


def _moved(old: str, new: str) -> None:
    warnings.warn(
        f"{old} moved: read {new} of the campaign type kit's components",
        PiloopDeprecationWarning,
        stacklevel=3,
    )


NOT_RETIRED = Campaign.status != CampaignStatus.RETIRED
"""The campaigns that hold their names, that the daemon schedules, and that a listing shows unless asked."""


def count[*Ts](s: Session, query: Select[*Ts]) -> int:
    """The number of rows that ``query`` selects."""
    return s.scalar(select(func.count()).select_from(query.subquery())) or 0


class Lease(Base):
    """A lease (``piloop.plugins.executor.kit.leases``): a time-limited claim on one job by one worker (``piloop.plugins.executor.kit.leases``).

    The table is in core's schema until a plugin can own tables. ``WorkersExecutor.submit`` inserts a ``queued`` lease that names its job by address (``job_cid``). A worker acquires it, starts the job and
    renews the lease with a heartbeat every ``renew_seconds``. The id is the **fencing token**: it only grows, and
    a job's next lease always has a larger one. A report applies only on the live, newest lease of its job, so a worker
    that was presumed dead cannot overwrite newer work. Rows are kept: a refused report stays as a
    ``lease.stale_report`` event on the job's row.
    """

    __tablename__ = 'leases'
    __table_args__ = (
        CheckConstraint(
            "(kind = 'agent') = (agent_session_id IS NOT NULL) AND (kind = 'evaluation') = (evaluation_id IS NOT NULL)",
            name='leases.a lease has the job of its kind',
        ),
        Index('ix_leases_state', 'state'),
        Index('ix_leases_agent_session', 'agent_session_id'),
        Index('ix_leases_evaluation', 'evaluation_id'),
        Index('ix_leases_acquire_request', 'acquire_request', unique=True),
        *text_checks('leases', nullable=('worker', 'report_request')),
        {'sqlite_autoincrement': True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    """The fencing token. AUTOINCREMENT: an id is never used again, so a newer lease always has a larger one."""
    kind: Mapped[LeaseKind]
    agent_session_id: Mapped[AgentSessionId | None] = mapped_column(ForeignKey('agent_sessions.id', ondelete='CASCADE'))
    """The job of an ``agent`` lease."""
    evaluation_id: Mapped[int | None]
    """The job of an ``evaluation`` lease: an ``evaluations`` id. No foreign key: the queue row goes when it is
    measured, and the lease stays."""
    state: Mapped[LeaseState] = mapped_column(default=LeaseState.QUEUED, server_default=LeaseState.QUEUED.value)
    worker: Mapped[str | None]
    """The worker that acquired it. NULL while it is queued."""
    job_cid: Mapped[Cid] = mapped_column(CidText)
    """The job description (``AgentJob``, ``EvaluationJob``), a JSON blob in the CAS: what to materialize, what to run,
    and what to return. One job queued again on a new lease is one blob."""
    handle: Mapped[dict[str, object]] = mapped_column(default=dict, server_default=EMPTY_JSON)
    """What the worker needs to find its job again after a restart: the runtime's handle of an agent, or the process
    group of an evaluator."""
    created_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=NOW)
    acquired_at: Mapped[datetime | None]
    started_at: Mapped[datetime | None]
    renewed_at: Mapped[datetime | None]
    """The last heartbeat, or the acquire."""
    expires_at: Mapped[datetime | None]
    """``renewed_at`` plus ``toleration_seconds``. Past it, the scheduler expires the lease."""
    revoked_at: Mapped[datetime | None]
    """When the daemon asked the worker to stop the job: a kill or a cancel. The worker still reports it."""
    ended_at: Mapped[datetime | None]
    acquire_request: Mapped[str | None]
    """The request id of the acquire that took it. An acquire sent again with it gets this lease again."""
    report_request: Mapped[str | None]
    """The request id of the report that applied. A report sent again with it gets the same ``receipt``."""
    receipt: Mapped[dict[str, object]] = mapped_column(default=dict, server_default=EMPTY_JSON)
    """What the report applied, or why the lease ended."""


class Token(Base):
    """A bearer token of the server of ``piloop serve`` and ``piloop cas serve`` (``piloop.net.tokens``).

    Only the SHA-256 of the secret is kept: the secret is shown once, when the token is created. ``piloop sql`` cannot
    write this table, so an agent cannot add a token. Revoking a token deletes its row.
    """

    __tablename__ = 'tokens'

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(unique=True)
    """The caller's name, such as a worker's id, or the signature of the agent that the token is bound to."""
    access: Mapped[Access]
    sha256: Mapped[str] = mapped_column(unique=True)
    """The hex SHA-256 of the secret."""
    agent_session_id: Mapped[AgentSessionId | None] = mapped_column(unique=True)
    """The agent session that the token was minted for when it launched: it acts as that agent while the session lives.

    No foreign key: its cascade would write this table, which an agent's ``piloop sql`` may not, so an agent's delete of
    a campaign would fail. A token whose session is gone acts as nobody. A session that reuses its id gets its own token
    at its start (``mint_agent``), in place of the old one.
    """
    created_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=NOW)


class KnownKind(Base):
    """A kind that events and comments may point at: core's, or one that a plugin declared (``TARGET_KINDS``).

    ``migrate`` writes each installed kind. A kind of a plugin that is no longer installed stays, with its rows.
    """

    __tablename__ = 'target_kinds'

    id: Mapped[TargetKind] = mapped_column(primary_key=True)
    """The kind's name, such as ``idea``."""
    owner: Mapped[str]
    """The schema that has the table of its rows: ``core``, or a plugin's, such as ``optimization``."""


class Migration(Base):
    """Core or a plugin that migrated this database: one row each (``piloop.db.schema``). Only ``migrate`` writes it.

    ``fingerprint`` is that of its revisions up to the one in its version table (``fingerprints``).
    """

    __tablename__ = 'migrations'

    name: Mapped[str] = mapped_column(primary_key=True)
    """``core``, or a plugin's schema name, such as ``memory/local``."""
    package: Mapped[str]
    """The distribution that declared the schema: what to install again when it is missing."""
    fingerprint: Mapped[int]


class Limit(Base):
    """A live limit (``piloop limits set``): a value for one ``Live`` key of a provider's parameters, over ``config.toml``.

    The daemon applies the rows at once, without a restart. The table is in core's schema, as the leases are.
    """

    __tablename__ = 'limits'
    __table_args__ = (*text_checks('limits', 'key', 'reason', 'actor'),)

    key: Mapped[str] = mapped_column(primary_key=True)
    """The dotted key: the capability, then the path in its parameters, ``executor.pools.serve.evaluations``."""
    value: Mapped[JsonValue] = mapped_column(JSON, nullable=False)
    """JSON: a null is a value too, as ``agents = null``, no bound."""
    reason: Mapped[str]
    actor: Mapped[str]
    """Who set it."""
    at: Mapped[datetime] = mapped_column(default=utcnow, server_default=NOW)


class Comment(Tracked, Base):
    """The forum and the human's inbox. Anything with an id can get a comment: ``idea#12``, ``run#7``, ``candidate#30``.

    A comment with a ``priority`` is for the human. It stays in the inbox (``piloop inbox``) until someone acknowledges
    it (``acked_at``, ``acked_by``). An escalation always has a priority (a CHECK makes sure). The orchestrator gives
    a priority to every decision it makes for the human, so that the human can review and change each one later. The
    rest of the forum (implementers that steer each other, research findings) has no priority.
    """

    __tablename__ = 'comments'
    __table_args__ = (
        Index('ix_comments_target', 'target_kind', 'target_id'),
        # the inbox: the widget counts the items not acknowledged on every refresh, and the CLI lists them by priority
        Index('ix_comments_inbox', 'priority', 'id', sqlite_where=text('priority IS NOT NULL AND acked_at IS NULL')),
        CheckConstraint("kind != 'escalation' OR priority IS NOT NULL", name='escalation_needs_priority'),
        CheckConstraint("in_reply_to IS NULL OR target_kind = 'comment'", name='reply_is_on_a_comment'),
        CheckConstraint("target_kind != 'home'", name='comment_not_on_home'),
        *text_checks('comments', 'author', 'body'),
    )

    id: Mapped[CommentId] = mapped_column(primary_key=True)
    target_kind: Mapped[TargetKind] = mapped_column(ForeignKey('target_kinds.id', name='targetkind'))
    target_id: Mapped[int]
    target: Mapped[TargetRef] = composite('target_kind', 'target_id')
    in_reply_to: Mapped[CommentId | None] = mapped_column(ForeignKey('comments.id', ondelete='SET NULL'))
    """The comment that a reply answers, as email's In-Reply-To. The target of a reply stays the root of its thread.

    NULL for a comment that is not a reply, and for a reply written before piloop recorded it.
    """
    author: Mapped[str]
    """Who wrote the comment, for example ``human``, ``orchestrator`` or ``implementer#12``."""
    kind: Mapped[CommentKind] = mapped_column(default=CommentKind.NOTE, server_default=CommentKind.NOTE.value)
    body: Mapped[str]
    priority: Mapped[int | None]
    """When set, the comment is for the human: 0 (urgent, notify now) to 4 (someday), as on ideas. NULL: forum only."""
    acked_at: Mapped[datetime | None]
    """When someone acknowledged the inbox item. NULL while it waits in the inbox."""
    acked_by: Mapped[str | None]
    """Who acknowledged it: ``human`` with ``piloop inbox ack``, or ``orchestrator`` when the orchestrator did."""
    form: Mapped[JsonValue | None] = mapped_column(JSON)
    """An escalation's typed payload, as ``piloop escalate`` filed it: the campaign's ``[plugins.escalation]`` provider
    reads it, and ``body`` is its plain text. NULL for every other comment, and for an escalation filed as free text."""
    created_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=NOW)


class AgentSession(Tracked, Base):
    """An agent process in one of the roles. While ``state`` is ``requested``, the row is a request to start one.

    A request needs only ``kind`` and ``campaign_id``. ``harness`` and ``model`` pin the rotation, and ``reason`` says
    why. The daemon fills in the rest when it launches the row, so the table is also the launch queue of every role.
    """

    __tablename__ = 'agent_sessions'
    __table_args__ = (
        # the launch queue of the pass: the requested and active rows of a campaign for each role, and its latest row
        Index('ix_agent_sessions_campaign_kind_state', 'campaign_id', 'kind', 'state'),
        # the sessions of a subject: those of a run, behind its candidates (every kept-lineage row)
        Index('ix_agent_sessions_subject', 'subject_kind', 'subject_id'),
        CheckConstraint('(subject_kind IS NULL) = (subject_id IS NULL)', name='agent_sessions_subject'),
        *text_checks('agent_sessions', 'kind', nullable=('reason', 'harness', 'model')),
    )

    id: Mapped[AgentSessionId] = mapped_column(primary_key=True)
    kind: Mapped[RoleName]
    """The agent's role: a role of its campaign, a built-in one or a ``[plugins.role.<name>]`` table. A trigger refuses other
    names."""
    campaign_id: Mapped[CampaignId | None] = mapped_column(ForeignKey('campaigns.id', ondelete='CASCADE'))
    """The campaign the agent works for. piloop always sets it, but readers must accept NULL."""
    subject_kind: Mapped[TargetKind | None] = mapped_column(ForeignKey('target_kinds.id', name='agent_sessions_kind'))
    subject_id: Mapped[int | None]
    subject: Mapped[TargetRef | None] = composite('subject_kind', 'subject_id')
    """What the agent works on, such as ``run#12`` for an implementer. NULL for a sweep. No foreign key to the row: an
    agent session outlives the work that a plugin deletes."""
    harness: Mapped[str | None]
    model: Mapped[str | None]
    runtime: Mapped[str | None]
    """The runtime of the agent (``runtime`` in ``piloop.toml``): how to reach the process."""
    handle: Mapped[dict[str, object]] = mapped_column(default=dict, server_default=EMPTY_JSON)
    """The address of the process in its runtime. Each runtime defines its keys, for example a pid or a pane."""
    state: Mapped[AgentSessionState] = mapped_column(
        default=AgentSessionState.REQUESTED, server_default=AgentSessionState.REQUESTED.value
    )
    reason: Mapped[str | None]
    """Why it was requested: the trigger that fired, or what the requester wrote."""
    log_dir: Mapped[str | None]
    started_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=NOW)
    """The time of the request until the launch, then the time of the launch."""
    ended_at: Mapped[datetime | None]
    error: Mapped[str | None]
    session_id: Mapped[str | None]
    """The harness's own session id. piloop creates it at launch when the harness takes it as an argument
    (``{session_id}``), or the runtime reports it after the agent starts."""
    machine: Mapped[str | None]
    """The machine that runs the agent, as its executor names it (a worker of ``piloop worker``). NULL: the daemon's."""
    session_file: Mapped[Cid | None] = mapped_column(CidText)
    """The harness's own session file, as a chunked file in the CAS (``piloop cas get``): a snapshot a few seconds after
    the agent writes, at least every 30 s while it runs, and the whole file once it ended. NULL until the first
    snapshot, and for a harness without a usage spec."""
    usage: Mapped[Usage | None]
    """The tokens the agent spent, added up from the harness's own session file when the agent ended:
    ``{"input", "output", "cache_read", "cache_write", "total", "turns"}``. ``total`` is what the harness itself counts
    as the session's tokens (sum ``json_extract(usage, '$.total')`` for the spend). ``turns`` is the number of model
    responses. For every harness, ``input`` is the uncached input, and ``cache_read`` and ``cache_write`` the input
    read from and written to the prompt cache. NULL while the agent runs, or when no session file was found
    (``agent_session.usage_unknown``)."""


class Event(Base):
    """The append-only feed of what happened. The daemon pushes comments to implementers from it, reading from a cursor.

    It has two writers: ``piloop.db.events.emit``, for what only piloop knows, and the triggers of the change log
    (``piloop.db.triggers.changelog``), for what any connection does to the rows. The authorizer of ``piloop sql``
    denies any other insert. The payload is an ``EventModel``, with its ``kind``.
    """

    __tablename__ = 'events'
    __table_args__ = (
        Index('ix_events_target', 'target_kind', 'target_id', 'id'),
        # the asks of a kind and whether a newer one exists (``piloop.db.requests.pending``)
        Index('ix_events_kind', 'kind', 'target_kind', 'target_id', 'id'),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(default=utcnow, server_default=NOW)
    kind: Mapped[str] = mapped_column(Computed("json_extract(payload, '$.kind')"))
    """``<subject>.<what>``, for example ``run.started``, ``candidate.judged``, ``idea.status``, ``comment.decision``
    or ``idea.deleted``: the payload's ``kind``."""
    actor: Mapped[str]
    target_kind: Mapped[TargetKind] = mapped_column(ForeignKey('target_kinds.id', name='targetkind'))
    target_id: Mapped[int]
    target: Mapped[TargetRef] = composite('target_kind', 'target_id')
    payload: Mapped[EventModel]


_warned: set[tuple[str, str, str]] = set()


@event.listens_for(Base, 'load', propagate=True)
def _blob_placeholders(target: Base, _context: object) -> None:
    """Replace bytes in a text column (a BLOB that raw SQL stored) with a placeholder, and warn once.

    Agents write raw SQL, and one such row must not make every view of its table fail. The value is replaced in memory
    only, never written back. Once per process, stderr says which row it is and how to repair it. ``text_checks``
    keeps BLOBs out of the columns that agents write; this covers the other columns.
    """
    state = inspect(target)
    for attr in state.mapper.column_attrs:
        value, column = target.__dict__.get(attr.key), attr.columns[0]
        if not isinstance(value, bytes) or not isinstance(column, Column) or not isinstance(column.type, String):
            continue
        set_committed_value(target, attr.key, f'<not text: a {len(value)}-byte blob>')
        table, row = column.table.name, ', '.join(map(str, state.identity or ()))
        if (table, row, attr.key) not in _warned:
            _warned.add((table, row, attr.key))
            print(
                f'piloop: warning: {table}#{row} {attr.key} is a {len(value)}-byte blob, not text; shown as a'
                f' placeholder. Repair it: UPDATE {table} SET {attr.key} = CAST({attr.key} AS TEXT) WHERE id = {row}',
                file=sys.stderr,
            )


type Moved = Literal['Evaluator', 'Idea', 'IdeaLink', 'Run', 'Candidate', 'Measurement', 'Evaluation', 'Technique']
"""The tables that moved to the campaign type kit's components, which a plugin of 0.3 imported from here."""


@deprecated.until('0.5.0', 'the research tables moved to the components: delete this __getattr__ and Moved')
def __getattr__(name: Moved) -> type[Base]:
    """``from piloop.db.models import Candidate`` as in 0.3, with a warning: the table is a component's now."""
    if name not in get_args(Moved.__value__):  # the import system asks for other names too, such as __path__
        raise AttributeError(f'module {__name__!r} has no attribute {name!r}')
    from piloop.plugins.campaign_type.optimization import tables  # here: it imports this module; it has them all

    table: type[Base] = getattr(tables, name)
    warnings.warn(
        f'piloop.db.models.{name} moved: import it from {table.__module__}',
        PiloopDeprecationWarning,
        stacklevel=2,
    )
    return table
