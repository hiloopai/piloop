"""The liveness capability: which stuck agents the daemon ends (``[plugins.liveness]`` in ``piloop.toml``).

On each wind-down pass the daemon gives the campaign's policy one ``AgentCase`` for each of its starting or running
agents, and acts on its answer:

- ``Alive``: the agent goes on. The pass asks again by ``recheck`` at the latest: a new snapshot of the agent's session
  does not wake it.
- ``Stalled``: the daemon ends the agent, as it ends one for a stop, and frees its slot. Its run ends ``stalled``, which is
  not an attempt at its idea, and its idea opens again, with a note that says ``reason``.
- ``EscalateStall``: piloop posts one escalation at priority 0 for each distinct ``reason`` on the agent's run, and then
  acts on ``then``.

The policy is pure: it reads only the case. ``[plugins.liveness] provider`` selects it, and the other keys of that
table are its fields. Without the table, the policy is ``probes``. The role's timeout still bounds every agent. The
seam copies the probes of Kubernetes: a startup probe, a liveness probe, and a failure threshold.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import timedelta

from piloop.db.models import AgentSessionState
from piloop.ids import RoleName
from piloop.plugins.host import Capability, One, Parameters, Scope, Tier


@dataclass(frozen=True)
class AgentCase:
    """What a liveness policy reads for one agent that is starting or running."""

    role: RoleName
    """The agent's role: a policy may treat a role apart, such as one whose agents wait long on purpose."""
    harness: str | None
    """The ``[plugins.harness.<name>]`` that runs the agent. None only for a row that a person wrote without one."""
    state: AgentSessionState
    """``starting``: its job waits for a worker, or its worker sets it up (a clone, the first transfer). ``running``:
    its harness runs."""
    running_for: timedelta | None
    """Since a worker took its job (``leases.acquired_at``). None while its job waits for a worker."""
    quiet_for: timedelta | None
    """Since its last snapshot of its session file (``SessionSaved``), or since it started running when it saved none
    since. A worker snapshots the file at most 30 s after it grows. None while it is starting."""
    stalls: int
    """How many earlier runs of the ideas of its run ended ``stalled``."""


@dataclass(frozen=True)
class Alive:
    """The agent goes on."""

    recheck: timedelta | None = None
    """When to ask again at the latest, from now. None: at the next pass that something else wakes."""


@dataclass(frozen=True)
class Stalled:
    """The daemon ends the agent: its run ends ``stalled``, and its idea opens again."""

    reason: str
    """Why, for the run's error and the note on its idea: ``piloop status`` shows ``ended stalled: <reason>``."""


@dataclass(frozen=True)
class EscalateStall:
    """piloop posts one escalation at priority 0 for each distinct ``reason``, and then acts on ``then``."""

    reason: str
    """What a human must decide, for the escalation: ``Liveness policy <name>: <reason>``."""
    then: Alive | Stalled
    """What piloop does with the agent."""


class LivenessPolicy(Parameters, ABC):
    """A liveness policy: its fields are its parameters. A policy has no error: it answers every case."""

    @abstractmethod
    def check(self, case: AgentCase) -> Alive | Stalled | EscalateStall:
        """Return whether the agent goes on or ends, or tell a human why and then do one of those."""


LIVENESS = Capability(
    'liveness',
    LivenessPolicy,
    tier=Tier.PURE,
    table=One(Scope.CAMPAIGN, default='probes'),
    group='piloop.liveness_policies',
    noun='liveness policy',
)
"""Every liveness policy by name. ``probes``, built in and the default, ends an agent that does not start, or that goes
quiet (``piloop.plugins.liveness.probes``)."""
