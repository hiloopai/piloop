"""Each party acknowledges an inbox item for itself: ``acks`` replaces ``comments.acked_at`` and ``acked_by``.

An item that was acknowledged before this revision left every inbox, so it gets an ack for each party that reads the
inbox, the people (``human``) and the orchestrator, signed and timed as it was. The ``ix_comments_inbox`` index loses
its ``acked_at`` condition. The downgrade puts each item's earliest ack back on its row, and drops the table.

Revision ID: 0084
Revises: 0083
"""

from collections.abc import Sequence

from alembic import op

revision: str = '0084'
down_revision: str | None = '0083'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen here, as of this revision.
ACKS = """CREATE TABLE acks (
	comment_id INTEGER NOT NULL,
	party VARCHAR NOT NULL,
	acked_by VARCHAR NOT NULL,
	acked_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (comment_id, party),
	CONSTRAINT "acks.party must be a text value" CHECK (typeof(party) IN ('text')),
	CONSTRAINT "acks.acked_by must be a text value" CHECK (typeof(acked_by) IN ('text')),
	FOREIGN KEY(comment_id) REFERENCES comments (id) ON DELETE CASCADE
)"""
READERS = ('human', 'orchestrator')


def upgrade() -> None:
    op.execute(ACKS)
    for party in READERS:
        op.execute(
            f"INSERT INTO acks (comment_id, party, acked_by, acked_at) SELECT id, '{party}',"
            " COALESCE(acked_by, 'human'), acked_at FROM comments WHERE acked_at IS NOT NULL"
        )
    op.execute('DROP INDEX ix_comments_inbox')
    op.execute('CREATE INDEX ix_comments_inbox ON comments (priority, id) WHERE priority IS NOT NULL')
    op.execute('ALTER TABLE comments DROP COLUMN acked_at')
    op.execute('ALTER TABLE comments DROP COLUMN acked_by')


def downgrade() -> None:
    op.execute('ALTER TABLE comments ADD COLUMN acked_at DATETIME')
    op.execute('ALTER TABLE comments ADD COLUMN acked_by VARCHAR')
    first = 'FROM acks WHERE acks.comment_id = comments.id ORDER BY acked_at, party LIMIT 1'
    op.execute(f'UPDATE comments SET acked_at = (SELECT acked_at {first}), acked_by = (SELECT acked_by {first})')
    op.execute('DROP INDEX ix_comments_inbox')
    op.execute(
        'CREATE INDEX ix_comments_inbox ON comments (priority, id) WHERE priority IS NOT NULL AND acked_at IS NULL'
    )
    op.execute('DROP TABLE acks')
