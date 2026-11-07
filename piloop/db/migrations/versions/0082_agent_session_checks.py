"""``agent_sessions`` gets AUTOINCREMENT and CHECKs that tie an agent's state to its end (``AGENT_SESSION_CHECKS``).

A raw ``piloop sql`` write is the only way to break the CHECKs, and this repairs what one broke, in this order, and logs
how many rows it changed:

- a closed agent (``exited``, ``killed``, ``never_worked``) without ``ended_at`` gets its last known time;
- an open one with ``ended_at`` loses it;
- a ``never_worked`` agent without an error gets one that says the migration gave it.

SQLite cannot add a CHECK or AUTOINCREMENT, so the table is rebuilt with its rows, ids and indexes, and gets its
triggers back. Its counter starts past every id that names an agent anywhere (a token, a lease, an event): a deleted
agent's id is never used again. The downgrade drops the CHECKs and AUTOINCREMENT.

Revision ID: 0082
Revises: 0081
"""

import logging
import re
from collections.abc import Callable, Sequence

from alembic import op
from sqlalchemy import text

from piloop.db.triggers import recreate

revision: str = '0082'
down_revision: str | None = '0081'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen here, as of this revision.
CHECKS = (
    'CONSTRAINT "agent_sessions.an ended agent has its end"'
    " CHECK ((state IN ('exited', 'killed', 'never_worked')) = (ended_at IS NOT NULL))",
    'CONSTRAINT "agent_sessions.a never-worked agent says why" CHECK (state != \'never_worked\' OR error IS NOT NULL)',
)
REPAIRS = (
    'UPDATE agent_sessions SET ended_at = COALESCE(updated_at, started_at)'
    " WHERE state IN ('exited', 'killed', 'never_worked') AND ended_at IS NULL",
    "UPDATE agent_sessions SET ended_at = NULL WHERE state IN ('requested', 'starting', 'running', 'ending')"
    ' AND ended_at IS NOT NULL',
    "UPDATE agent_sessions SET error = 'it did not start to work (migration 0082)'"
    " WHERE state = 'never_worked' AND error IS NULL",
)
KEY = 'id INTEGER NOT NULL, '
AUTOINCREMENT = 'id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, '
PRIMARY_KEY = re.compile(r'\n\tPRIMARY KEY \(id\), ')
SPENT = (
    'SELECT max(id) FROM (SELECT max(id) AS id FROM agent_sessions'
    ' UNION ALL SELECT max(agent_session_id) FROM tokens'
    ' UNION ALL SELECT max(agent_session_id) FROM leases'
    " UNION ALL SELECT max(target_id) FROM events WHERE target_kind = 'agent_session')"
)


def upgrade() -> None:
    bind = op.get_bind()
    if repaired := sum(bind.execute(text(each)).rowcount for each in REPAIRS):
        logging.getLogger(__name__).warning(
            'migration 0082 repaired %d writes to agent_sessions that broke its CHECKs', repaired
        )
    spent = bind.execute(text(SPENT)).scalar()
    _rebuild(_up)
    if spent is not None:  # the next id goes past a deleted agent's, which a token or an event may still name
        op.execute("DELETE FROM sqlite_sequence WHERE name = 'agent_sessions'")
        bind.execute(text("INSERT INTO sqlite_sequence (name, seq) VALUES ('agent_sessions', :seq)"), {'seq': spent})


def downgrade() -> None:
    _rebuild(_down)
    op.execute("DELETE FROM sqlite_sequence WHERE name = 'agent_sessions'")


def _up(sql: str) -> str:
    assert sql.count(KEY) == 1 and PRIMARY_KEY.search(sql), f'agent_sessions has no single id key: {sql}'
    sql = PRIMARY_KEY.sub('\n\t', sql.replace(KEY, AUTOINCREMENT))
    return re.sub(r'\)\s*$', ''.join(f',\n\t{c}' for c in CHECKS) + '\n)', sql)


def _down(sql: str) -> str:
    sql = re.sub(''.join(rf',\s*{re.escape(c)}' for c in CHECKS), '', sql)
    return sql.replace(AUTOINCREMENT, KEY).replace('\n\tCONSTRAINT', '\n\tPRIMARY KEY (id), \n\tCONSTRAINT', 1)


def _rebuild(change: Callable[[str], str]) -> None:
    """Copy ``agent_sessions`` into a new table whose DDL is ``change`` of its own, and swap them."""
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
    changed = change(sql)
    assert changed != sql, f'agent_sessions did not change: {sql}'
    columns = ', '.join(f'"{c}"' for c in stored)
    op.execute(re.sub(r'^CREATE TABLE "?agent_sessions"?', 'CREATE TABLE agent_sessions_new', changed))
    op.execute(f'INSERT INTO agent_sessions_new ({columns}) SELECT {columns} FROM agent_sessions')
    op.execute('DROP TABLE agent_sessions')
    op.execute('ALTER TABLE agent_sessions_new RENAME TO agent_sessions')
    for each in indexes:
        op.execute(each)
    recreate(op, 'agent_sessions')
