"""``terms``: each daemon's term on the home, which every process that serves its API reads.

Revision ID: 0089
Revises: 0088
"""

from collections.abc import Sequence

from alembic import op

revision: str = '0089'
down_revision: str | None = '0088'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen here, as of this revision.
TERMS = """CREATE TABLE terms (
	epoch INTEGER NOT NULL,
	pid INTEGER NOT NULL,
	listen VARCHAR,
	started_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	stopping_at DATETIME,
	PRIMARY KEY (epoch)
)"""


def upgrade() -> None:
    op.execute(TERMS)


def downgrade() -> None:
    op.execute('DROP TABLE terms')
