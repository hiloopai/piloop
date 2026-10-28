"""A reference may have a verdict: someone kept it by hand.

The CHECK keeps its name: core's revision 0043 drops it by that name when it is downgraded.

Batch mode rebuilds the table for the CHECK. It keeps the row ids and drops the table's triggers, so ``recreate`` puts
them back.

Revision ID: 0003
Revises: 0002
"""

from collections.abc import Sequence

from alembic import op

from piloop.db.triggers import recreate

revision: str = '0003'
down_revision: str | None = '0002'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NAME = 'candidates.verdict needs a judged origin'


def _check(origins: str) -> None:
    with op.batch_alter_table('candidates', recreate='always') as batch:
        batch.drop_constraint(NAME, type_='check')
        batch.create_check_constraint(NAME, f'verdict IS NULL OR origin IN ({origins})')
    recreate(op, 'candidates')


def upgrade() -> None:
    _check("'baseline', 'reference', 'run'")


def downgrade() -> None:
    op.execute("UPDATE candidates SET verdict = NULL, cause = NULL, verdict_by = NULL WHERE origin = 'reference'")
    _check("'baseline', 'run'")
