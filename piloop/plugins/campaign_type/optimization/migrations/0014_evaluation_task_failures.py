"""Keep infrastructure retry counts and evaluation holds per task within a campaign."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0014'
down_revision: str | None = '0013'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('candidates', sa.Column('task_id', sa.String(), nullable=True))
    op.create_table(
        'evaluation_task_failures',
        sa.Column('campaign_id', sa.Integer(), sa.ForeignKey('campaigns.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('task_key', sa.String(), primary_key=True),
        sa.Column('failures', sa.Integer(), nullable=False),
        sa.Column('held', sa.Boolean(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table('evaluation_task_failures')
    op.drop_column('candidates', 'task_id')
