"""Each kind of comment is a row of ``comment_kinds``, and a plugin declares its own kinds.

SQLite cannot drop a CHECK, so ``comments`` is rebuilt with its rows, ids and indexes, and gets its triggers back: its
``commentkind`` CHECK becomes a foreign key to ``comment_kinds``. Today's kinds are written here: core's four, and
optimization's ``finding``, which an agent whose session has ended cannot write (``after_end``). ``migrate`` writes
each kind that a plugin declares later.

The downgrade deletes the comments of a kind that 0070 did not know, and rebuilds the CHECK.

Revision ID: 0071
Revises: 0070
"""

import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import text

from piloop.db.triggers import recreate

revision: str = '0071'
down_revision: str | None = '0070'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen here, as of this revision.
OLD = ('note', 'finding', 'decision', 'escalation', 'message')
REFUSED_AFTER_END = ('finding',)
FOREIGN = 'CONSTRAINT commentkind FOREIGN KEY(kind) REFERENCES comment_kinds (id)'
CONSTRAINT = (
    r'CONSTRAINT commentkind (?:CHECK \(kind IN \([^)]*\)\)|FOREIGN KEY\(kind\) REFERENCES comment_kinds \(id\))'
)


def upgrade() -> None:
    op.create_table(
        'comment_kinds',
        sa.Column('id', sa.String(length=16), nullable=False),
        sa.Column(
            'after_end',
            sa.Enum('kept', 'refused', name='afterend', native_enum=False, length=16, create_constraint=True),
            server_default='kept',
            nullable=False,
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    rows = [{'id': k, 'after_end': 'refused' if k in REFUSED_AFTER_END else 'kept'} for k in OLD]
    op.bulk_insert(sa.table('comment_kinds', sa.column('id'), sa.column('after_end')), rows)
    _rebuild(FOREIGN)
    recreate(op, 'comments')


def downgrade() -> None:
    op.execute(f'DELETE FROM comments WHERE kind NOT IN ({", ".join(repr(k) for k in OLD)})')
    _rebuild(f'CONSTRAINT commentkind CHECK (kind IN ({", ".join(repr(k) for k in OLD)}))')
    recreate(op, 'comments')
    op.drop_table('comment_kinds')


def _rebuild(constraint: str) -> None:
    """Copy ``comments`` into a new table whose ``commentkind`` constraint is ``constraint``, with its ids, then swap."""
    bind = op.get_bind()
    sql = bind.execute(text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'comments'")).scalar_one()
    indexes = (
        bind.execute(
            text("SELECT sql FROM sqlite_master WHERE type = 'index' AND tbl_name = 'comments' AND sql IS NOT NULL")
        )
        .scalars()
        .all()
    )
    stored = bind.execute(text("SELECT name FROM pragma_table_xinfo('comments') WHERE hidden = 0")).scalars().all()
    columns = ', '.join(f'"{c}"' for c in stored)
    new, found = re.subn(CONSTRAINT, constraint, sql)
    assert found == 1, f'comments has no single commentkind constraint: {sql}'
    op.execute(re.sub(r'^CREATE TABLE "?comments"?', 'CREATE TABLE comments_new', new))
    op.execute(f'INSERT INTO comments_new ({columns}) SELECT {columns} FROM comments')
    op.execute('DROP TABLE comments')
    op.execute('ALTER TABLE comments_new RENAME TO comments')
    for index in indexes:
        op.execute(index)
