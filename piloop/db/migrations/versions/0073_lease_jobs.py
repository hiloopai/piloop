"""A lease names its job by kind and row, ``leases.job_id``, and what a worker needs to take it, ``leases.runner``.

The kind is any job kind's name, not only ``agent`` or ``evaluation``, so its enum CHECK goes, and ``evaluation_id``
becomes ``job_id``. SQLite cannot change a CHECK, so the table is rebuilt with its rows, ids and indexes. The runner of
an evaluation's lease comes from its evaluator, where its row of the queue is still there: only a queued lease reads it,
and a queued evaluation always has its row. The live limits of a pool's evaluations move to ``slots.evaluation``.

The downgrade refuses a lease of a kind that 0.4 does not have.

Revision ID: 0073
Revises: 0072
"""

import re
from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = '0073'
down_revision: str | None = '0072'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_COLUMN = '\tevaluation_id INTEGER, \n'
NEW_COLUMNS = '\tjob_id INTEGER, \n\trunner VARCHAR, \n'
OLD_KIND = "\tCONSTRAINT leasekind CHECK (kind IN ('agent', 'evaluation')), \n"
OLD_CHECK = "(kind = 'agent') = (agent_session_id IS NOT NULL) AND (kind = 'evaluation') = (evaluation_id IS NOT NULL)"
NEW_CHECK = "(kind = 'agent') = (agent_session_id IS NOT NULL) AND (kind = 'agent') = (job_id IS NULL)"
OLD_INDEX = 'CREATE INDEX ix_leases_evaluation ON leases (evaluation_id)'
NEW_INDEX = 'CREATE INDEX ix_leases_job ON leases (kind, job_id)'
RUNNER = (
    '(SELECT evaluators.runner FROM evaluations JOIN evaluators ON evaluators.id = evaluations.evaluator_id'
    ' WHERE evaluations.id = leases.evaluation_id)'
)
POOL = r'^executor\.pools\.[^.]+\.'


def upgrade() -> None:
    _rebuild(
        [(OLD_COLUMN, NEW_COLUMNS), (OLD_KIND, ''), (OLD_CHECK, NEW_CHECK)],
        {'job_id': 'evaluation_id', 'runner': RUNNER},
        {'evaluation_id'},
        (OLD_INDEX, NEW_INDEX),
    )
    _limits('evaluations', 'slots.evaluation')


def downgrade() -> None:
    bind = op.get_bind()
    other = bind.execute(text("SELECT kind FROM leases WHERE kind NOT IN ('agent', 'evaluation') LIMIT 1")).scalar()
    if other is not None:
        raise RuntimeError(f'a lease of the job kind {other!r}, which piloop 0.4 does not run: end it first')
    _rebuild(
        [
            (NEW_COLUMNS, OLD_COLUMN),
            (NEW_CHECK, OLD_CHECK),
            ('\tCONSTRAINT "leases.a', OLD_KIND + '\tCONSTRAINT "leases.a'),
        ],
        {'evaluation_id': 'job_id'},
        {'job_id', 'runner'},
        (NEW_INDEX, OLD_INDEX),
    )
    _limits('slots.evaluation', 'evaluations')


def _limits(old: str, new: str) -> None:
    """Move each live limit ``executor.pools.<pool>.<old>`` to ``<new>``."""
    bind = op.get_bind()
    for key in bind.execute(text('SELECT key FROM limits')).scalars().all():
        if re.fullmatch(POOL + re.escape(old), key):
            moved = key[: -len(old)] + new
            op.execute(text('UPDATE limits SET key = :moved WHERE key = :key').bindparams(moved=moved, key=key))


def _rebuild(swaps: list[tuple[str, str]], made: dict[str, str], dropped: set[str], index: tuple[str, str]) -> None:
    """Copy ``leases`` into a new table with ``swaps`` made to its DDL, and swap them.

    The columns of ``made`` take their SQL expression over the old row; those of ``dropped`` are not copied.
    """
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
    for old, new in swaps:
        assert sql.count(old) == 1, f'leases has no single {old!r}: {sql}'
        sql = sql.replace(old, new)
    kept = [c for c in stored if c not in dropped]
    columns = ', '.join(f'"{c}"' for c in (*kept, *made))
    values = ', '.join([*(f'"{c}"' for c in kept), *made.values()])
    op.execute(re.sub(r'^CREATE TABLE "?leases"?', 'CREATE TABLE leases_new', sql))
    op.execute(f'INSERT INTO leases_new ({columns}) SELECT {values} FROM leases')
    op.execute('DROP TABLE leases')
    op.execute('ALTER TABLE leases_new RENAME TO leases')
    for each in indexes:
        op.execute(index[1] if each == index[0] else each)
