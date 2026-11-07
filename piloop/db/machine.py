"""An entity's state column with one owner: each transition is one guarded UPDATE, and emits one event.

A machine names its column and, for each state, the states that may move to it. ``transition`` is the only code that
writes the column: it refuses a move from any other state, so two writers that race cannot both move a row, and the
second learns what the first did (``Refused.found``). A raw ``piloop sql`` UPDATE is the escape hatch: the table's
CHECKs are what it meets, and its readers read the state again.
"""

from collections.abc import Mapping
from enum import StrEnum

from sqlalchemy import select, update
from sqlalchemy.orm import InstrumentedAttribute, Session

from piloop.actor import Actor
from piloop.db.events import emit
from piloop.db.models import EventModel
from piloop.ids import TargetKind, TargetRef


class Machine[S: StrEnum]:
    """The state ``column`` of a table whose rows have an integer ``id``, such as ``Run.status``, and its moves.

    ``edges`` has, for each state, the states that may move to it: a state that is no key is never moved to. ``kind``
    is what a row's events point at, such as ``run``.
    """

    def __init__(self, column: InstrumentedAttribute[S], edges: Mapping[S, frozenset[S]], kind: TargetKind) -> None:
        # set here, not declared on the class: a declared attribute of this type reads as a descriptor
        self.column, self.edges, self.kind = column, edges, kind


class Refused[S: StrEnum](Exception):
    """The row was not in a state that may move there: another writer moved it first, to ``found``."""

    def __init__(self, ref: TargetRef, to: S, found: S) -> None:
        super().__init__(f'{ref} is {found}: it cannot become {to}')
        self.ref, self.to, self.found = ref, to, found


def transition[S: StrEnum](
    s: Session, m: Machine[S], row_id: int, to: S, event: EventModel, actor: Actor, **values: object
) -> None:
    """Move row ``row_id`` to ``to``, with ``values`` in the same UPDATE, and emit ``event`` on it. The caller commits.

    Raises:
        Refused: the row is in a state that may not move to ``to``.
    """
    table = m.column.class_
    assert isinstance(table, type)  # a mapped class, not an alias: the machine names its column on it
    ref = TargetRef(m.kind, row_id)
    moved = s.execute(
        update(table)
        .where(table.id == row_id, m.column.in_(m.edges.get(to, frozenset())))
        .values({m.column.key: to, **values})
        .returning(table.id)
        .execution_options(synchronize_session='fetch')
    ).first()
    if moved is None:
        raise Refused(ref, to, s.execute(select(m.column).where(table.id == row_id)).scalar_one())
    emit(s, event, ref, actor)
