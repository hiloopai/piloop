"""The stored copy of each ``piloop.toml`` (``campaigns.config``) drops the keys that assigned ideas to runs.

Workers claim their ideas, and each run starts from the head. The ``run_role_assignment`` and ``track_shares`` bridges
of ``piloop.deprecated`` drop the same keys from the file: ``takes``, ``explore_every`` and ``recovery_requeues`` of
each role, and ``share`` of each ``[tracks.<name>]``, with a table that it leaves empty. The downgrade puts nothing
back: the old release reads each dropped key as its default, but refuses several tracks without a table with their
shares.

Revision ID: 0076
Revises: 0075
"""

import json
from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = '0076'
down_revision: str | None = '0075'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen here, as of this revision.
KEYS = ('takes', 'explore_every', 'recovery_requeues')


def upgrade() -> None:
    bind = op.get_bind()
    for campaign_id, config in bind.execute(text('SELECT id, config FROM campaigns')).all():
        bind.execute(
            text('UPDATE campaigns SET config = :c WHERE id = :id'),
            {'c': json.dumps(dropped(json.loads(config))), 'id': campaign_id},
        )


def downgrade() -> None:
    pass


def dropped(config: dict[str, object]) -> dict[str, object]:
    """Return a stored config without the keys that assigned ideas to runs."""
    plugins = config.get('plugins')
    roles = plugins.get('role') if isinstance(plugins, dict) else None
    for role in roles.values() if isinstance(roles, dict) else ():
        for key in KEYS if isinstance(role, dict) else ():
            role.pop(key, None)
    tracks = config.get('tracks')
    for name, table in list(tracks.items()) if isinstance(tracks, dict) else ():
        if isinstance(table, dict):
            table.pop('share', None)
            if not table:
                del tracks[name]
    return config
