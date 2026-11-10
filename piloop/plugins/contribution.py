"""What a provider or a capability adds to core: the daemon's ``Contribution``, and the routes of what ``Serves``.

The daemon asks each provider that it opened once, in the order that it opened them, then each capability that runs
machinery of its own (``CONTRIBUTORS``). It hands their consumers to its loop (``collect``). A provider's routes are not
the daemon's: each process that serves the API mounts those of each installed provider that ``Serves``, built with its
defaults (``provided_routers``). What a package adds before anything opens, it declares in a ``Slot`` (``piloop.plugins.host``).

Inspiration: Backstage's backend plugins, which register their routers, scheduled tasks and startup and shutdown hooks
with the core services.
"""

from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from pydantic import TypeAdapter, ValidationError

from piloop.daemon.loop import Consumer
from piloop.plugins.host import PluginError, Plugins, Scope, Slot, declared
from piloop.settings import AgentSettings, DaemonSettings

if TYPE_CHECKING:
    # a command that reads a contract loads no web framework (``tests/cli/test_imports.py``)
    from fastapi import APIRouter

    # the run controller and the database import the contracts that import this module
    from piloop.db.session import Database
    from piloop.otel import Tracer
    from piloop.plugins.campaign_type.contract import Planner
    from piloop.plugins.campaign_type.kit.runs.controller import RunController
    from piloop.plugins.runtime.contract import RuntimeFactory


class Leaving(StrEnum):
    """How the daemon stops."""

    STOP = 'stop'
    """For good: what is still open fails."""
    HAND_OVER = 'hand_over'
    """For an in-place restart: nothing is written, and the next daemon process adopts what runs."""


class Lifecycle(Protocol):
    """What a contribution does when the daemon starts and stops, beside its consumers."""

    def start(self) -> None:
        """Take over what a previous daemon process left. The daemon calls it before its loop's first turn."""
        ...

    def in_flight(self) -> Mapping[str, Future[None]]:
        """Its threads in flight, by what they work on: a stop waits for them."""
        ...

    def stop(self, leaving: Leaving, timeout: float) -> None:
        """Start nothing more, and wait up to ``timeout`` seconds for what is in flight. The executor stopped first."""
        ...


@dataclass(frozen=True)
class DaemonHost:
    """What the daemon gives the machinery of a capability (``CONTRIBUTORS``)."""

    db: Database
    tracer: Tracer
    home: Path
    controller: RunController
    """The daemon's executor, launcher, sources and CAS, and the runs that it executes."""
    runtime_factory: RuntimeFactory
    wake: Callable[[str], None]
    """Runs the consumer of this name in the next turn of the loop: a thread of the daemon ended (``Loop.wake``)."""
    daemon: DaemonSettings
    agents: AgentSettings
    planners: Mapping[str, Planner]
    """Each installed campaign type at work in this daemon, by its name (``campaigns.type``)."""
    thread: Callable[[Callable[[], None]], Future[None]]
    """Runs a body in a thread of the daemon."""
    plugins: Plugins
    """The home's ``[plugins]`` (``config.toml``), validated: the instances of a capability of the home."""
    defaults: Mapping[str, object] = field(default_factory=dict)
    """The tables of ``config.toml`` that are defaults of every campaign, unvalidated (``host.campaign_defaults``): the
    home's own instances of a capability that campaigns configure, for what belongs to no campaign."""


@dataclass(frozen=True)
class Contribution:
    """What one provider adds while the daemon runs."""

    consumers: tuple[Consumer, ...] = ()
    """Jobs of the daemon's loop. Each names its ``Phase``: within one, a turn runs them in the order of their
    contributions."""
    lifecycle: Lifecycle | None = None
    """What it does when the daemon starts and stops."""


@runtime_checkable
class Contributes(Protocol):
    """A provider that adds to core."""

    def contribution(self) -> Contribution:
        """What it adds to core. The daemon asks once, after it opened the provider."""
        ...


