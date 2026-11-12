"""Ids that never come back: the tables whose ids leave the daemon count with AUTOINCREMENT, and a term lifts them.

A worker, an agent or a client holds an id of these tables and sends it back later: a lease (the fence), an agent
session, an evaluation, an event (a cursor), a comment, a run, a candidate, an idea, a measurement. A database restored
from its replica lost the last seconds of commits, and its ``sqlite_sequence`` with them, so it would hand those ids out
again. So the first transaction of a term that may follow a restore lifts every AUTOINCREMENT sequence to a floor that
no earlier term reached (``lift``): ``floor(epoch)`` (``piloop.plugins.leader``).
"""

import re
from collections.abc import Callable

from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.orm import Session

SHIFT = 32
"""A term's ids start at ``epoch << SHIFT``: 2^32 ids per term (about 4 billion), and every id of the first 2^21 - 1
terms (about 2 million) is below ``EXACT``."""
EXACT = 2**53
"""The largest integer that a JSON number holds exactly: a client in JavaScript reads ids as numbers."""


class IdsExhausted(Exception):
    """A term's ids would pass ``EXACT``: the home has had too many terms for its ids to stay exact."""


def floor(epoch: int) -> int:
    """The first id of term ``epoch``: every id of an earlier term is below it.

    Raises:
        IdsExhausted: The term's ids would reach ``EXACT``.
    """
    if (epoch + 1) << SHIFT > EXACT:
        raise IdsExhausted(f'term {epoch} would hand out ids past 2^53: this home has had too many terms')
    return epoch << SHIFT


def lift(s: Session, floor: int) -> None:
    """Make every AUTOINCREMENT table hand out ids above ``floor`` from now, unless it is past it. The caller commits."""
    tables = s.scalars(
        text("SELECT name FROM sqlite_master WHERE type = 'table' AND sql LIKE '%PRIMARY KEY AUTOINCREMENT%'")
    ).all()
    for table in tables:
        if s.scalar(text('SELECT 1 FROM sqlite_sequence WHERE name = :t'), {'t': table}) is None:
            # a table that never had a row has no sequence yet
            s.execute(text('INSERT INTO sqlite_sequence (name, seq) VALUES (:t, :floor)'), {'t': table, 'floor': floor})
        else:
            s.execute(
                text('UPDATE sqlite_sequence SET seq = max(seq, :floor) WHERE name = :t'), {'t': table, 'floor': floor}
            )


PLAIN = re.compile(r'\bid INTEGER NOT NULL,(?P<rest>.*?),\s*PRIMARY KEY \(id\)', re.DOTALL)
AUTO = re.compile(
    r'\bid INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,(?P<rest>.*?)(?P<end>,\s*CONSTRAINT|\n\))', re.DOTALL
)


def rebuild(op: Operations, table: str, *, autoincrement: bool) -> None:
    """Rebuild ``table`` with or without AUTOINCREMENT on ``id``, as ``rewrite`` does. SQLite cannot add AUTOINCREMENT."""

    def edit(sql: str) -> str:
        if autoincrement:
            new, n = PLAIN.subn(r'id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,\g<rest>', sql, count=1)
        else:
            new, n = AUTO.subn(r'id INTEGER NOT NULL,\g<rest>,\n\tPRIMARY KEY (id)\g<end>', sql, count=1)
        assert n == 1, f'{table}: no id column of the expected form'
        return new

    rewrite(op, table, edit)


def rewrite(op: Operations, table: str, edit: Callable[[str], str]) -> None:
    """Rebuild ``table`` from the SQL that ``edit`` makes of its own, keeping its rows, indexes, triggers and sequence.

    A revision copies it into a new table. It keeps the table's exact SQL but for ``edit``'s change, partial indexes
    and generated columns included, which a batch rebuild would reflect and could lose. The triggers that name the
    table, its own and those on other tables (an ``AFTER INSERT`` that writes ``events``), are dropped first and created
    again from their SQL: a rename refuses to run while one names a table that is gone. An AUTOINCREMENT table keeps
    its sequence: its copy would start again after its largest id, below a floor that a term lifted it to (``lift``).
    """
    bind = op.get_bind()
    sql = bind.execute(text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = :t"), {'t': table}).scalar()
    assert isinstance(sql, str), f'no table {table}'
    staged = f'{table}_rebuilt'
    new = re.sub(rf'^CREATE TABLE "?{table}"?', f'CREATE TABLE "{staged}"', edit(sql), count=1)
    indexes = (
        bind.execute(
            text("SELECT sql FROM sqlite_master WHERE type = 'index' AND tbl_name = :t AND sql IS NOT NULL"),
            {'t': table},
        )
        .scalars()
        .all()
    )
    triggers = bind.execute(
        text(
            "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' AND (tbl_name = :t OR sql LIKE '%' || :t || '%')"
        ),
        {'t': table},
    ).all()
    sequences = bind.execute(text("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'sqlite_sequence'"))
    seq = (
        bind.execute(text('SELECT seq FROM sqlite_sequence WHERE name = :t'), {'t': table}).scalar()
        if sequences.scalar()
        else None
    )
    # Generated columns are computed: the copy leaves them out (``table_xinfo``'s hidden 2 and 3).
    columns = ', '.join(f'"{c[1]}"' for c in bind.execute(text(f'PRAGMA table_xinfo("{table}")')) if c[6] == 0)
    for name, _ in triggers:
        op.execute(f'DROP TRIGGER "{name}"')
    op.execute(new)
    op.execute(f'INSERT INTO "{staged}" ({columns}) SELECT {columns} FROM "{table}"')
    op.execute(f'DROP TABLE "{table}"')
    op.execute(f'ALTER TABLE "{staged}" RENAME TO "{table}"')
    if seq is not None:  # an empty copy has no row, and a full one starts after its largest id
        now = bind.execute(text('SELECT max(seq) FROM sqlite_sequence WHERE name = :t'), {'t': table}).scalar() or 0
        bind.execute(text('DELETE FROM sqlite_sequence WHERE name = :t'), {'t': table})
        bind.execute(
            text('INSERT INTO sqlite_sequence (name, seq) VALUES (:t, :seq)'), {'t': table, 'seq': max(seq, now)}
        )
    for index in indexes:
        op.execute(index)
    for _, trigger in triggers:
        op.execute(trigger)
