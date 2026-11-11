"""``job_files``: the log files that each job's report put under its place, by path, so another host can rebuild them.

Revision ID: 0093
Revises: 0092
"""

from collections.abc import Sequence

from alembic import op

revision: str = '0093'
down_revision: str | None = '0092'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen here, as of this revision.
TABLE = """CREATE TABLE job_files (
	id INTEGER NOT NULL,
	place VARCHAR NOT NULL,
	files VARCHAR NOT NULL,
	PRIMARY KEY (id)
)"""
INDEXES = ()


def upgrade() -> None:
    op.execute(TABLE)
    for index in INDEXES:
        op.execute(index)


def downgrade() -> None:
    op.execute('DROP TABLE job_files')
