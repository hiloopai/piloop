"""The role capability: what the agents of a role do, and the defaults of its ``[plugins.role.<name>]`` table.

Every role is a provider of ``ROLES``, a built-in one or a package's, and so are its defaults: a subclass of
``SweepRole`` or of ``ImplementerRole`` whose fields have the role's defaults. Its prompt is a resource of its package
(``PROMPT``). A table with another name defines a new role (``new_role``).
"""

import re
from collections.abc import Mapping
from datetime import timedelta
from enum import StrEnum
from importlib.resources import files
from importlib.resources.abc import Traversable
from string import Formatter
from typing import TYPE_CHECKING, ClassVar, Final, Literal, Protocol, Self

from pydantic import Field, field_validator, model_validator

from piloop.ids import ROLE_NAME, CommentKind
from piloop.plugins.harness.contract import HarnessKey, HarnessName
from piloop.plugins.host import Capability, Many, Parameters, Reach, Scope
from piloop.plugins.mcp_server.contract import McpServerName
from piloop.plugins.runtime.contract import RuntimeName
from piloop.plugins.section.contract import SectionName, Surface, defaults
from piloop.plugins.trigger.contract import Pass, TriggerName

if TYPE_CHECKING:  # piloop.db.models types campaigns.config with the roles: only the annotations name the rows
    from piloop.db.models import AgentSession


PUSHED: Final = CommentKind.MESSAGE
"""The comment kind that the daemon sends to a running agent: a message on it, or on its run or its run's idea. Every
other kind, a note included, is on the board for the agents that read it."""


class Files(StrEnum):
    """What the agents of a role may file with ``piloop idea``. The CLI refuses the rest."""

    NOTHING = 'nothing'
    """No ideas. A hunch goes in a note."""
    IDEAS = 'ideas'
    """Ideas for the board (``piloop idea add``)."""


type Verb = tuple[str, ...]
"""A ``piloop`` command by its words, such as ``('idea', 'add')``."""

RELAYED: frozenset[Verb] = frozenset(
    {
        ('sql',),
        ('escalate',),
        ('idea', 'add'),
        ('idea', 'claim'),
        ('idea', 'propose'),
        ('idea', 'status'),
        ('run', 'end'),
        ('candidate', 'import'),
        ('restore',),
        ('cas', 'get'),
        ('schema',),
    }
)
"""The commands that the prompts of the built-in roles name, besides the orchestrator's: what ``RoleConfig.RELAYED``
lets their agents run on a worker elsewhere."""


class Continuity(StrEnum):
    """How the agents of a sweeping role follow each other (``continuity``)."""

    FRESH = 'fresh'
    """Each agent starts a new harness session from its whole brief."""
    RESUME = 'resume'
    """Each agent continues the harness session of the role's last agent, from the CAS, on any machine, with only the
    new part of its brief. When that cannot work, it starts fresh, and ``agent_session.not_resumed`` says why."""


