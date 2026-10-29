"""The harness choice capability: which harness of a role's rotation the next agent runs on (``[plugins.harness_choice]``).

A run's harness is chosen when the run is queued, a sweep's when the sweep is taken, and a queued run's again when its
harness left the rotation. Core keeps the rest: a harness that a person pinned (on the idea, or by ``piloop agent
launch --harness``) is never chosen over, and a harness at its ``max_concurrent`` is ``full``. ``round_robin``, built
in and the default, takes the rotation in turn.

The choice is pure: it sees only ``HarnessCase``. It is a campaign's, in its ``piloop.toml``, since the rotation, the caps
and the models are.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass

from piloop.ids import RoleName
from piloop.plugins.host import Capability, ContractBroken, One, Parameters, Scope, Tier


@dataclass(frozen=True)
class HarnessCase:
    """What a harness choice reads for one agent."""

    role: RoleName
    """The role of the agent."""
    rotation: tuple[str, ...]
    """The role's harnesses, in the order of ``piloop.toml``."""
    full: frozenset[str]
    """The harnesses at their ``max_concurrent``: none of them can be chosen now."""
    turn: int
    """How many agents of the role came before this one."""
    resumes: str | None
    """The harness of the role's last saved session, for a role that resumes its sessions: the agent can resume it on
    that harness only. None: there is none, or the role starts afresh."""
    models: dict[str, str]
    """The model that each harness of the rotation runs for the role."""


class HarnessChoice(Parameters, ABC):
    """A harness choice: its fields are its parameters, the keys of ``[plugins.harness_choice]``."""

    @abstractmethod
    def choose(self, case: HarnessCase) -> str | None:
        """Return a harness of the rotation that is not full, or None: every one is, and the agent waits."""


HARNESS_CHOICES = Capability(
    'harness_choice', HarnessChoice, tier=Tier.PURE, table=One(Scope.CAMPAIGN, default='round_robin')
)
"""Every harness choice by name. ``round_robin``, built in and the default, takes the rotation in turn."""


def chosen(choice: HarnessChoice, case: HarnessCase) -> str | None:
    """What ``choice`` chooses for ``case``.

    Raises:
        ContractBroken: It chose a harness that is not in the rotation, or that is full.
    """
    harness = choice.choose(case)
    if harness is not None and (harness not in case.rotation or harness in case.full):
        raise ContractBroken(
            f'harness choice {type(choice).__name__} chose {harness!r}, which is not a free harness of'
            f' {", ".join(case.rotation)}'
        )
    return harness
