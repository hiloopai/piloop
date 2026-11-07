"""``campaigns`` gets ``paused_by``, the owner of a pause, and CHECKs that tie a campaign's status to its stop and its
pause (``CAMPAIGN_CHECKS``).

A raw ``piloop sql`` write is the only way to break them, and this repairs what one broke, in this order, and logs
how many rows it changed:

- a paused campaign's owner is the actor of its latest ``campaign.paused`` event, else ``human``;
- an active campaign with a ``stopped_reason`` loses it;
- a stopped or retired campaign without one gets ``stopped by hand``;
- a retired campaign that is paused is not.

SQLite cannot add a CHECK, so the table is rebuilt with its rows, ids and indexes, and gets its triggers back. The
downgrade drops the CHECKs and the column.

Revision ID: 0081
Revises: 0080
"""

import logging
import re
from collections.abc import Callable, Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import text

from piloop.db.triggers import recreate

revision: str = '0081'
down_revision: str | None = '0080'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen here, as of this revision.
CHECKS = (
    'CONSTRAINT "campaigns.a pause has its owner" CHECK ((paused IS NULL) = (paused_by IS NULL))',
    'CONSTRAINT "campaigns.a stopped or retired campaign has its stopped_reason"'
    " CHECK ((status = 'active') = (stopped_reason IS NULL))",
    'CONSTRAINT "campaigns.a retired campaign is not paused" CHECK (status != \'retired\' OR paused IS NULL)',
)
OWNERS = (
    "UPDATE campaigns SET paused_by = COALESCE((SELECT actor FROM events WHERE target_kind = 'campaign'"
    " AND target_id = campaigns.id AND kind = 'campaign.paused' ORDER BY id DESC LIMIT 1), 'human')"
    ' WHERE paused IS NOT NULL'
)
REPAIRS = (
    "UPDATE campaigns SET stopped_reason = NULL WHERE status = 'active' AND stopped_reason IS NOT NULL",
    "UPDATE campaigns SET stopped_reason = 'stopped by hand' WHERE status != 'active' AND stopped_reason IS NULL",
    "UPDATE campaigns SET paused = NULL, paused_reason = NULL, paused_by = NULL WHERE status = 'retired'"
    ' AND paused IS NOT NULL',
)


def upgrade() -> None:
    op.add_column('campaigns', sa.Column('paused_by', sa.String(), nullable=True))
    bind = op.get_bind()
    bind.execute(text(OWNERS))
    if repaired := sum(bind.execute(text(each)).rowcount for each in REPAIRS):
        logging.getLogger(__name__).warning(
            'migration 0081 repaired %d writes to campaigns that broke its CHECKs', repaired
        )
    _rebuild(lambda sql: re.sub(r'\)\s*$', ''.join(f',\n\t{c}' for c in CHECKS) + '\n)', sql))


def downgrade() -> None:
    _rebuild(lambda sql: re.sub(''.join(rf',\s*{re.escape(c)}' for c in CHECKS), '', sql))
    op.execute('ALTER TABLE campaigns DROP COLUMN paused_by')


def _rebuild(change: Callable[[str], str]) -> None:
    """Copy ``campaigns`` into a new table whose DDL is ``change`` of its own, and swap them.

    The tables that reference ``campaigns`` keep their foreign keys: they name the table, which has its name again
    at the end. A migration's connection does not enforce foreign keys, so the drop leaves their rows alone.
    """
    bind = op.get_bind()
    sql = bind.execute(text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'campaigns'")).scalar_one()
    indexes = (
        bind.execute(
            text("SELECT sql FROM sqlite_master WHERE type = 'index' AND tbl_name = 'campaigns' AND sql IS NOT NULL")
        )
        .scalars()
        .all()
    )
    stored = bind.execute(text("SELECT name FROM pragma_table_xinfo('campaigns') WHERE hidden = 0")).scalars().all()
    changed = change(sql)
    assert changed != sql, f'campaigns did not change: {sql}'
    columns = ', '.join(f'"{c}"' for c in stored)
    op.execute(re.sub(r'^CREATE TABLE "?campaigns"?', 'CREATE TABLE campaigns_new', changed))
    op.execute(f'INSERT INTO campaigns_new ({columns}) SELECT {columns} FROM campaigns')
    op.execute('DROP TABLE campaigns')
    # Triggers of other tables read campaigns, which is gone until the rename: the legacy rename leaves them unparsed.
    op.execute('PRAGMA legacy_alter_table = ON')
    op.execute('ALTER TABLE campaigns_new RENAME TO campaigns')
    op.execute('PRAGMA legacy_alter_table = OFF')
    for each in indexes:
        op.execute(each)
    recreate(op, 'campaigns')
