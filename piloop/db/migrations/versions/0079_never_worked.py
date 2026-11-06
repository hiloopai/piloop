"""``agent_sessions.state`` takes ``never_worked``: an agent whose job did not start, or that left nothing.

SQLite cannot change a CHECK constraint, so the table is rebuilt with its rows, ids and indexes, and gets its triggers
back. The rows that ended before stay as they are: a never-worked agent of an earlier release stays ``exited`` or
``killed``. The downgrade makes each ``never_worked`` row ``killed``, which keeps its error.

Revision ID: 0079
Revises: 0078
"""

import re
from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

from piloop.db.triggers import recreate

revision: str = '0079'
down_revision: str | None = '0078'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD = "CHECK (state IN ('requested', 'starting', 'running', 'ending', 'exited', 'killed'))"
NEW = "CHECK (state IN ('requested', 'starting', 'running', 'ending', 'exited', 'killed', 'never_worked'))"


def upgrade() -> None:
    _rebuild(OLD, NEW)


def downgrade() -> None:
    op.execute("UPDATE agent_sessions SET state = 'killed' WHERE state = 'never_worked'")
    _rebuild(NEW, OLD)


def _rebuild(old: str, new: str) -> None:
    """Copy ``agent_sessions`` into a new table whose DDL has ``new`` for ``old``, and swap them."""
    bind = op.get_bind()
    sql = bind.execute(
        text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'agent_sessions'")
    ).scalar_one()
    indexes = (
        bind.execute(
            text(
                "SELECT sql FROM sqlite_master WHERE type = 'index' AND tbl_name = 'agent_sessions' AND sql IS NOT NULL"
            )
        )
        .scalars()
        .all()
    )
    stored = (
        bind.execute(text("SELECT name FROM pragma_table_xinfo('agent_sessions') WHERE hidden = 0")).scalars().all()
    )
    assert sql.count(old) == 1, f'agent_sessions has no single {old!r}: {sql}'
    columns = ', '.join(f'"{c}"' for c in stored)
    op.execute(re.sub(r'^CREATE TABLE "?agent_sessions"?', 'CREATE TABLE agent_sessions_new', sql.replace(old, new)))
    op.execute(f'INSERT INTO agent_sessions_new ({columns}) SELECT {columns} FROM agent_sessions')
    op.execute('DROP TABLE agent_sessions')
    op.execute('ALTER TABLE agent_sessions_new RENAME TO agent_sessions')
    for each in indexes:
        op.execute(each)
    recreate(op, 'agent_sessions')
