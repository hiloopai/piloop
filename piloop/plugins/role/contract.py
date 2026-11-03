"""The role capability: what the agents of a role do, and the defaults of its ``[plugins.role.<name>]`` table.

Every role is a provider of ``ROLES``, a built-in one or a package's, and so are its defaults: a subclass of
``ImplementerRole``, whose agents do runs, or of ``StartsItsAgents``, whose provider starts its agents itself. Its
fields have the role's defaults, and its prompt is a resource of its package (``PROMPT``). A table with another name and
``runs = true`` defines a new role that does runs (``new_role``).
"""

import re
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from importlib.resources import files
from importlib.resources.abc import Traversable
from string import Formatter
from typing import TYPE_CHECKING, ClassVar, Final, Literal

from pydantic import Field, field_validator

from piloop.ids import ROLE_NAME, AgentSessionId, CommentKind, RoleName
from piloop.plugins.harness.contract import HarnessKey, HarnessName
from piloop.plugins.host import Capability, Many, Parameters, Reach, Scope
from piloop.plugins.mcp_server.contract import McpServerName
from piloop.plugins.runtime.contract import RuntimeName
from piloop.plugins.section.contract import SectionName, Surface, defaults

if TYPE_CHECKING:  # piloop.db.models types campaigns.config with the roles: only the annotations name the rows
    from sqlalchemy.orm import Session

    from piloop.db.models import Campaign, EventModel
    from piloop.otel import Tracer
    from piloop.plugins.contribution import DaemonHost


PUSHED: Final = CommentKind.MESSAGE
"""The comment kind that the daemon sends to a running agent: a message on it, or on its run or its run's idea. Every
other kind, a note included, is on the board for the agents that read it."""


type Verb = tuple[str, ...]
"""A ``piloop`` command by its words, such as ``('idea', 'add')``."""

RELAYED: frozenset[Verb] = frozenset(
    {
        ('sql',),
        ('escalate',),
        ('idea', 'add'),
        ('idea', 'claim'),
        ('idea', 'propose'),
        ('run', 'end'),
        ('candidate', 'import'),
        ('restore',),
        ('cas', 'get'),
        ('schema',),
        ('submit',),
    }
)
"""The commands that the prompts of the built-in roles name, besides the orchestrator's: what ``RoleConfig.RELAYED``
lets their agents run on a worker elsewhere."""


class Continuity(StrEnum):
    """How the agents of a role follow each other (``continuity``)."""

    FRESH = 'fresh'
    """Each agent starts a new harness session from its whole brief."""
    RESUME = 'resume'
    """Each agent continues the harness session of the role's last agent, from the CAS, on any machine, with only the
    new part of its brief. When that cannot work, it starts fresh, and ``agent_session.not_resumed`` says why."""


FIRST_MESSAGE = 'Your brief is in {brief}. Read all of it, in parts if a read comes back cut short, then follow it.'
"""An agent's first message, on its argv, by default.

It names the brief's file instead of holding the brief. The message is one argument, and Linux limits one argument to
128 KiB (``MAX_ARG_STRLEN``). A brief with a long history is longer than that.
"""
RESUME_MESSAGE = (
    'You continue this session as a new agent, {agent}. Your earlier briefs and work are above.'
    ' Follow your role as before, with this new context.'
)
"""What starts a resumed agent's brief, by default."""


def _filled(message: str, field: str) -> set[str]:
    """The fields in braces of ``message``.

    Raises:
        ValueError: One is not ``field``.
    """
    fields = {name for _, name, _, _ in Formatter().parse(message) if name is not None}
    if others := fields - {field}:
        raise ValueError(f'{message!r} has {", ".join(sorted(others))}: only {{{field}}} is filled in')
    return fields


