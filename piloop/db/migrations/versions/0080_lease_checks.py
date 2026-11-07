"""``leases`` gets CHECKs that tie a lease's state to its worker and its times (``LEASE_CHECKS``).

A raw ``piloop sql`` write is the only way to break them, and this repairs what one broke, in this order, and logs
how many rows it changed:

- a ``queued`` lease with a worker loses the worker: a worker takes it again;
- an ``acquired`` or ``running`` lease without a worker is ``cancelled``: nothing renews it, and the next daemon
  start fails or asks again for its job, as for any lease that it does not adopt;
- a worker without ``acquired_at`` gets ``created_at``, and an ``acquired_at`` without a worker is cleared;
- an ended lease without ``ended_at`` gets its last known time, and an open one with ``ended_at`` loses it;
- a ``running`` lease without ``started_at`` gets ``acquired_at``.

SQLite cannot add a CHECK, so the table is rebuilt with its rows, ids and indexes, and gets its triggers back. Its
AUTOINCREMENT counter is kept: a lease id is its fencing token, which never goes back. The downgrade drops the CHECKs.

Revision ID: 0080
Revises: 0079
"""

import logging
import re
from collections.abc import Callable, Sequence

from alembic import op
from sqlalchemy import text

from piloop.db.triggers import recreate

revision: str = '0080'
down_revision: str | None = '0079'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen here, as of this revision.
CHECKS = (
    'CONSTRAINT "leases.an ended lease has its end"'
    " CHECK ((state IN ('reported', 'expired', 'cancelled')) = (ended_at IS NOT NULL))",
    'CONSTRAINT "leases.a taken lease has its worker" CHECK ((worker IS NULL) = (acquired_at IS NULL))',
    'CONSTRAINT "leases.only a queued lease has no worker"'
    " CHECK (state = 'cancelled' OR (state = 'queued') = (worker IS NULL))",
    'CONSTRAINT "leases.a running lease has its start" CHECK (state != \'running\' OR started_at IS NOT NULL)',
)
REPAIRS = (
    "UPDATE leases SET worker = NULL, acquired_at = NULL WHERE state = 'queued' AND worker IS NOT NULL",
    "UPDATE leases SET state = 'cancelled', ended_at = COALESCE(ended_at, renewed_at, created_at),"
    " receipt = json_object('cancelled', 'it had no worker (migration 0080)')"
    " WHERE state IN ('acquired', 'running') AND worker IS NULL",
    'UPDATE leases SET acquired_at = created_at WHERE worker IS NOT NULL AND acquired_at IS NULL',
    'UPDATE leases SET acquired_at = NULL WHERE worker IS NULL AND acquired_at IS NOT NULL',
    'UPDATE leases SET ended_at = COALESCE(renewed_at, acquired_at, created_at)'
    " WHERE state IN ('reported', 'expired', 'cancelled') AND ended_at IS NULL",
    "UPDATE leases SET ended_at = NULL WHERE state IN ('queued', 'acquired', 'running') AND ended_at IS NOT NULL",
    "UPDATE leases SET started_at = acquired_at WHERE state = 'running' AND started_at IS NULL",
)


def upgrade() -> None:
    bind = op.get_bind()
    repaired = sum(bind.execute(text(each)).rowcount for each in REPAIRS)
    if repaired:
        logging.getLogger(__name__).warning(
            'migration 0080 repaired %d writes to leases that broke its CHECKs', repaired
        )
    _rebuild(lambda sql: re.sub(r'\)\s*$', ''.join(f',\n\t{c}' for c in CHECKS) + '\n)', sql))


def downgrade() -> None:
    _rebuild(lambda sql: re.sub(''.join(rf',\s*{re.escape(c)}' for c in CHECKS), '', sql))


def _rebuild(change: Callable[[str], str]) -> None:
    """Copy ``leases`` into a new table whose DDL is ``change`` of its own, and swap them."""
    bind = op.get_bind()
    sql = bind.execute(text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'leases'")).scalar_one()
    indexes = (
        bind.execute(
            text("SELECT sql FROM sqlite_master WHERE type = 'index' AND tbl_name = 'leases' AND sql IS NOT NULL")
        )
        .scalars()
        .all()
    )
    stored = bind.execute(text("SELECT name FROM pragma_table_xinfo('leases') WHERE hidden = 0")).scalars().all()
    counter = bind.execute(text("SELECT seq FROM sqlite_sequence WHERE name = 'leases'")).scalar()
    changed = change(sql)
    assert changed != sql, f'leases did not change: {sql}'
    columns = ', '.join(f'"{c}"' for c in stored)
    op.execute(re.sub(r'^CREATE TABLE "?leases"?', 'CREATE TABLE leases_new', changed))
    op.execute(f'INSERT INTO leases_new ({columns}) SELECT {columns} FROM leases')
    op.execute('DROP TABLE leases')
    op.execute('ALTER TABLE leases_new RENAME TO leases')
    if counter is not None:  # the next lease id stays past every one that a deleted campaign took with it
        bind.execute(text("UPDATE sqlite_sequence SET seq = :seq WHERE name = 'leases'"), {'seq': counter})
    for each in indexes:
        op.execute(each)
    recreate(op, 'leases')
