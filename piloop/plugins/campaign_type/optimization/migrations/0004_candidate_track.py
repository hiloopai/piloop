"""``candidates.track``: the track of the daemon's verdict on a candidate, which it judges on every track. A verdict
before it was on the candidate's run's track: NULL.

Batch mode rebuilds the table for the CHECK. It keeps the row ids and drops the table's triggers, so ``recreate`` puts
them back.

Revision ID: 0004
Revises: 0003
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from piloop.db.triggers import recreate

revision: str = '0004'
down_revision: str | None = '0003'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TEXT = 'candidates.track must be a text value'


def upgrade() -> None:
    with op.batch_alter_table('candidates', recreate='always') as batch:
        batch.add_column(sa.Column('track', sa.String(), nullable=True))
        batch.create_check_constraint(TEXT, "typeof(track) IN ('text', 'null')")
    recreate(op, 'candidates')


def downgrade() -> None:
    with op.batch_alter_table('candidates', recreate='always') as batch:
        batch.drop_constraint(TEXT, type_='check')
        batch.drop_column('track')
    recreate(op, 'candidates')
