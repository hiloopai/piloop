"""What the API's routes and its client share: who may call, the models of a call, the bounds, and why a route refuses.

The routes are FastAPI's (``piloop.daemon.api``). The client (``piloop.api.client``) sends these query models and
reads the answers of ``piloop.api.views``: one set of models on both sides.
"""

from collections.abc import Mapping, Sequence
from enum import StrEnum
from pathlib import PurePath
from typing import Annotated, Protocol

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    JsonValue,
    PlainSerializer,
    SecretStr,
    Tag,
    model_validator,
)
from pydantic_core import PydanticCustomError

from piloop import deprecated
from piloop.actor import CALLER_NAME
from piloop.ids import (
    LISTED,
    CampaignName,
    CampaignStatus,
    CommentId,
    CommentKind,
    Ending,
    EnvName,
    RoleName,
    TargetKind,
)

MAX_POLL_SECONDS = 25
"""The longest that the server holds a long-poll (``Polled.wait``), as the GitHub Actions runner's."""
LIMIT = 100
"""How many events a read returns at most, by default."""
MAX_LIMIT = 1000


class Access(StrEnum):
    """What a caller of the server may do. A token has one (``piloop token create --access``)."""

    READ = 'read'
    """Every read: the views and the feed."""
    ACT = 'act'
    """``read``, and every operation on a campaign."""
    ADMIN = 'admin'
    """``act``, and the home's own settings. The owner on the Unix socket has it."""
    WORKER = 'worker'
    """A worker's calls: the executor's routes and the CAS."""


GRANTS: Mapping[Access, frozenset[Access]] = {
    Access.READ: frozenset({Access.READ}),
    Access.ACT: frozenset({Access.READ, Access.ACT}),
    Access.ADMIN: frozenset({Access.READ, Access.ACT, Access.ADMIN}),
    Access.WORKER: frozenset({Access.WORKER}),
}
"""The access of the routes that a caller of each access may call."""


type Revealed = Annotated[SecretStr, PlainSerializer(SecretStr.get_secret_value, when_used='json')]
"""A secret in a body or an answer: its JSON carries the value, which pydantic writes as stars otherwise. Its repr
hides it."""


class Params(BaseModel):
    """The parameters of an endpoint."""

    model_config = ConfigDict(frozen=True, use_attribute_docstrings=True)


class Polled(Params):
    """The parameters of a long-poll: the server holds it up to ``wait`` seconds for an answer."""

    wait: float = Field(0, ge=0, le=MAX_POLL_SECONDS)
    """Seconds to hold the request while there is nothing to answer yet."""


class Located(Params):
    """A campaign by its name or a path, from where the client runs."""

    campaign: str | None = None
    """The campaign's name, its id, or a path inside its repo."""
    cwd: str | None = None
    """Where the client runs, as an absolute path: a relative path in ``campaign`` is from there."""


class Selection(Located):
    """Which campaigns: the one that ``campaign`` names, else the one that ``cwd`` is in, else those of ``status``."""

    status: tuple[CampaignStatus, ...] = LISTED
    """The statuses of the campaigns to list when no campaign is named. By default all but ``retired``."""


class Stated(Protocol):
    """A campaign with its status: a row, or a ``CampaignState``."""

    @property
    def status(self) -> CampaignStatus:
        """Whether the daemon still requests work for the campaign."""
        ...


def defaulted[C: Stated](campaigns: Sequence[C]) -> list[C]:
    """What a command shows of a selection that names no campaign: the only active campaign, else every campaign."""
    active = [c for c in campaigns if c.status is CampaignStatus.ACTIVE]
    return active if len(active) == 1 else list(campaigns)


class InboxQuery(Located):
    """Which inbox items: those of ``campaign``, or of every campaign."""

    kind: tuple[CommentKind, ...] = ()
    """Only the items of these kinds."""
    acked: bool = False
    """The acknowledged items too."""


class CommentsQuery(Params):
    """Which comments: those on one row."""

    kind: TargetKind
    """The kind of the row, such as ``idea``."""
    id: int = Field(ge=1)
    """The row's id."""


class OutputQuery(Params):
    """How much of an agent's output."""

    lines: int = Field(ge=1, le=MAX_LIMIT)
    """The most lines to answer: the last ones."""


