"""A candidate's origin may be ``merge``: the accumulating campaign type's merge of submissions onto its trunk.

The CHECK of ``candidates.origin`` takes the new value. The downgrade makes each ``merge`` candidate a ``reference``,
which every campaign type keeps and none submits: its measurements stay.

Revision ID: 0016
Revises: 0015
"""

from collections.abc import Sequence

from alembic import op

from piloop.db.ids import rewrite

revision: str = '0016'
down_revision: str | None = '0015'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen here, as of this revision.
BEFORE = "origin IN ('baseline', 'run', 'reference', 'recovered', 'skipped')"
AFTER = "origin IN ('baseline', 'run', 'reference', 'recovered', 'skipped', 'merge')"


def _check(old: str, new: str) -> None:
    def edit(sql: str) -> str:
        assert sql.count(old) == 1, f'candidates: no CHECK {old}'
        return sql.replace(old, new)

    rewrite(op, 'candidates', edit)


def upgrade() -> None:
    _check(BEFORE, AFTER)


def downgrade() -> None:
    op.execute("UPDATE candidates SET origin = 'reference' WHERE origin = 'merge'")
    _check(AFTER, BEFORE)
