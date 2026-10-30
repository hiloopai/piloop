"""The stored copy of each ``piloop.toml`` (``campaigns.config``) drops the keys of the removed features.

The bridges of ``piloop.deprecated`` drop them from the file: ``[plugins.role.scout]``, and the ``leaking_funnel``
trigger from its tables and from each role's ``triggers``, and ``files`` and ``plan_words`` from each role that does
runs. The rows that those features wrote stay, such as the scouts' agent sessions and the ideas that implementers
filed. The downgrade puts nothing back: 0.5 reads each dropped key as its default.

Revision ID: 0074
Revises: 0073
"""

import json
from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = '0074'
down_revision: str | None = '0073'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LEAKING_FUNNEL = 'leaking_funnel'
IMPLEMENTER = 'implementer'
RUN_ROLE_KEYS = ('files', 'plan_words')


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
    """Return a stored config without the keys of the removed features. Frozen here, as of this revision."""
    plugins = config.get('plugins')
    if not isinstance(plugins, dict):
        return config
    triggers = plugins.get('trigger')
    triggers = triggers if isinstance(triggers, dict) else {}
    funnels = {n for n, t in triggers.items() if isinstance(t, dict) and t.get('provider', n) == LEAKING_FUNNEL}
    for name in funnels:
        del triggers[name]
    if LEAKING_FUNNEL not in triggers:
        funnels.add(LEAKING_FUNNEL)
    roles = plugins.get('role')
    if isinstance(roles, dict):
        roles.pop('scout', None)
        for name, role in roles.items():
            if not isinstance(role, dict):
                continue
            if isinstance(role.get('triggers'), list):
                role['triggers'] = [t for t in role['triggers'] if t not in funnels]
            if name == IMPLEMENTER or role.get('runs') is True:
                for key in RUN_ROLE_KEYS:
                    role.pop(key, None)
    return config