class EventsQuery(Polled, Located):
    """Which events: those after a cursor, oldest first, about rows of ``campaign`` or of every campaign."""

    after: int = Field(0, ge=0)
    """The cursor to read after: the ``next`` of the last read, or the ``cursor`` of a snapshot."""
    kind: tuple[str, ...] = ()
    """Only the events of these kinds."""
    limit: int = Field(LIMIT, ge=1, le=MAX_LIMIT)
    """The most events to answer."""


class NewsQuery(Polled, Located):
    """What needs the orchestrator: the news after a cursor, of ``campaign`` or of every campaign."""

    after: int | None = Field(None, ge=0)
    """The cursor to read after: the ``next`` of the last wait. Default: now."""


class Problem(StrEnum):
    """Why the API refuses a request: the ``error`` of its body."""

    UNKNOWN_CAMPAIGN = 'unknown_campaign'
    UNKNOWN_COMMENT = 'unknown_comment'
    UNKNOWN_IDEA = 'unknown_idea'
    UNKNOWN_CANDIDATE = 'unknown_candidate'
    UNKNOWN_AGENT = 'unknown_agent'
    UNKNOWN_RUN = 'unknown_run'
    UNKNOWN_LIMIT = 'unknown_limit'
    UNKNOWN_TOKEN = 'unknown_token'
    UNKNOWN_SECRET = 'unknown_secret'
    CURSOR_AHEAD = 'cursor_ahead'
    REFUSED = 'refused'
    """An operation refused its input and changed nothing. The detail says why."""
    STILL_STOPPING = 'still_stopping'
    """A retire refused a campaign whose stop is not over. The detail names its open agents: retire it again later."""
    FORBIDDEN = 'forbidden'
    """The caller may not act as the actor that it asks for (``piloop.net.acting``). The detail says why."""
    BUSY = 'busy'
    """Another connection held the database's write lock for the whole busy timeout: send it again after Retry-After."""


PRIORITIES = range(5)
"""The priorities, from 0 (urgent) to 4 (someday)."""
type Priority = Annotated[int, Field(ge=PRIORITIES.start, le=PRIORITIES.stop - 1)]
"""An inbox item's or an idea's priority: from 0 (urgent) to 4 (someday)."""


class PauseBody(Params):
    """How to pause a campaign."""

    reason: str
    """Why, with ids: what the pause waits for."""
    evals: bool = False
    """Hold the measurements that the daemon starts too: after a new evaluator, or asked."""


class StopBody(Params):
    """How to stop a campaign."""

    reason: str = ''
    """Why, for ``stopped_reason`` and the closing review."""
    ending: Ending = Ending.WRAP_UP
    """How the running implementers end. ``now`` also ends those of a stopped campaign that still wind down."""


class ReopenBody(Params):
    """Why a stopped campaign is set active again."""

    reason: str
    """Why, with ids: it goes into the ``campaign.reopened`` event."""


class RetireBody(Params):
    """Why a campaign is retired."""

    reason: str
    """Why, with ids: it goes into a note on the campaign."""


class Reasoned(Params):
    """A change to an idea, with why: it goes into the event or a note on the idea."""

    reason: str


class AckBody(Params):
    """The inbox items to acknowledge."""

    ids: tuple[CommentId, ...]


class ReplyBody(Params):
    """An answer to an inbox item: a ``decision`` on the root of its thread."""

    body: str
    priority: Priority = 2


class MessageBody(Params):
    """A word to a campaign's orchestrator: a ``message`` on the campaign, which it answers with a reply."""

    body: str
    priority: Priority = 2


class LimitBody(Reasoned):
    """A value for a live key, with why: it shows next to the value."""

    value: JsonValue


class LaunchBody(Reasoned):
    """One more agent of a sweeping role (``[plugins.role.<name>]``). The agent's brief includes the reason."""

    role: RoleName
    harness: str | None = None
    """The harness to use instead of the role's."""
    model: str | None = None
    """The model id, as the harness names it."""


class PromptBody(Polled):
    """A message for the next turn of a running agent. With wait, the answer waits for what came of it."""

    text: str


class CancelBody(Params):
    """How to cancel a run or an agent."""

    ending: Ending = Ending.NOW
    """How its running agents end: at once, or after they were told to save their work and stop."""


