"""``evaluations`` gets ``ask``: the remeasure that an evaluation answers.

0.6 answered a remeasure with any measurement of the candidate under the current evaluator, from an evaluation that ran
before the ask too. Now only the evaluation that the ask's request made answers it. A row of a previous daemon has no
ask: a daemon that starts clears the queue anyway. The downgrade drops the column.

Revision ID: 0012
Revises: 0011
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0012'
down_revision: str | None = '0011'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('evaluations', sa.Column('ask', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('evaluations', 'ask')
