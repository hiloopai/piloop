"""``Readings``: a fact source of named values that the daemon reads every ``interval`` seconds.

Each reading is a ``fact.read`` event on the campaign, the one record of its values. The source supplies each name
that it reads (``reading``), and how much its value grew since a candidate last became the best (``since_best``), from those events. It
posts a note at each of its ``notices``, and an escalation when its readings start to fail.

A reading counts only for the source as it read: the event names a digest of its parameters (``Readings.digest``), and
a source's readings are its series, those since another config of it last read its names. A source whose command
changed in ``piloop init`` has no reading yet: its values are unknown, and it reads at once. A command that changes
back starts a new series too.
"""

import hashlib
import json
import math
from abc import abstractmethod
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, ClassVar, Literal, Self

from pydantic import Field, model_validator
from sqlalchemy import ColumnElement, func, select
from sqlalchemy.orm import Session

from piloop import deprecated
from piloop.actor import System
from piloop.campaign import escalations
from piloop.db.events import has, latest, payload_of
from piloop.db.models import Campaign, Comment, Event, EventModel
from piloop.db.triggers import DAEMON_ACTOR
from piloop.ids import CommentKind, TargetKind, TargetRef
from piloop.plugins.campaign_type.kit.pool.scored import last_best_at
from piloop.plugins.escalation.contract import Choice, EscalationView
from piloop.plugins.fact_source.contract import FACT_SOURCES, FactReadError, FactSource
from piloop.plugins.fact_source.facts import Fact, FactValue, reading, since_best

NOTICE_PRIORITY = 1
NEVER_READ = datetime.min.replace(tzinfo=UTC)
"""The due of a source that never read: now."""


class FactRead(EventModel):
    """One reading of a ``Readings`` source: each value by its name, or None for each when the reading failed."""

    kind: Literal['fact.read'] = 'fact.read'
    values: dict[str, float | None]
    error: str | None = None
    source: str | None = None
    """The digest of the source that read it (``Readings.digest``). None: a reading of 0.8, which counts for the
    source as it is now."""


