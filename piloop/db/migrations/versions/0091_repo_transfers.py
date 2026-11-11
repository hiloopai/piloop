"""``repo_transfers``: the versions that each repo of record received, in the CAS, so another host can rebuild it.

Revision ID: 0091
Revises: 0090
"""

from collections.abc import Sequence

from alembic import op

revision: str = '0091'
down_revision: str | None = '0090'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen here, as of this revision.
TABLE = """CREATE TABLE repo_transfers (
	id INTEGER NOT NULL,
	campaign_id INTEGER NOT NULL,
	base VARCHAR NOT NULL,
	head VARCHAR NOT NULL,
	blob VARCHAR NOT NULL,
	received_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (id)
)"""
INDEXES = ('CREATE INDEX ix_repo_transfers_campaign ON repo_transfers (campaign_id, id)',)


def upgrade() -> None:
    op.execute(TABLE)
    for index in INDEXES:
        op.execute(index)


def downgrade() -> None:
    op.execute('DROP TABLE repo_transfers')
