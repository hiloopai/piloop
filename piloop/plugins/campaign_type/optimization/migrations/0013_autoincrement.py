"""``runs``, ``candidates``, ``ideas`` and ``measurements`` count their ids with AUTOINCREMENT.

Agents name them (``run#7``, ``candidate#30``, ``idea#12``), and a measurement's id names its units file. A daemon whose
term follows a restore lifts every AUTOINCREMENT sequence past the ids that the lost tail handed out
(``piloop.db.ids.lift``). The rows keep their ids. The downgrade rebuilds the tables without AUTOINCREMENT.

Revision ID: 0013
Revises: 0012
"""

from collections.abc import Sequence

from alembic import op

from piloop.db.ids import rebuild

revision: str = '0013'
down_revision: str | None = '0012'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ('runs', 'candidates', 'ideas', 'measurements')


def upgrade() -> None:
    for table in TABLES:
        rebuild(op, table, autoincrement=True)


def downgrade() -> None:
    for table in TABLES:
        rebuild(op, table, autoincrement=False)