FIRST_MESSAGE = 'Your brief is in {brief}. Read all of it, in parts if a read comes back cut short, then follow it.'
"""An agent's first message, on its argv, by default.

It names the brief's file instead of holding the brief. The message is one argument, and Linux limits one argument to
128 KiB (``MAX_ARG_STRLEN``). A research brief with a long history is longer than that.
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
    IDEAS: ClassVar[str] = ''
    """The ideas line of a built-in role's brief when the line for its ``files`` does not fit it. ``{piloop}`` is the CLI
    and ``{campaign}`` is the campaign's name. Empty: the line for ``files`` (``piloop.agents.roles.IDEAS``)."""

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
    that only this role needs, for example how the research agent searches."""
    mcp: list[McpServerName] = []
    """The MCP servers that the agents of this role get, by the name of their ``[plugins.mcp_server.<name>]`` table.
    Every harness in the role's rotation must be able to take them in the invocation of the role's runtime."""
    writes: list[str] = []
    """What the agents of this role may write, as their brief states it. piloop enforces only a sweep's ``files``."""
    sections: list[SectionName] | None = None
    """What the briefs of this role show after ``brief.md``: the names of sections, in order, the ``context`` among them.
    When unset, the role's own guides, then every installed section that a brief of this kind of role shows by default,
    the guides first, in their order (``Section.SHOWN_IN``, ``Section.ORDER``)."""
    first_message: str = FIRST_MESSAGE
    """The first message of each agent of this role. ``{brief}`` is the path of its brief."""
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

    The run queue starts its agents. Each agent claims one idea, in a new checkout of the head, and its versions are
    scored and judged when it ends. A new role of this kind takes the implementer's defaults, its prompt included.
    """

    PROMPT: ClassVar[Traversable | None] = files(__package__) / 'prompts' / 'implementer.md'

    runs: Literal[True] = True
    """The agents of this role do runs. A table without ``runs = true`` is a sweeping role."""
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
    max_concurrent: int = Field(default=4, ge=1)
    """The maximum number of implementers that run at the same time, on all workers together. A run that is being
    scored after its implementer ended does not count."""
    writes: list[str] = ['versions', 'comments(note) on your idea or your run']
    """What an implementer may write, as its brief states it. It files no ideas: its hunches go in its
    notes, which research reads."""
    SURFACE: ClassVar[Surface] = Surface.RUN


class Sweep(Pass, Protocol):
    """What a sweeping role reads to give its agent its context: its trigger's ``Pass``, and the agent's row."""

    @property
    def agent_session(self) -> AgentSession:
        """The row of the agent that the brief is for."""
        ...


class SweepRole(RoleConfig):
    """``[plugins.role.<name>]`` of a sweeping role: the watchdog, research, or a new role that you define.

    A sweep is an agent without a run. Its triggers request it, and nothing is scored when it ends. A table with a new
    name defines a new role, and needs ``prompt`` and ``triggers``.
    """

    INCIDENT_PROMPT: ClassVar[Traversable | None] = None
    """The role's own ``incident_prompt``: a resource of its package, or None."""
    ROUTINE_PROMPT: ClassVar[Traversable | None] = None
    """The role's own ``routine_prompt``: a resource of its package, or None."""

    runs: Literal[False] = False
    """The agents of this role do no runs. Set ``runs = true`` in a new table to define a role that does runs."""
    triggers: list[TriggerName]
    """When the daemon requests an agent of this role: the names of triggers (``[plugins.trigger.<name>]``), checked in
    order on each pass. A package can add triggers. An empty list means that agents start only on request
    (``piloop launch``)."""
    interval: float = Field(default=0, ge=0)
    """The number of seconds between two routine sweeps of the ``cron`` trigger, counted from the start of the last
    one. A sweep that investigates an anomaly is not a routine sweep. 0 turns the timer off."""
    files: Files = Files.NOTHING
    """What the agents of this role may file with ``piloop idea``: ``nothing`` or ``ideas``. The CLI refuses the
    rest."""
    plan_words: int = Field(default=150, ge=1)
    """The most words in the plan of an idea that an agent of this role adds or develops: steps that one run can
    follow, not a protocol. A longer plan is refused."""
    max_ideas: int = Field(default=1, ge=0)
    """The maximum number of new ideas one sweep may insert, as its brief states it. ``low_water`` requests enough
    sweeps to fill the backlog at this many ideas each. 0 turns ``low_water`` off for the role."""
    continuity: Continuity = Continuity.FRESH
    """``fresh``: each agent starts a new harness session. ``resume``: each agent continues the session of the role's
    last agent, written back from the CAS, so it remembers what the agents before it did. The harness needs
    ``resume``, and a provider that restores its session files (``Harness.restore``): every built-in one does."""
    incident_prompt: str | None = None
    """A file that follows ``prompt`` in the brief of an incident sweep, a sweep that got incidents from a
    trigger that investigates, such as ``run_failed``: a path relative to the repo root, or an absolute path. When unset, the role's own (the watchdog's
    ``watchdog_incident.md``), else nothing."""
    routine_prompt: str | None = None
    """A file that follows ``prompt`` in the brief of every other sweep. When unset, the role's own (the watchdog's
    ``watchdog_progress.md``), else nothing."""
    SURFACE: ClassVar[Surface] = Surface.SWEEP

    def context(self, sweep: Sweep) -> list[str]:
        """The lines that the role adds to its agent's context, after the daemon's own and before the sections.

        By default, a role that defines ideas (``files = "ideas"``) is told its ``max_ideas``, which ``low_water``
        counts on, and its ``plan_words``. Any other role adds nothing.
        """
        if self.files is Files.IDEAS:
            return [
                f'- New ideas of your own in this sweep: at most {self.max_ideas} (`max_ideas`), each with a plan of'
                f' at most {self.plan_words} words (`plan_words`).'
            ]
        return []

    @model_validator(mode='after')
    def _check(self) -> Self:
        if self.prompt is None and self.PROMPT is None:
            raise ValueError('a new role needs a prompt: the file that starts its brief')
        return self


RESERVED_NAMES = frozenset({'run', 'orchestrator', 'human', 'daemon'})
"""Names that already sign writes: a run, and the actors that are not roles."""


def new_role(name: str, table: Mapping[str, object]) -> type[ImplementerRole] | type[SweepRole]:
    """The class of a ``[plugins.role.<name>]`` table with a new name: with ``runs = true`` a role that does runs.

    Raises:
        ValueError: ``name`` is no role name, or one that signs writes already.
    """
    if not re.fullmatch(ROLE_NAME, name) or name in RESERVED_NAMES:
        raise ValueError(
            f'{name!r} is not a role name: use lowercase letters, digits, _ and - ({ROLE_NAME}), and none of'
            f' {sorted(RESERVED_NAMES)}'
        )
    return ImplementerRole if table.get('runs') else SweepRole


ROLES = Capability('role', RoleConfig, table=Many(Scope.CAMPAIGN, new=new_role, reach=Reach.EVERY))
"""Every role by name, with its defaults: the built-in ones, then the ones that packages add. Each entry point of the
group names a subclass of ``SweepRole`` or of ``ImplementerRole``, whose defaults are the role's."""
