"""``workspace_snapshots``: the manifest of a campaign's shared workspace after each merge, so another host can rebuild it.

Revision ID: 0092
Revises: 0091
"""

from collections.abc import Sequence

from alembic import op

revision: str = '0092'
down_revision: str | None = '0091'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen here, as of this revision.
TABLE = """CREATE TABLE workspace_snapshots (
	id INTEGER NOT NULL,
	campaign_id INTEGER NOT NULL,
	manifest VARCHAR NOT NULL,
	at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (id)
)"""
INDEXES = ('CREATE INDEX ix_workspace_snapshots_campaign ON workspace_snapshots (campaign_id, id)',)


def upgrade() -> None:
    op.execute(TABLE)
    for index in INDEXES:
        op.execute(index)


def downgrade() -> None:
    op.execute('DROP TABLE workspace_snapshots')
