"""The triggers of the roles: the conditions that make the daemon request an agent (https://piloop.autoresearch.inc/concepts/roles#triggers).

A trigger is a provider of ``TRIGGERS``: its fields are its parameters, from ``[plugins.trigger.<name>]``. A role names
its triggers in ``[plugins.role.<name>] triggers``. On each
**pass** over the campaigns, for each campaign and each sweeping role without a pending request, the scheduler calls
``Trigger.fire`` of each trigger of the role, in the order of the list. A package adds a trigger with an entry point in
the group of ``TRIGGERS``.

The daemon makes a pass when an event of a campaign's state arrives, or of a kind that a trigger reads
(``WAKES_ON``), and when the time comes that a trigger asked for (``due_in``). It does not make one on a timer.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, ClassVar, Protocol

from piloop.plugins.host import Capability, Many, Parameters, Scope

if TYPE_CHECKING:  # piloop.db.models types campaigns.config with the triggers: only the annotations name the rows
    from sqlalchemy.orm import Session

    from piloop.db.models import AgentSession, Campaign, EventModel


class Quiet(StrEnum):
    """A phase in which the triggers of a campaign do not fire, unless a trigger names it (``Trigger.WHILE``)."""

    PAUSED = 'paused'
    """``piloop pause``."""
    HELD = 'held'
    """The stop rule holds the campaign's launches, such as at a spend limit."""
    STOPPED = 'stopped'
    """The campaign stopped, and the daemon closed it."""


class Pass(Protocol):
    """What a trigger reads to decide. The scheduler makes one for each sweeping role of each campaign on each pass.

    A trigger reads with ``s`` and writes nothing. "The role's rows" are the ``agent_sessions`` rows of the role in the
    campaign.
    """

    @property
    def s(self) -> Session:
        """The pass's session."""
        ...

    @property
    def now(self) -> datetime:
        """The time of the pass. A trigger that compares a time reads this, not the clock."""
        ...

    @property
    def campaign(self) -> Campaign:
        """The campaign of the pass."""
        ...

    @property
    def role(self) -> str:
        """The name of the role: its ``[plugins.role.<name>]`` table and ``agent_sessions.kind``."""
        ...

    @property
    def last(self) -> AgentSession | None:
        """The role's latest row. None before the first."""
        ...

    @property
    def quiet(self) -> Quiet | None:
        """The quiet phase of the campaign in which this pass checks the trigger (``Trigger.WHILE``). None: it runs."""
        ...

    def live(self) -> int:
        """The number of the role's rows that are starting or running."""
        ...


@dataclass(frozen=True)
class Fire:
    """A trigger that fired: the reason for the request, and what the requested row gets."""

    reason: str
    """The ``agent_sessions.reason`` of the request. The agent's brief shows it."""
    count: int = 1
    """The number of rows to request. When several triggers fire together, the largest count wins."""
    alone: bool = False
    """The row is for the fires that change what the agent does, such as the incidents that it investigates.

    When the first fire of the pass is alone, the row is for every fire of the pass that is alone. Otherwise the fires
    that are alone wait for a later pass.
    """
    events: tuple[EventModel, ...] = ()
    """The events to emit on the first requested row."""


@dataclass(frozen=True)
class Steer:
    """A note for the role's agent: on its newest open row, or else on a row that the daemon requests for it.

    An open row is requested, starting or running. An agent gets the notes written on its row before it starts in its
    brief, and the later ones as it works: in its turn through its harness's hook, or else when its turn ends. The note
    makes another pass, so a trigger steers only for what its earlier notes did not take.
    """

    text: str
    reason: str
    """The ``agent_sessions.reason`` of the row that the daemon requests when the role has none open."""
    events: tuple[EventModel, ...] = ()
    """The events to emit on the note. Whether the agent got it is its ``comment.delivered`` on the agent."""


class Trigger(Parameters, ABC):
    """``[plugins.trigger.<name>]``: a trigger, which a role names in its ``triggers``.

    The daemon checks a role's triggers in the order of its list.
    """

    WHILE: ClassVar[frozenset[Quiet]] = frozenset()
    """The quiet phases in which the daemon still checks the trigger, and launches what it requests. A trigger that
    names ``stopped`` is checked on every pass after the daemon closed the stopped campaign."""
    WAKES_ON: ClassVar[frozenset[type[EventModel]]] = frozenset()
    """The event models that make the daemon check the trigger, beyond those of its campaign's state: its ideas,
    runs, candidates and agents, its status and config. A trigger that reads anything else, such as the events of a
    plugin, names them here."""

    @abstractmethod
    def fire(self, p: Pass) -> Fire | Steer | None:
        """Return the request that the trigger makes now, the note that it writes (``Steer``), or None."""

    @abstractmethod
    def due_in(self, p: Pass) -> timedelta | None:
        """How long until ``fire`` can answer otherwise with time alone, or None when only events change its answer.

        The daemon makes a pass then. A trigger that compares a time with ``p.now`` returns when that comparison
        turns; without it, the trigger is checked again only on the next event.
        """


TRIGGERS = Capability('trigger', Trigger, table=Many(Scope.CAMPAIGN))
"""Every trigger by name: ``[plugins.trigger.<name>]``, and each provider that needs no keys."""

TriggerName = Annotated[str, TRIGGERS.ref]
"""The name of a trigger of the campaign, in a role's ``triggers``."""
