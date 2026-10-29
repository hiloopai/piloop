"""Each kind that events and comments point at is a row of ``target_kinds``, and a plugin declares its own kinds.

SQLite cannot drop a CHECK, so ``events`` and ``comments`` are rebuilt with their rows, ids and indexes: their
``targetkind`` CHECK becomes a foreign key to ``target_kinds``, and a comment still cannot be on the home. The rebuild
takes about 8 s per million events. Today's kinds are written here with their owners: core's, and the four that
optimization's tables have. ``migrate`` writes each kind that a plugin declares later. ``comments_escalation_event``
writes into ``events``, so it goes while ``events`` is swapped, as in 0044.

The downgrade deletes the events and comments on a kind that 0068 did not know, and rebuilds the CHECKs.

Revision ID: 0069
Revises: 0068
"""

import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import text

from piloop.db.triggers import recreate

revision: str = '0069'
down_revision: str | None = '0068'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen here, as of this revision.
CORE = ('campaign', 'agent_session', 'comment', 'instance', 'home')
OPTIMIZATION = ('idea', 'run', 'candidate', 'measurement')
OLD_EVENTS = ('campaign', 'idea', 'run', 'candidate', 'measurement', 'agent_session', 'comment', 'instance', 'home')
OLD_COMMENTS = OLD_EVENTS[:-1]
FOREIGN = 'CONSTRAINT targetkind FOREIGN KEY(target_kind) REFERENCES target_kinds (id)'
NOT_HOME = "CONSTRAINT comment_not_on_home CHECK (target_kind != 'home')"


def upgrade() -> None:
    op.create_table(
        'target_kinds',
        sa.Column('id', sa.String(length=16), nullable=False),
        sa.Column('owner', sa.String(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    rows = [{'id': k, 'owner': 'core'} for k in CORE] + [{'id': k, 'owner': 'optimization'} for k in OPTIMIZATION]
    op.bulk_insert(sa.table('target_kinds', sa.column('id'), sa.column('owner')), rows)
    op.execute('DROP TRIGGER comments_escalation_event')  # ``comments``' own, which its recreate installs
    _rebuild('events', FOREIGN)
    _rebuild('comments', f'{FOREIGN},\n\t{NOT_HOME}')
    for table in ('events', 'comments'):
        recreate(op, table)


def downgrade() -> None:
    known = ', '.join(repr(k) for k in OLD_EVENTS)
    op.execute(f'DELETE FROM events WHERE target_kind NOT IN ({known})')
    op.execute(f"DELETE FROM comments WHERE target_kind NOT IN ({known}) OR target_kind = 'home'")
    op.execute('DROP TRIGGER comments_escalation_event')
    _rebuild('events', _check(OLD_EVENTS))
    _rebuild('comments', _check(OLD_COMMENTS))
    for table in ('events', 'comments'):
        recreate(op, table)
    op.drop_table('target_kinds')


def _check(kinds: Sequence[str]) -> str:
    return f'CONSTRAINT targetkind CHECK (target_kind IN ({", ".join(repr(k) for k in kinds)}))'


def _rebuild(table: str, constraint: str) -> None:
    """Copy ``table`` into a new one whose ``targetkind`` constraint is ``constraint``, with its ids, then swap them."""
    bind = op.get_bind()
    sql = bind.execute(
        text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = :t"), {'t': table}
    ).scalar_one()
    indexes = (
        bind.execute(
            text("SELECT sql FROM sqlite_master WHERE type = 'index' AND tbl_name = :t AND sql IS NOT NULL"),
            {'t': table},
        )
        .scalars()
        .all()
    )
    stored = bind.execute(text(f"SELECT name FROM pragma_table_xinfo('{table}') WHERE hidden = 0")).scalars().all()
    columns = ', '.join(f'"{c}"' for c in stored)
    pattern = r"CONSTRAINT targetkind (?:CHECK \(target_kind IN \([^)]*\)\)|FOREIGN KEY\(target_kind\) REFERENCES target_kinds \(id\))(?:,\s*CONSTRAINT comment_not_on_home CHECK \(target_kind != 'home'\))?"
    new, found = re.subn(pattern, constraint, sql)
    assert found == 1, f'{table} has no single targetkind constraint: {sql}'
    op.execute(re.sub(rf'^CREATE TABLE "?{table}"?', f'CREATE TABLE {table}_new', new))
    op.execute(f'INSERT INTO {table}_new ({columns}) SELECT {columns} FROM {table}')
    op.execute(f'DROP TABLE {table}')
    op.execute(f'ALTER TABLE {table}_new RENAME TO {table}')
    for index in indexes:
        op.execute(index)