class TokenBody(Params):
    """A bearer token to make for a caller."""

    name: Annotated[str, Field(pattern=CALLER_NAME)]
    """The name of the caller that uses it, such as a worker. No ``#``: that names a launched agent's token."""
    access: Access


class SecretBody(Params):
    """The value of a secret, in place of any."""

    value: Revealed


def _absolute(path: str) -> str:
    if not PurePath(path).is_absolute():
        raise PydanticCustomError('absolute_path', 'an absolute path, not {path}', {'path': path})
    return path


class Checkout(Params):
    """A checkout on the daemon's machine, registered where it is."""

    model_config = ConfigDict(extra='forbid')

    path: Annotated[str, AfterValidator(_absolute)]
    name: CampaignName | None = None
    """The campaign's name. None: the ``name`` of its ``piloop.toml``."""


class SecretCredential(Params):
    """The home's secret that holds the credential, read when the call uses it."""

    model_config = ConfigDict(extra='forbid')

    secret: EnvName


class TokenCredential(Params):
    """A credential that the call carries, such as a short-lived token: its git command uses it, and nothing keeps it."""

    model_config = ConfigDict(extra='forbid')

    token: Revealed


def _credential_kind(value: object) -> str | None:
    """Which of a ``Credential``'s models a body is, by its key. Both keys are neither."""
    match value:
        case {'secret': _, 'token': _}:
            return None
        case SecretCredential() | {'secret': _}:
            return 'secret'
        case TokenCredential() | {'token': _}:
            return 'token'
        case _:
            return None


type Credential = Annotated[
    Annotated[SecretCredential, Tag('secret')] | Annotated[TokenCredential, Tag('token')],
    Discriminator(_credential_kind),
]
"""What the host of a repository asks for: exactly one of ``{"secret": "<home secret>"}`` and ``{"token": "..."}``."""


@deprecated.until('0.4.0', 'the secret of a clone is its credential: delete secret_is_credential and its validator')
def secret_is_credential(data: object) -> object:
    """Return ``data``, a body, with its ``secret`` as its ``credential``. A body that has both keeps both: it is refused."""
    match data:
        case {'secret': secret, **rest} if 'credential' not in rest:
            return {**rest, 'credential': {'secret': secret}}
        case _:
            return data


class Remote(Params):
    """A repository to clone, and the credential that its host asks for."""

    model_config = ConfigDict(extra='forbid')

    url: str
    credential: Credential | None = None
    """What the host of ``url`` asks for: a home secret, or a token for this clone alone. None: nothing."""

    @model_validator(mode='before')
    @classmethod
    def _secret(cls, data: object) -> object:
        return secret_is_credential(data)


class SyncBody(Params):
    """How to read a campaign's ``piloop.toml`` again."""

    model_config = ConfigDict(extra='forbid')

    credential: Credential | None = None
    """What the host of a cloned campaign's repository asks for, as for ``Remote``. None: nothing."""


class Clone(Params):
    """A repository that the daemon clones under its home, then registers."""

    model_config = ConfigDict(extra='forbid')

    clone: Remote
    name: CampaignName | None = None
    """The campaign's name. None: the ``name`` of its ``piloop.toml``."""


class Fork(Params):
    """A new campaign that starts from a version of another one, in a clone of that one's checkout."""

    model_config = ConfigDict(extra='forbid')

    fork: str
    """The source campaign, in any state: its name, or its id."""
    at: str | None = None
    """The version to start from, as the source's vcs names one: a revision, a branch, a tag. None: its head."""
    name: CampaignName | None = None
    """The new campaign's name. None: ``<source>-<n>``, with the lowest ``n`` from 2 that is free."""


def _source(value: object) -> str | None:
    """Which of a ``Registration``'s models a body is, by its key."""
    match value:
        case Checkout() | {'path': _}:
            return 'path'
        case Clone() | {'clone': _}:
            return 'clone'
        case Fork() | {'fork': _}:
            return 'fork'
        case _:
            return None


type Registration = Annotated[
    Annotated[Checkout, Tag('path')] | Annotated[Clone, Tag('clone')] | Annotated[Fork, Tag('fork')],
    Discriminator(_source),
]
"""What to register as a campaign: exactly one of ``{"path": ...}``, ``{"clone": {...}}`` and ``{"fork": ...}``."""
