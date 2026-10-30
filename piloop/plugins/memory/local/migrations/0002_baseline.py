"""This chain's revisions 0001 to 0002, squashed at 0.4's head. It changes nothing.

Core's baseline (``0067``) creates this chain's tables as these revisions left them: core's revisions created
them before this chain adopted them. A database at 0.4's head of this chain is at this revision already.

Revision ID: 0002
Revises:
"""

from collections.abc import Sequence

revision: str = '0002'
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SQUASHED = (
    '0001_adopt',
    '0002_journal_campaign',
)
"""The revisions that this one replaces, oldest first: the fingerprint of each revision after it counts their names
(``piloop.db.session.fingerprints``), so that it stays what databases recorded before the squash."""


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
