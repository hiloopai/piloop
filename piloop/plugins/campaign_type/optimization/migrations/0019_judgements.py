"""The best rule's answers: ``judgements``, one row for each case that the rule answered.

The best of a track becomes a fold over these rows. Nothing is backfilled: the daemon's first pass asks the rule for
each case that the fold of a campaign needs, also for a stopped campaign.

Revision ID: 0019
Revises: 0018
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0019'
down_revision: str | None = '0018'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'judgements',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('candidate_id', sa.Integer(), nullable=False),
        sa.Column('incumbent_id', sa.Integer(), nullable=False),
        sa.Column('evaluator_id', sa.Integer(), nullable=False),
        sa.Column('key', sa.String(), nullable=False),
        sa.Column(
            'answer',
            sa.Enum(
                'keep', 'refuse', 'unanswered', name='answer', native_enum=False, length=16, create_constraint=True
            ),
            nullable=False,
        ),
        sa.Column('reason', sa.String(), nullable=True),
        sa.Column('seconds', sa.Double(), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.CheckConstraint("typeof(key) IN ('text')", name='judgements.key must be a text value'),
        sa.CheckConstraint("typeof(reason) IN ('text', 'null')", name='judgements.reason must be a text value'),
        sa.ForeignKeyConstraint(['candidate_id'], ['candidates.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['evaluator_id'], ['evaluators.id']),
        sa.ForeignKeyConstraint(['incumbent_id'], ['candidates.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('candidate_id', 'key'),
        sqlite_autoincrement=True,
    )


def downgrade() -> None:
    op.drop_table('judgements')
