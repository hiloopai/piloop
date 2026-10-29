"""The table of secrets/local, ``secrets``, which core's revisions up to 0066 created.

It changes nothing. Each later revision of secrets/local changes it.

Revision ID: 0001
Revises:
"""

from collections.abc import Sequence

revision: str = '0001'
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
