"""``owners``: this server's owner id moves from the file ``<home>/owner`` into the database.

The id tags every instance that a pool's provisioner starts. In the database, a home restored on another host keeps it,
so its instances stay its own. The upgrade copies the file beside the database when there is one, and leaves the file:
nothing reads it any more. The downgrade writes the file back from the row, and drops the table.

Revision ID: 0003
Revises: 0002
"""

from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision: str = '0003'
down_revision: str | None = '0002'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen here, as of this revision.
OWNERS = """CREATE TABLE owners (
	id VARCHAR NOT NULL,
	PRIMARY KEY (id)
)"""
FILE = 'owner'


def _file() -> Path | None:
    """``<home>/owner``: the home is the directory of the database file. None for a database in memory."""
    database = op.get_bind().engine.url.database
    return None if not database or database == ':memory:' else Path(database).parent / FILE


def upgrade() -> None:
    op.execute(OWNERS)
    path = _file()
    if path is not None and path.is_file() and (found := path.read_text().strip()):
        op.get_bind().execute(sa.text('INSERT INTO owners (id) VALUES (:id)'), {'id': found})


def downgrade() -> None:
    found = op.get_bind().execute(sa.text('SELECT id FROM owners')).scalar()
    path = _file()
    if found is not None and path is not None and not path.exists():
        path.write_text(found)
    op.execute('DROP TABLE owners')
