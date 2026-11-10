"""``first_transfers`` holds requests and failed builds too: any process asks for a first transfer, the daemon builds it.

The table only caches blobs that the next request builds again, so it is made anew: each repo's first transfer is
built once more after the upgrade. The downgrade makes the old table, empty, for the same reason.

Revision ID: 0002
Revises: 0001
"""

from collections.abc import Sequence

from alembic import op

revision: str = '0002'
down_revision: str | None = '0001'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen here, as of this revision.
REQUESTS = """CREATE TABLE first_transfers (
	campaign_id INTEGER NOT NULL,
	state VARCHAR(16) NOT NULL,
	started_at DATETIME NOT NULL,
	revision VARCHAR,
	blob VARCHAR,
	error VARCHAR,
	PRIMARY KEY (campaign_id),
	CONSTRAINT "first_transfers.a built one names it" CHECK ((state = 'built') = (revision IS NOT NULL AND blob IS NOT NULL)),
	CONSTRAINT "first_transfers.a failed one says why" CHECK ((state = 'failed') = (error IS NOT NULL)),
	CONSTRAINT "first_transfers.revision must be a text value" CHECK (typeof(revision) IN ('text', 'null')),
	CONSTRAINT "first_transfers.blob must be a text value" CHECK (typeof(blob) IN ('text', 'null')),
	CONSTRAINT "first_transfers.error must be a text value" CHECK (typeof(error) IN ('text', 'null')),
	FOREIGN KEY(campaign_id) REFERENCES campaigns (id) ON DELETE CASCADE,
	CONSTRAINT firsttransferstate CHECK (state IN ('requested', 'built', 'failed'))
)"""
BUILT = """CREATE TABLE first_transfers (
	campaign_id INTEGER NOT NULL,
	revision VARCHAR NOT NULL,
	blob VARCHAR NOT NULL,
	PRIMARY KEY (campaign_id),
	CONSTRAINT "first_transfers.revision must be a text value" CHECK (typeof(revision) IN ('text')),
	CONSTRAINT "first_transfers.blob must be a text value" CHECK (typeof(blob) IN ('text')),
	FOREIGN KEY(campaign_id) REFERENCES campaigns (id) ON DELETE CASCADE
)"""


def upgrade() -> None:
    op.execute('DROP TABLE first_transfers')
    op.execute(REQUESTS)


def downgrade() -> None:
    op.execute('DROP TABLE first_transfers')
    op.execute(BUILT)
