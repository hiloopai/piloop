"""``events`` and ``comments`` count their ids with AUTOINCREMENT, so that a term can lift them past a restore.

An event id is a cursor and a comment id is what an ack names: a worker or a client holds them. A daemon whose term
follows a restore lifts every AUTOINCREMENT sequence past the ids that the lost tail handed out
(``piloop.db.ids.lift``). The rows keep their ids. The downgrade rebuilds the tables without AUTOINCREMENT.

Revision ID: 0090
Revises: 0089
"""

from collections.abc import Sequence

from alembic import op

from piloop.db.ids import rebuild

revision: str = '0090'
down_revision: str | None = '0089'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ('events', 'comments')


def upgrade() -> None:
    for table in TABLES:
        rebuild(op, table, autoincrement=True)


def downgrade() -> None:
    for table in TABLES:
        rebuild(op, table, autoincrement=False)