class RoleConfig(Parameters):
    """The keys that every ``[plugins.role.<name>]`` table accepts. Each role's table below adds keys and sets defaults."""

    PROMPT: ClassVar[Traversable | None] = None
    """The prompt that starts the brief of the role's agents: a resource of the role's package, such as
    ``files(__package__) / 'auditor.md'``. ``prompt`` in the table overrides it. A role with neither is refused."""
    RELAYED: ClassVar[frozenset[Verb]] = RELAYED
    """The ``piloop`` commands that an agent of the role on a worker elsewhere may run: those that its prompt names. The
    worker relays them to the server (``piloop.plugins.executor.kit.relay``), and refuses the rest."""
    IDEAS: ClassVar[str] = (
        '- A hunch worth a run of its own goes in a note, with pointers (ids, paths, the number). You file no ideas.'
    )
    """The ideas line of the role's brief: how its agents give work to the implementers. ``{piloop}`` is the CLI and
    ``{campaign}`` is the campaign's name."""

    prompt: str | None = None
    """The file that starts the brief of this role: a path relative to the repo root, or an absolute path. When unset,
    the role's own prompt (``PROMPT``). A new role needs it."""
    runtime: RuntimeName | None = None
    """Where the agents of this role run. When unset, the role uses the campaign's, ``[plugins.runtime] use``."""
    harnesses: list[HarnessName] | None = Field(default=None, min_length=1)
    """The harness rotation of this role. When unset, the role uses the campaign's, ``[plugins.harness] use``."""
    model: dict[HarnessKey, str] | None = None
    """The model for each harness in this role, over the ``model`` of each ``[plugins.harness.<name>]``."""
    effort: str = 'high'
    """How hard the agents of this role think, as the harness names it, for example ``high``. It fills ``{effort}`` in
    the harness's argv, and the harness checks it."""
    timeout: float = 600
    """The number of seconds one agent may run before it is stopped."""
    max_concurrent: int = Field(default=1, ge=1)
    """The maximum number of agents of this role that run at the same time. More requests stay ``requested`` until a
    slot is free."""
    addendum: str | None = None
    """A repo-relative file that is appended to ``brief.md`` in the briefs of this role only. Use it for instructions
    that only this role needs, for example how the orchestrator reads a loss table."""
    mcp: list[McpServerName] = []
    """The MCP servers that the agents of this role get, by the name of their ``[plugins.mcp_server.<name>]`` table.
    Every harness in the role's rotation must be able to take them in the invocation of the role's runtime."""
    writes: list[str] = []
    """What the agents of this role may write, as their brief states it. piloop enforces only that an agent of a role
    that does runs adds no ideas for others."""
    sections: list[SectionName] | None = None
    """What the briefs of this role show after ``brief.md``: the names of sections, in order, the ``context`` among them.
    When unset, the role's own guides, then every installed section that a brief of this kind of role shows by default,
    the guides first, in their order (``Section.SHOWN_IN``, ``Section.ORDER``)."""
    first_message: str = FIRST_MESSAGE
    """The first message of each agent of this role. ``{brief}`` is the path of its brief."""
    continuity: Continuity = Continuity.FRESH
    """``fresh``: each agent starts a new harness session. ``resume``: each agent continues the session of the role's
    last agent, written back from the CAS, so it remembers what the agents before it did. The harness needs
    ``resume``, and a provider that restores its session files (``Harness.restore``): every built-in one does. In a
    role that does runs, each run's agent continues the last run's session, and claims its own idea."""
    resume_message: str = RESUME_MESSAGE
    """What starts the brief of an agent that resumes the session of the role's last agent. ``{agent}`` is its
    signature."""

    @field_validator('first_message')
    @classmethod
    def _names_the_brief(cls, message: str) -> str:
        if not _filled(message, 'brief'):
            raise ValueError(f'{message!r} names no {{brief}}')
        return message

    @field_validator('resume_message')
    @classmethod
    def _names_the_agent(cls, message: str) -> str:
        _filled(message, 'agent')
        return message

    SURFACE: ClassVar[Surface]
    """Where the briefs of this kind of role show sections: the default of ``sections``."""
    GUIDES: ClassVar[tuple[str, ...]] = ('database',)
    """The guides that the role's agents use (``Guide``), in order: the head of ``sections`` when it is unset. A role
    that does more extends its base's list. The others are a ``piloop schema <guide>`` away."""

    def shown(self) -> list[str]:
        """The names of the sections of this role's briefs: its ``sections``, else its guides and its surface's."""
        if self.sections is not None:
            return self.sections
        return list(dict.fromkeys([*self.GUIDES, *defaults(self.SURFACE)]))


class ImplementerRole(RoleConfig):
    """``[plugins.role.implementer]``, and every ``[plugins.role.<name>]`` table with ``runs = true``: a role that does runs.

    The run queue starts its agents. Each agent claims one idea, in a new checkout of the best (or of another
    candidate, ``--base``), and its versions are scored into the pool. A new role of this kind takes the implementer's defaults, its prompt included.
    """

    PROMPT: ClassVar[Traversable | None] = files(__package__) / 'prompts' / 'implementer.md'

    runs: Literal[True] = True
    """The agents of this role do runs. A table of a new name needs ``runs = true``."""
    timeout: float = 1800
    """The number of seconds one implementer may run before it is stopped. Its brief states it. A quarter of it before,
    at most ``wrap_up_lead``, the daemon tells the implementer to wrap up."""
    wrap_up_lead: timedelta = Field(default=timedelta(minutes=10), ge=timedelta(0))
    """How long before its ``timeout`` the daemon tells an implementer to wrap up, at most a quarter of the timeout."""
    end_grace: timedelta = Field(default=timedelta(seconds=60), ge=timedelta(0))
    """How long after an implementer ended its run (``piloop run end``) the daemon ends it, and every process that it
    left: its last tool call returns first."""
    startup_relaunches: int = Field(default=2, ge=0)
    """How many times a run is queued again with a new implementer after its implementer failed to start. Then the run
    fails. A failure at startup is a fault of the host or the harness, not an attempt at an idea."""
    max_concurrent: int = Field(default=1, ge=1)
    """The maximum number of implementers that run at the same time, on all workers together. A run that is being
    scored after its implementer ended does not count. More of them spend more to finish sooner, not more per dollar:
    they often work on the same best."""
    IDEAS: ClassVar[str] = '- A hunch for another run goes in a note, with its numbers.'
    SURFACE: ClassVar[Surface] = Surface.RUN
    GUIDES: ClassVar[tuple[str, ...]] = ()
    """None: the context says how to run piloop and its SQL, and ``piloop schema database`` prints the rest."""


