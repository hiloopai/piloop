"""``campaigns.clone_url`` and ``clone_secret``: where a cloned campaign came from, and the secret it was cloned with.

Another host clones it again from them after a restore (``piloop.daemon.rebuild``). ``campaigns.repo`` names the
repository, not a URL to clone. ``repo_transfers.base`` may be NULL: a fork of a retired campaign begins from a pack of
every version, which the vcs fetches, not receives. Campaigns cloned before keep NULL: piloop cannot tell their URL
from the database. The downgrade drops the columns, and the transfers of packs.

Revision ID: 0094
Revises: 0093
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0094'
down_revision: str | None = '0093'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COLUMNS = ('clone_url', 'clone_secret')


def upgrade() -> None:
    for column in COLUMNS:
        check = f"CONSTRAINT \"campaigns.{column} must be a text value\" CHECK (typeof({column}) IN ('text', 'null'))"
        op.execute(f'ALTER TABLE campaigns ADD COLUMN {column} VARCHAR {check}')
    with op.batch_alter_table('repo_transfers', recreate='always') as batch:
        batch.alter_column('base', existing_type=sa.String(), nullable=True)


def downgrade() -> None:
    op.execute('DELETE FROM repo_transfers WHERE base IS NULL')
    with op.batch_alter_table('repo_transfers', recreate='always') as batch:
        batch.alter_column('base', existing_type=sa.String(), nullable=False)
    for column in COLUMNS:
        op.execute(f'ALTER TABLE campaigns DROP COLUMN {column}')
