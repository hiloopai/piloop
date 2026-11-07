"""``evaluations`` gets a CHECK: only a running evaluation is cancelled.

A waiting evaluation that is cancelled leaves the queue, and one that runs keeps ``cancelled_by`` until its job ends.
0.6's lost job cleared ``started_at`` and kept ``cancelled_by``, so a cancelled row could wait again. This deletes each
such row, as the cancel would have, and logs how many. A daemon that starts clears the queue anyway.

SQLite cannot add a CHECK, so the table is rebuilt with its rows, ids and indexes, and gets its triggers back. Its
AUTOINCREMENT counter is kept: a request that waits for a row never mistakes a later one for it. The downgrade drops
the CHECK.

Revision ID: 0010
Revises: 0009
"""

import logging
import re
from collections.abc import Callable, Sequence

from alembic import op
from sqlalchemy import text

from piloop.db.triggers import recreate

revision: str = '0010'
down_revision: str | None = '0009'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen here, as of this revision.
CHECK = (
    'CONSTRAINT "evaluations.only a running evaluation is cancelled"'
    ' CHECK (cancelled_by IS NULL OR started_at IS NOT NULL)'
)
REPAIR = 'DELETE FROM evaluations WHERE cancelled_by IS NOT NULL AND started_at IS NULL'


def upgrade() -> None:
    if repaired := op.get_bind().execute(text(REPAIR)).rowcount:
        logging.getLogger(__name__).warning(
            'optimization migration 0010 deleted %d cancelled evaluations that waited again', repaired
        )
    _rebuild(lambda sql: re.sub(r'\)\s*$', f',\n\t{CHECK}\n)', sql))


def downgrade() -> None:
    _rebuild(lambda sql: re.sub(rf',\s*{re.escape(CHECK)}', '', sql))


def _rebuild(change: Callable[[str], str]) -> None:
    """Copy ``evaluations`` into a new table whose DDL is ``change`` of its own, and swap them."""
    bind = op.get_bind()
    sql = bind.execute(text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'evaluations'")).scalar_one()
    indexes = (
        bind.execute(
            text("SELECT sql FROM sqlite_master WHERE type = 'index' AND tbl_name = 'evaluations' AND sql IS NOT NULL")
        )
        .scalars()
        .all()
    )
    stored = bind.execute(text("SELECT name FROM pragma_table_xinfo('evaluations') WHERE hidden = 0")).scalars().all()
    counter = bind.execute(text("SELECT seq FROM sqlite_sequence WHERE name = 'evaluations'")).scalar()
    changed = change(sql)
    assert changed != sql, f'evaluations did not change: {sql}'
    columns = ', '.join(f'"{c}"' for c in stored)
    op.execute(re.sub(r'^CREATE TABLE "?evaluations"?', 'CREATE TABLE evaluations_new', changed))
    op.execute(f'INSERT INTO evaluations_new ({columns}) SELECT {columns} FROM evaluations')
    op.execute('DROP TABLE evaluations')
    op.execute('ALTER TABLE evaluations_new RENAME TO evaluations')
    if counter is not None:
        bind.execute(text("UPDATE sqlite_sequence SET seq = :seq WHERE name = 'evaluations'"), {'seq': counter})
    for each in indexes:
        op.execute(each)
    recreate(op, 'evaluations')