class Quiet(StrEnum):
    """A phase in which a campaign launches no agent, but those of a role that wakes in it (``wakes_while``)."""

    PAUSED = 'paused'
    """``piloop pause``."""
    HELD = 'held'
    """The stop rule holds the campaign's launches, such as at a spend limit."""
    STOPPED = 'stopped'
    """The campaign stopped, and the daemon closed it."""


@dataclass(frozen=True)
class Wake:
    """What a role that starts its agents reads on a pass over one campaign (``StartsItsAgents.request``)."""

    s: Session
    """The pass's session: the role inserts its requested rows (``agent_sessions``) with it. It does not commit."""
    now: datetime
    """The time of the pass. A role that compares a time reads this, not the clock."""
    campaign: Campaign
    role: RoleName
    """The role's name: its ``[plugins.role.<name>]`` table and the ``agent_sessions.kind`` of its rows."""
    quiet: Quiet | None
    """The campaign's quiet phase, for a role that wakes in it (``wakes_while``). None while the campaign runs."""
    tracer: Tracer


class StartsItsAgents(RoleConfig, ABC):
    """``[plugins.role.<name>]`` of a role that does no runs: its provider decides when its agents start, and how.

    On each pass over a campaign the daemon asks it for requests (``request``), and launches the requested rows of the
    role as it launches every row: as far as its ``max_concurrent`` and the executor's slots allow. Each launch runs in
    a thread of the daemon (``launch``). Anyone can request a row of the role by hand too (``piloop launch``).
    """

    runs: Literal[False] = False
    """The agents of this role do no runs."""

    @classmethod
    @abstractmethod
    def wakes_on(cls) -> frozenset[type[EventModel]]:
        """The event models, beyond the campaign's state, that make the daemon pass over the campaigns for the role."""

    @abstractmethod
    def request(self, wake: Wake) -> datetime | None:
        """Insert the requested rows of the role for ``wake``'s campaign, if any, and say when to ask again.

        None: only the next event of the campaign changes the answer.
        """

    @abstractmethod
    def wakes_while(self, quiet: Quiet) -> bool:
        """Whether the role is asked for requests, and its rows launch, while the campaign is ``quiet``."""

    @abstractmethod
    def starts_by_itself(self) -> bool:
        """Whether the role requests agents of its own accord. False: its agents start only by hand."""

    @abstractmethod
    def launch(self, host: DaemonHost, agent_session_id: AgentSessionId) -> None:
        """Launch the requested row ``agent_session_id`` of the role, in a thread of the daemon, and wait for its end."""


RESERVED_NAMES = frozenset({'run', 'orchestrator', 'human', 'daemon'})
"""Names that already sign writes: a run, and the actors that are not roles."""


def new_role(name: str, table: Mapping[str, object]) -> type[ImplementerRole]:
    """The class of a ``[plugins.role.<name>]`` table with a new name: a role that does runs (``runs = true``).

    Raises:
        ValueError: ``name`` is no role name, or one that signs writes already, or the table does no runs.
    """
    if not re.fullmatch(ROLE_NAME, name) or name in RESERVED_NAMES:
        raise ValueError(
            f'{name!r} is not a role name: use lowercase letters, digits, _ and - ({ROLE_NAME}), and none of'
            f' {sorted(RESERVED_NAMES)}'
        )
    if not table.get('runs'):
        raise ValueError(f'a role of your own does runs: set runs = true in [plugins.role.{name}]')
    return ImplementerRole


ROLES = Capability('role', RoleConfig, table=Many(Scope.CAMPAIGN, new=new_role, reach=Reach.EVERY))
"""Every role by name, with its defaults: the built-in ones, then the ones that packages add. Each entry point of the
group names a subclass of ``ImplementerRole`` or of ``StartsItsAgents``, whose defaults are the role's."""
