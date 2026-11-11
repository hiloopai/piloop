"""The leader capability: which host's daemon writes the home, for how long, and the epoch that fences the others.

``[plugins.leader]`` in ``config.toml`` chooses the home's provider: ``local`` by default. The daemon opens it once,
after it migrated the home's database, and takes a term (``Leader.take``) before it serves. A daemon that cannot take
it waits there as a standby. A term whose ``until`` is set lasts until then unless its holder renews it
(``Leader.renew``), and the daemon writes nothing past it: it begins to stop when renewals fail for
``Renewal.deadline``, and exits at once at ``until`` (``piloop.daemon.holding``). Its stop releases the term after its
last write (``Leader.release``).

A term's epoch is the fencing token: every take writes a higher one than any before it, on any host, and the daemon's
term row in ``terms`` has it.
"""

import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from piloop.db.session import Database
from piloop.plugins.host import Capability, One, Parameters, Scope


class Term(BaseModel):
    """A daemon's right to write the home."""

    model_config = ConfigDict(frozen=True)

    epoch: int = Field(ge=1)
    """Higher than every term's before it: what fences a stale writer."""
    holder: str
    """The daemon that holds it: ``<host>/<pid>``."""
    url: str | None = None
    """Where the holder serves other machines, when it does."""
    until: datetime | None = None
    """When it ends unless renewed, by the holder's clock. None: it lasts until it is released."""


class LostTerm(Exception):
    """Another daemon holds a newer term: the holder must write nothing more."""


class Unreachable(Exception):
    """The record of the terms could not be read or written now. The holder tries again until its deadline."""


@dataclass(frozen=True)
class Renewal:
    """How the holder of a term that ends keeps it."""

    every: timedelta
    """How long after a renewal, or a take, the next one goes."""
    deadline: timedelta
    """How long after the last good renewal the holder begins to stop. Less than the term lasts, so that its stop has
    time before ``Term.until``."""


@dataclass(frozen=True)
class LeaderHost:
    """What piloop gives a leader provider."""

    db: Database
    """The home's database, migrated: a take's epoch is above its newest term's (``piloop.db.terms``)."""


class Leader(Protocol):
    """An opened leader provider: the record of the home's terms."""

    @property
    def renewal(self) -> Renewal | None:
        """How the holder renews its terms. None when they never end."""
        ...

    def take(self, holder: str, url: str | None, stop: threading.Event) -> Term | None:
        """Take the next term for ``holder``, waiting as long as another holds one. None once ``stop`` is set."""
        ...

    def renew(self, term: Term) -> Term:
        """``term`` again, ending later.

        Raises:
            LostTerm: Another daemon took a newer term.
            Unreachable: The record could not be written now.
        """
        ...

    def release(self, term: Term) -> None:
        """End ``term`` now: the holder wrote its last. Another daemon may take the next one at once.

        Raises:
            LostTerm: Another daemon took a newer term.
            Unreachable: The record could not be written now.
        """
        ...

    def current(self) -> Term | None:
        """The term that a daemon holds now, or None."""
        ...

    def begin(self, s: Session, term: Term) -> None:
        """Write what the provider needs in ``term``'s first transaction, before the daemon writes anything else.

        A provider whose term may follow a restore lifts the id sequences there (``piloop.db.ids.lift``). The caller
        commits.

        Raises:
            IdsExhausted: The term's ids would pass 2^53 (``piloop.db.ids.floor``): the daemon does not start.
        """
        ...


class LeaderConfig(Parameters, ABC):
    """A leader provider: its parameters, the keys of ``[plugins.leader]`` besides ``provider``."""

    @abstractmethod
    def open(self, host: LeaderHost) -> Leader:
        """Open the record of the home's terms. The daemon calls it once, before it takes a term."""

    def refuses(self, home: Path, checkout: Path) -> str | None:
        """Why this provider cannot serve a campaign whose checkout is ``checkout``, or None: it can.

        The daemon does not start while a campaign that is not retired has such a checkout, a registration of one is
        refused, and ``piloop doctor`` names it.
        """
        return None


LEADERS = Capability('leader', LeaderConfig, table=One(Scope.HOME, default='local'), noun='leader provider')
"""Every leader provider by name. ``local``, built in and the default, gives every daemon of the home a term at once:
the pid file's lock already makes it the home's only daemon. ``bucket`` keeps the term in an S3 object, for daemons
of one home on several hosts."""
