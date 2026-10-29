"""The launch policy capability: which campaign the next free agent slot goes to (``[plugins.launch_policy]``).

On each pass the daemon launches the agents that campaigns requested while the executor has free agent slots. For each
slot it asks the home's launch policy which waiting campaign gets it (``pick``), then takes that campaign's oldest
requested agent that can start. Core keeps the rest: the executor's slots, each role's and each harness's
``max_concurrent``, and the startup breaker, whose limits are the policy's fields. ``fewest_live``, built in and the
default, gives each slot to the campaign with the fewest live agents.

The policy is pure: it sees only ``LaunchCase``. A policy reads its own parameters for what it needs about a campaign,
such as a priority or a weight by campaign name. Returning None leaves the slots free in this pass, so a quota is a
policy that picks no campaign over it. The seam copies the queue-sort plugin of the Kubernetes scheduler: one per
scheduler, asked for an order, with None for a quota.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import timedelta

from pydantic import Field

from piloop.ids import CampaignId
from piloop.plugins.host import Capability, One, Parameters, Scope, Tier


@dataclass(frozen=True)
class Share:
    """One campaign with live agents or agents that wait for a slot."""

    campaign: CampaignId
    """Its id: what ``pick`` returns."""
    name: str
    """Its name, as ``piloop.toml`` sets it: what a policy's own keys name it by."""
    live: int
    """Its live agents, with those that this pass launched so far."""
    waits: bool
    """Whether it has requested agents that can take a slot in this pass. Only such a campaign can be picked."""


@dataclass(frozen=True)
class LaunchCase:
    """What a launch policy reads for one free agent slot."""

    free: int | None
    """The free agent slots left in this pass, this one included. None: the executor sets no bound."""
    campaigns: tuple[Share, ...]
    """Every campaign that has live agents or waits, by id."""


class LaunchPolicy(Parameters, ABC):
    """A launch policy: its fields are its parameters, the keys of ``[plugins.launch_policy]`` in ``config.toml``."""

    startup_breaker: int = Field(default=3, gt=0)
    """How many of a campaign's agents can fail at startup within ``startup_window`` before its launches pause. A host
    that is too loaded to start agents makes every new agent fail, and each failure costs a start window."""
    startup_window: timedelta = Field(default=timedelta(minutes=10), gt=timedelta(0))
    """How far back ``startup_breaker`` counts. Launches resume when fewer failures are that recent."""

    @abstractmethod
    def pick(self, case: LaunchCase) -> CampaignId | None:
        """Return the waiting campaign that the next free slot goes to, or None: no campaign launches more now.

        It is asked again for each slot, with the live agents of the campaign it picked counted up.
        """


LAUNCH_POLICIES = Capability(
    'launch_policy',
    LaunchPolicy,
    tier=Tier.PURE,
    table=One(Scope.HOME, default='fewest_live'),
    group='piloop.launch_policies',
)
"""Every launch policy by name. ``fewest_live``, built in and the default, gives each slot to the campaign with the fewest
live agents."""
