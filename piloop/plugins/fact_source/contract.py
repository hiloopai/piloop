"""The fact sources: where a campaign's facts come from (``[plugins.fact_source]``).

A source is a provider of ``FACT_SOURCES``: its fields are its parameters, from ``[plugins.fact_source.<name>]``. It names
the facts that it supplies (``supplies``), and gathers the values of those that a reader asks for (``gather``). A
stop rule or a section reads the facts of every source of the campaign by name (``piloop.plugins.fact_source.facts``):
piloop gives no fact a meaning of its own, a cost or a spend among them.

The campaign's sources are ``[plugins.fact_source] use``: unset, every installed source that is on by default
(``DEFAULT``), ``campaign`` and ``optimization``. A source may also read on its own: the daemon calls its ``read`` when it is ``due``, outside
any transaction, and then its ``record``. ``kit.readings.Readings`` does that for a source of named values, and
``command``, which runs a command that reports its values as the evaluate command does, is one.
"""

from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from piloop.plugins.fact_source.facts import Fact, FactValue
from piloop.plugins.host import Capability, Many, Parameters, Pick, Scope, Use

if TYPE_CHECKING:  # piloop.config imports this module, and the database imports piloop.config
    from sqlalchemy.orm import Session

    from piloop.db.models import Campaign, EventModel


class FactReadError(Exception):
    """A source read no values: it failed, timed out or printed something else. An unknown value is never 0."""


class FactSource(Parameters, ABC):
    """``[plugins.fact_source.<name>]``: a source of facts, which ``[plugins.fact_source] use`` names."""

    DEFAULT: ClassVar[bool] = False
    """Whether a campaign that does not set ``[plugins.fact_source] use`` has this source. Only the class that
    sets it is a default: a subclass is not (``is_default``)."""
    CHANGES: ClassVar[frozenset[type[EventModel]]] = frozenset()
    """The events that ``record`` writes, which wake the campaigns' pass: its stop rule reads the new values. A source
    whose facts change only with the campaign's state (its ideas, runs, agents, status and config) names none."""

    @abstractmethod
    def supplies(self) -> tuple[Fact[FactValue], ...]:
        """The facts that it supplies. Two sources of a campaign never supply the same one."""

    @abstractmethod
    def gather(
        self, s: Session, campaign: Campaign, wanted: frozenset[Fact[FactValue]], now: datetime
    ) -> dict[Fact[FactValue], FactValue]:
        """The values of the facts in ``wanted`` that it supplies, as of ``now``. It reads with ``s``, never writes.

        The campaign's config is ``campaign.config``.
        """

    def reads(self) -> tuple[Fact[FactValue], ...]:
        """The facts of the campaign's other sources that ``gather`` reads: a source that derives its values from theirs.

        The campaign's config checks that another source of ``use`` supplies each. None, by default.
        """
        return ()

    def as_of(self, s: Session, campaign: Campaign, fact: Fact[FactValue], now: datetime) -> datetime | None:
        """When the value of ``fact`` that ``gather`` returns as of ``now`` was read. None: it was never read.

        ``now``, by default: the source computes its values as it gathers them.
        """
        return now

    def due(self, s: Session, campaign: Campaign) -> datetime | None:
        """When the daemon calls ``read`` next: a time at or before now reads now.

        None, by default: the source reads nothing on its own.
        """
        return None

    def read(self, repo: Path) -> dict[str, float]:
        """Read values by name, at the campaign's repo root. The daemon calls it outside any transaction, when it is due.

        Raises:
            FactReadError: No values were read.
        """
        raise NotImplementedError  # a source whose ``due`` is ever set implements it

    def record(self, s: Session, campaign: Campaign, values: dict[str, float] | FactReadError) -> None:
        """Record what ``read`` returned or raised. The caller commits. By default, nothing."""


FACT_SOURCES = Capability(
    'fact_source',
    FactSource,
    table=Many(Scope.CAMPAIGN, use=Use(Scope.CAMPAIGN, Pick.ORDERED)),
    group='piloop.fact_sources',
    noun='fact source',
)
"""Every fact source by name: ``[plugins.fact_source.<name>]``, and each provider that needs no keys. ``[plugins.
fact_source] use`` lists the campaign's: unset, those that are ``DEFAULT``."""
