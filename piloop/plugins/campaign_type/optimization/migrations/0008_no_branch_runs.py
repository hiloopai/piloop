"""Drop ``runs.branch``: every run starts from the head and keeps on the trunk, so no run is a branch run.

A keep of a 0.5 branch run never moved the head. Without the column it would be a trunk keep, and could take the head.
So each one becomes ``interesting`` first, with ``BRANCH_KEEP`` at the end of its reason: the head stays where it is,
and the candidate stays in view as a lead. The downgrade gives the column back, and makes each such candidate its run's
branch keep again.

Revision ID: 0008
Revises: 0007
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from piloop.db.triggers import recreate

revision: str = '0008'
down_revision: str | None = '0007'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen here, as of this revision.
BRANCH_KEEP = '; a branch keep of 0.5, which never moved the head: interesting since 0.6'


def upgrade() -> None:
    op.execute(
        "UPDATE candidates SET verdict = 'interesting', verdict_reason = COALESCE(verdict_reason, '') || '"
        + BRANCH_KEEP
        + "' WHERE verdict = 'keep' AND run_id IN (SELECT id FROM runs WHERE branch)"
    )
    with op.batch_alter_table('runs', recreate='always') as batch:
        batch.drop_column('branch')
    recreate(op, 'runs')


def downgrade() -> None:
    with op.batch_alter_table('runs', recreate='always') as batch:
        batch.add_column(sa.Column('branch', sa.Boolean(), server_default=sa.text('0'), nullable=False))
    recreate(op, 'runs')
    demoted = f"verdict = 'interesting' AND verdict_reason LIKE '%{BRANCH_KEEP}'"
    op.execute(f'UPDATE runs SET branch = 1 WHERE id IN (SELECT run_id FROM candidates WHERE {demoted})')
    op.execute(
        f"UPDATE candidates SET verdict = 'keep', verdict_reason = substr(verdict_reason, 1,"
        f' length(verdict_reason) - {len(BRANCH_KEEP)}) WHERE {demoted}'
    )
