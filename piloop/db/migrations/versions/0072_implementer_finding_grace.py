"""A campaign's ``finding_grace`` is its implementer's: ``plugins.role.implementer.finding_grace``.

The stored copy of each ``piloop.toml`` (``campaigns.config``) moves the key as ``piloop.deprecated`` moves the file's.
A copy that 0.4 stored dumped every key of ``agents``, those that the file did not set as null: they go first, as a
null sets nothing. The downgrade moves the key back.

Revision ID: 0072
Revises: 0071
"""

import json
from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

from piloop.deprecated import Move, Prune, Step

revision: str = '0072'
down_revision: str | None = '0071'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen here, as of this revision.
MOVE = Move('agents.finding_grace', 'plugins.role.implementer.finding_grace')
STEPS: tuple[Step, ...] = (Prune('agents'), MOVE)


def upgrade() -> None:
    _rewrite(STEPS)


def downgrade() -> None:
    _rewrite((MOVE.inverse(),))  # a pruned key held nothing: nothing to put back


def _rewrite(steps: Sequence[Step]) -> None:
    bind = op.get_bind()
    for campaign_id, config in bind.execute(text('SELECT id, config FROM campaigns')).all():
        bind.execute(
            text('UPDATE campaigns SET config = :c WHERE id = :id'),
            {'c': json.dumps(moved(json.loads(config), steps)), 'id': campaign_id},
        )


def moved(config: dict[str, object], steps: Sequence[Step] = STEPS) -> dict[str, object]:
    """Return a stored config with ``steps`` applied: by default, in the shape of this revision."""
    for step in steps:
        step.apply(config)
    return config
