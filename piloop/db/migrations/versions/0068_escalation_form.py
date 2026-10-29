"""``comments.form``: an escalation's typed payload, which the campaign's escalation provider reads.

Revision ID: 0068
Revises: 0067
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0068'
down_revision: str | None = '0067'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('comments', sa.Column('form', sa.JSON(), nullable=True))


def downgrade() -> None:
    op.execute('ALTER TABLE comments DROP COLUMN form')  # in place: a batch rebuild would drop the table's triggers