@runtime_checkable
class Serves(Protocol):
    """A provider whose routes the daemon's API serves, beside core's: a campaign type's, say.

    Each process that serves the API builds the provider with its defaults and mounts its routes (``provided_routers``), so a
    route reads only the database and its request, never a daemon's memory. A route of a method and path that is taken
    is refused. The API's document lists them without a daemon.
    """

    def routes(self) -> APIRouter:
        """Its routes. Providers that share one router, the instances of a capability, return the same object."""
        ...


@dataclass(frozen=True)
class Contributions:
    """Every contribution of a daemon process, in the order that it opened their providers."""

    consumers: tuple[Consumer, ...] = ()
    lifecycles: tuple[Lifecycle, ...] = ()


type Machinery = Callable[[DaemonHost], Contribution]
"""What a capability runs itself, beside its providers: the reader of its providers, say."""


CONTRIBUTORS: Slot[Machinery] = Slot('contributors', TypeAdapter(Machinery), 'contributor')
"""What runs in the daemon for a capability, by the capability's name, or for a provider that needs the daemon's host,
by the provider's name: entry points ``piloop.contributors``."""


def provided_routers() -> tuple[APIRouter, ...]:
    """The routes of each installed provider that ``Serves`` (``installed``): each router once, in registry order.

    Raises:
        PluginError: A provider that serves cannot be built with its defaults. The message names it.
    """
    routers: list[APIRouter] = []
    for provider in installed():
        if (router := provider.routes()) not in routers:
            routers.append(router)
    return tuple(routers)


def installed() -> tuple[Serves, ...]:
    """Each installed provider that ``Serves`` of a capability that a campaign chooses, or that no table chooses.

    Each is built with its defaults, in registry order: by capability, then by provider.

    Raises:
        PluginError: A provider that serves cannot be built with its defaults. The message names it.
    """
    found: list[Serves] = []
    for capability in declared():
        if capability.table is not None and capability.table.scope is not Scope.CAMPAIGN:
            continue
        for name, provider in capability.providers().items():
            if not issubclass(provider.model, Serves):
                continue
            try:
                built = provider.model()
            except ValidationError as e:
                errors = '; '.join(f'{".".join(map(str, err["loc"]))}: {err["msg"]}' for err in e.errors())
                raise PluginError(
                    f'the {capability.noun} {name!r} serves routes, and cannot be built with its defaults: {errors}'
                ) from e
            found.append(built)
    return tuple(found)


def contributions_of(providers: Sequence[Contributes]) -> list[tuple[str, Contribution]]:
    """Ask each of ``providers`` for its contribution, by the name of its class."""
    return [(type(p).__qualname__, p.contribution()) for p in providers]


def collect(contributions: Sequence[tuple[str, Contribution]], taken: Sequence[str]) -> Contributions:
    """Join ``contributions``, by their owners' names, and refuse a consumer name that ``taken`` or another has.

    Raises:
        PluginError: Two consumers have one name. The message names both sides.
    """
    owners = dict.fromkeys(taken, 'the daemon')
    consumers: list[Consumer] = []
    lifecycles: list[Lifecycle] = []
    for owner, contribution in contributions:
        lifecycles += [contribution.lifecycle] if contribution.lifecycle is not None else []
        for consumer in contribution.consumers:
            if (other := owners.get(consumer.name)) is not None:
                raise PluginError(f'{owner} contributes the consumer {consumer.name!r}, which {other} has already')
            owners[consumer.name] = owner
            consumers.append(consumer)
    return Contributions(tuple(consumers), tuple(lifecycles))


def machinery(host: DaemonHost) -> list[tuple[str, Contribution]]:
    """Ask the machinery of each capability (``CONTRIBUTORS``) for its contribution, by the capability's name."""
    return [(name, make(host)) for name, make in CONTRIBUTORS.loaded().items()]
