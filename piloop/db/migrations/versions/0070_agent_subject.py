"""An agent session works on a subject, ``agent_sessions.subject_kind`` and ``subject_id``, such as ``run#12``.

``run_id`` goes. It had a foreign key, which SQLite cannot drop, so the table is rebuilt with its rows, ids and
indexes, and gets its triggers back. The stored ``agent_session.identified`` events name the subject, the groups and
the parent in place of ``run``, ``idea`` and ``proposed_by``, and ``anomaly.startup`` the subject in place of ``run``.

The downgrade turns each ``run`` subject back into ``run_id``, drops the subjects of other kinds, and gives the events
their keys of 0.4 back.

Revision ID: 0070
Revises: 0069
"""

import re
from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

from piloop.db.triggers import recreate

revision: str = '0070'
down_revision: str | None = '0069'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_COLUMN = '\trun_id INTEGER, \n'
NEW_COLUMNS = '\tsubject_kind VARCHAR(16), \n\tsubject_id INTEGER, \n'
OLD_KEY = 'FOREIGN KEY(run_id) REFERENCES runs (id) ON DELETE SET NULL'
NEW_KEY = (
    'CONSTRAINT agent_sessions_kind FOREIGN KEY(subject_kind) REFERENCES target_kinds (id), \n'
    '\tCONSTRAINT agent_sessions_subject CHECK ((subject_kind IS NULL) = (subject_id IS NULL))'
)
OLD_INDEX = 'CREATE INDEX ix_agent_sessions_run ON agent_sessions (run_id)'
NEW_INDEX = 'CREATE INDEX ix_agent_sessions_subject ON agent_sessions (subject_kind, subject_id)'
IDENTIFIED = "kind = 'agent_session.identified'"
STARTUP = "kind = 'anomaly.startup'"
RUN = "CASE WHEN json_type(payload, '$.run') = 'integer' THEN 'run#' || json_extract(payload, '$.run') END"
IDEA = (
    "json(CASE WHEN json_type(payload, '$.idea') = 'integer'"
    " THEN json_array('idea#' || json_extract(payload, '$.idea')) ELSE '[]' END)"
)
PROPOSER = (
    "json(CASE WHEN json_type(payload, '$.proposed_by') = 'object' THEN json_extract(payload, '$.proposed_by') END)"
)
SUBJECT_RUN = (
    "CASE WHEN json_extract(payload, '$.subject') LIKE 'run#%'"
    " THEN CAST(substr(json_extract(payload, '$.subject'), 5) AS INTEGER) END"
)
GROUP_IDEA = (
    "CASE WHEN json_extract(payload, '$.groups[0]') LIKE 'idea#%'"
    " THEN CAST(substr(json_extract(payload, '$.groups[0]'), 6) AS INTEGER) END"
)
PARENT = "json(CASE WHEN json_type(payload, '$.parent') = 'object' THEN json_extract(payload, '$.parent') END)"


def upgrade() -> None:
    _rebuild(
        [(OLD_COLUMN, NEW_COLUMNS), (OLD_KEY, NEW_KEY)],
        {'subject_kind': "CASE WHEN run_id IS NOT NULL THEN 'run' END", 'subject_id': 'run_id'},
        {'run_id'},
        (OLD_INDEX, NEW_INDEX),
    )
    op.execute(
        "UPDATE events SET payload = json_remove(json_set(payload, '$.subject', "
        f"{RUN}, '$.groups', {IDEA}, '$.parent', {PROPOSER}), '$.run', '$.idea', '$.proposed_by') WHERE {IDENTIFIED}"
    )
    op.execute(
        f"UPDATE events SET payload = json_remove(json_set(payload, '$.subject', {RUN}), '$.run') WHERE {STARTUP}"
    )


def downgrade() -> None:
    _rebuild(
        [(NEW_COLUMNS, OLD_COLUMN), (NEW_KEY, OLD_KEY)],
        {'run_id': "CASE WHEN subject_kind = 'run' THEN subject_id END"},
        {'subject_kind', 'subject_id'},
        (NEW_INDEX, OLD_INDEX),
    )
    op.execute(
        "UPDATE events SET payload = json_remove(json_set(payload, '$.run', "
        f"{SUBJECT_RUN}, '$.idea', {GROUP_IDEA}, '$.proposed_by', {PARENT}), '$.subject', '$.groups', '$.parent')"
        f' WHERE {IDENTIFIED}'
    )
    op.execute(
        f"UPDATE events SET payload = json_remove(json_set(payload, '$.run', {SUBJECT_RUN}), '$.subject') WHERE {STARTUP}"
    )


def _rebuild(swaps: list[tuple[str, str]], made: dict[str, str], dropped: set[str], index: tuple[str, str]) -> None:
    """Copy ``agent_sessions`` into a new table with ``swaps`` made to its DDL, and swap them.

    The columns of ``made`` take their SQL expression over the old row; those of ``dropped`` are not copied.
    """
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
    for old, new in swaps:
        assert sql.count(old) == 1, f'agent_sessions has no single {old!r}: {sql}'
        sql = sql.replace(old, new)
    kept = [c for c in stored if c not in dropped]
    columns = ', '.join(f'"{c}"' for c in (*kept, *made))
    values = ', '.join([*(f'"{c}"' for c in kept), *made.values()])
    op.execute(re.sub(r'^CREATE TABLE "?agent_sessions"?', 'CREATE TABLE agent_sessions_new', sql))
    op.execute(f'INSERT INTO agent_sessions_new ({columns}) SELECT {values} FROM agent_sessions')
    op.execute('DROP TABLE agent_sessions')
    op.execute('ALTER TABLE agent_sessions_new RENAME TO agent_sessions')
    for each in indexes:
        op.execute(index[1] if each == index[0] else each)
    recreate(op, 'agent_sessions')
