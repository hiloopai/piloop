"""Add to the events feed, and find events in it. Every state change that piloop makes goes through ``emit``."""

from collections.abc import Mapping
from dataclasses import dataclass
from functools import cached_property
from typing import Literal

from pydantic_core import to_jsonable_python
from sqlalchemy import ColumnElement, and_, func, select
from sqlalchemy.orm import Session

from piloop.actor import Actor
from piloop.db.models import Event, EventModel
from piloop.ids import CampaignId, TargetRef


class SubjectSettled(EventModel):
    """On a subject (``idea#12``): its work is over.

    So the top-level workspace entries named after it (``idea12``, ``idea12-…``) are final.
    """

    kind: Literal['subject.settled'] = 'subject.settled'
    campaign: CampaignId


def emit(s: Session, event: EventModel, target: TargetRef, actor: Actor) -> Event:
    """Add ``event`` on ``target``, signed by ``actor``, to the session. The caller commits."""
    row = Event(actor=actor.signature, target=target, payload=event)
    s.add(row)
    return row


def has(model: type[EventModel], **fields: object) -> ColumnElement[bool]:
    """The events of ``model``'s kinds, and of its subclasses', whose payload has each value of ``fields``.

    A tuple is one of its values.

    Raises:
        TypeError: ``model`` has no such field.
    """
    clauses = [Event.kind.in_([kind for kind, m in EventModel.registered().items() if issubclass(m, model)])]
    for name, wanted in fields.items():
        field = model.model_fields.get(name)
        if field is None:
            raise TypeError(f'{model.__qualname__} has no field {name!r}')
        values = wanted if isinstance(wanted, tuple) else (wanted,)
        path = func.json_extract(Event.payload, f'$.{field.alias or name}')
        clauses.append(path.in_([to_jsonable_python(v) for v in values]))
    return and_(*clauses)


@dataclass(frozen=True)
class NewEvent:
    """An event as ``Database`` reads it after its commit, to wake the waiters whose ``Topic`` admits it."""

    id: int
    kind: str
    target_kind: str
    target_id: int
    payload: Mapping[str, object] | None
    """Its stored JSON. None when that is no JSON object: no ``Topic.to`` admits it."""


@dataclass(frozen=True)
class Topic:
    """The events of ``model`` and of its subclasses: ``EventModel``, the default, is every event.

    With ``to``, only those addressed to that worker: the model's ``worker``. With ``target``, only those on that row.
    """

    model: type[EventModel] = EventModel
    to: str | None = None
    target: TargetRef | None = None

    def matches(self) -> ColumnElement[bool]:
        """The events that it admits, in SQL."""
        clauses = [has(self.model) if self.to is None else has(self.model, worker=self.to)]
        if self.target is not None:
            clauses.append(Event.target == self.target)
        return and_(*clauses)

    def admits(self, event: NewEvent) -> bool:
        """Whether ``event`` is of the topic: ``matches`` in memory."""
        if self._kinds is not None and event.kind not in self._kinds:
            return False
        if self.target is not None and (event.target_kind, event.target_id) != (self.target.kind, self.target.id):
            return False
        return self._to is None or (event.payload is not None and event.payload.get(self._to) == self.to)

    @cached_property
    def _kinds(self) -> frozenset[str] | None:
        """The kinds of ``model`` and of its subclasses. None for ``EventModel``: every kind, unknown ones too."""
        if self.model is EventModel:
            return None
        return frozenset(kind for kind, m in EventModel.registered().items() if issubclass(m, self.model))

    @cached_property
    def _to(self) -> str | None:
        """The key of the model's ``worker`` in the stored JSON, when ``to`` is set."""
        if self.to is None:
            return None
        field = self.model.model_fields['worker']
        return field.alias or 'worker'


def latest[M: EventModel](s: Session, model: type[M], *where: ColumnElement[bool]) -> tuple[Event, M] | None:
    """The newest event of ``model`` that matches ``where``, with its payload, or None."""
    found = s.scalars(select(Event).where(has(model), *where).order_by(Event.id.desc()).limit(1)).first()
    return None if found is None else (found, payload_of(found, model))


def in_order[M: EventModel](s: Session, model: type[M], *where: ColumnElement[bool]) -> list[tuple[Event, M]]:
    """The events of ``model`` that match ``where``, oldest first, with their payloads."""
    found = s.scalars(select(Event).where(has(model), *where).order_by(Event.id))
    return [(e, payload_of(e, model)) for e in found]


def payload_of[M: EventModel](event: Event, model: type[M]) -> M:
    """The payload of ``event`` as ``model``.

    Raises:
        TypeError: it is of another model: the module that registers its kind is not loaded, or another one is.
    """
    if not isinstance(event.payload, model):
        raise TypeError(f'event#{event.id} is {type(event.payload).__qualname__}, not {model.__qualname__}')
    return event.payload