class Readings(FactSource):
    """A source that the daemon reads every ``interval`` seconds.

    It supplies each name that it reads (``reading``), and how much its value grew since a candidate last became the best
    (``since_best``), from its ``fact.read`` events.
    """

    CHANGES: ClassVar[frozenset[type[EventModel]]] = frozenset({FactRead})
    """Each reading is a ``fact.read`` event."""

    interval: float = Field(default=300, gt=0)
    """The number of seconds between two readings."""
    notices: dict[str, list[Annotated[float, Field(gt=0)]]] = {}
    """Values by the name that it reads. When a value passes one, the daemon posts one note to the inbox, at priority
    1."""

    @model_validator(mode='after')
    def _notices_name_its_values(self) -> Self:
        if unknown := sorted(set(self.notices) - set(self.names())):
            raise ValueError(f'notices for {", ".join(unknown)}, which the source does not read')
        return self

    @abstractmethod
    def names(self) -> tuple[str, ...]:
        """The names of the values that it reads."""

    def digest(self) -> str:
        """What its readings are of: its provider and its parameters, without ``interval`` and ``notices``.

        Those two say when it reads and what it posts, not what it reads.
        """
        params = self.model_dump(mode='json', exclude=set(Readings.model_fields))
        provider = f'{type(self).__module__}.{type(self).__qualname__}'
        return hashlib.sha256(json.dumps([provider, params], sort_keys=True).encode()).hexdigest()

    @abstractmethod
    def read(self, repo: Path) -> dict[str, float]:
        """Read every value of ``names``. The daemon calls it outside any transaction, at the campaign's repo root.

        Raises:
            FactReadError: No values were read.
        """

    def supplies(self) -> tuple[Fact[FactValue], ...]:
        """``reading(name)`` and ``since_best(name)`` for each name of ``names``."""
        return tuple(fact for name in self.names() for fact in (reading(name), since_best(name)))

    def gather(
        self, s: Session, campaign: Campaign, wanted: frozenset[Fact[FactValue]], now: datetime
    ) -> dict[Fact[FactValue], FactValue]:
        """Each value of the latest reading that has it, None after a failed one, and its growth since the latest new best."""
        found: dict[Fact[FactValue], FactValue] = {}
        for name in self.names():
            value = self._latest(s, campaign, name)
            if reading(name) in wanted:
                found[reading(name)] = value
            if since_best(name) in wanted:
                found[since_best(name)] = None if value is None else value - self._at_best(s, campaign, name, value)
        return found

    def as_of(self, s: Session, campaign: Campaign, fact: Fact[FactValue], now: datetime) -> datetime | None:
        """The time of the latest reading of the name that ``fact`` is of: None before the first."""
        name = next(n for n in self.names() if fact in (reading(n), since_best(n)))
        last = latest(s, FactRead, self._series(campaign, name))
        return None if last is None else last[0].at

    def due(self, s: Session, campaign: Campaign) -> datetime | None:
        """When the source reads next: ``interval`` after its latest reading, or now before the first."""
        last = latest(s, FactRead, self._series(campaign, self.names()[0]))
        return NEVER_READ if last is None else last[0].at + timedelta(seconds=self.interval)

    def record(self, s: Session, campaign: Campaign, values: dict[str, float] | FactReadError) -> None:
        """Record a reading as a ``fact.read`` event, with a note for each notice that it passed. The caller commits.

        The first reading passes every notice below it. The first failed reading of a series posts an escalation at
        priority 0: a stop rule that reads the values may hold on them.
        """
        on = TargetRef(TargetKind.CAMPAIGN, campaign.id)
        if isinstance(values, FactReadError):
            failing = self.names()[0]
            last = latest(s, FactRead, self._series(campaign, failing))
            s.add(
                Event(
                    target=on,
                    actor=DAEMON_ACTOR,
                    payload=FactRead(values=dict.fromkeys(self.names()), error=str(values), source=self.digest()),
                )
            )
            if last is None or last[1].values.get(failing) is not None:
                asked = EscalationView(
                    headline='A fact source cannot read its facts. Do you fix it?',
                    why='The stop rule can read these facts. Without them, it can hold the campaign.',
                    choices=(
                        Choice(id='A', label="Fix the source's command.", result='The next reading records the facts.'),
                        Choice(
                            id='B', label='Take the source out of the campaign.', result='Nothing reads these facts.'
                        ),
                    ),
                    details=f'Fact source {FACT_SOURCES.name_of(self)} ({", ".join(self.names())}): the reading'
                    f' failed ({values}).',
                )
                escalations.escalate(s, on, asked, author=System.DAEMON, priority=0)
            return
        before = {name: self._last_known(s, campaign, name) for name in self.notices}
        s.add(Event(target=on, actor=DAEMON_ACTOR, payload=FactRead(values=dict(values), source=self.digest())))
        for name, notices in self.notices.items():
            was = -math.inf if (known := before[name]) is None else known
            for notice in notices:
                if was < notice <= values[name]:
                    body = f'Notice: {name} is {values[name]:g}, past the {notice:g} notice.'
                    s.add(
                        Comment(
                            target=on, author=DAEMON_ACTOR, kind=CommentKind.NOTE, priority=NOTICE_PRIORITY, body=body
                        )
                    )

    def _series(self, campaign: Campaign, name: str) -> ColumnElement[bool]:
        """Select this source's readings of ``name`` on ``campaign``: those since another config of it last read it."""
        on = (Event.target == TargetRef(TargetKind.CAMPAIGN, campaign.id)) & _of(name)
        deprecated.until('0.10.0', 'a fact.read of 0.8 has no source and counts for the current one: drop the coalesce')
        digest = func.coalesce(func.json_extract(Event.payload, '$.source'), self.digest())
        other = select(func.max(Event.id)).where(has(FactRead), on, digest != self.digest()).scalar_subquery()
        return on & (digest == self.digest()) & (Event.id > func.coalesce(other, 0))

    def _latest(self, s: Session, campaign: Campaign, name: str) -> float | None:
        """The value of ``name`` of the latest reading that has it: None before the first, and after a failed one."""
        last = latest(s, FactRead, self._series(campaign, name))
        return None if last is None else last[1].values.get(name)

    def _last_known(self, s: Session, campaign: Campaign, name: str) -> float | None:
        """The value of ``name`` of the latest reading that read one, failed readings since or not."""
        last = latest(s, FactRead, self._series(campaign, name), _known(name))
        return None if last is None else last[1].values.get(name)

    def _at_best(self, s: Session, campaign: Campaign, name: str, now: float) -> float:
        """``name`` when a candidate last became the best: the latest reading by then, else the first after.

        Before the first new best it is 0, so all of the value counts.
        """
        at = last_best_at(s, campaign)
        if at is None:
            return 0.0
        readings = select(Event).where(has(FactRead), self._series(campaign, name), _known(name))
        found = (
            s.scalars(readings.where(Event.at <= at).order_by(Event.id.desc()).limit(1)).first()
            or s.scalars(readings.where(Event.at > at).order_by(Event.id).limit(1)).first()
        )
        then = payload_of(found, FactRead).values.get(name) if found is not None else None
        return now if then is None else then


def _of(name: str) -> ColumnElement[bool]:
    """Select the readings that have a value, or None, for ``name``."""
    return func.json_type(Event.payload, f'$.values."{name}"').is_not(None)


def _known(name: str) -> ColumnElement[bool]:
    """Select the readings that read a value for ``name``: not the failed ones."""
    return func.json_type(Event.payload, f'$.values."{name}"').in_(('real', 'integer'))
