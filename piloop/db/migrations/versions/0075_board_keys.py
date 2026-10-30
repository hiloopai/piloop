"""The stored copy of each ``piloop.toml`` (``campaigns.config``) drops the keys of seeds and of reviews.

An idea is open on the board from the start: there are no seeds and no ``proposed`` ideas. The bridges of
``piloop.deprecated`` drop the same keys from the file: the ``seed`` trigger from its tables and from each role's
``triggers``, ``idle_alert`` from each role, and ``seeds`` from ``[plugins.section.pack]``. ``files = "seeds"`` becomes
``files = "nothing"``. The downgrade puts nothing back: 0.5 reads each dropped key as its default.

Revision ID: 0075
Revises: 0074
"""

import json
from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = '0075'
down_revision: str | None = '0074'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SEED = 'seed'


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
    """Return a stored config without the keys of seeds and of reviews. Frozen here, as of this revision."""
    plugins = config.get('plugins')
    if not isinstance(plugins, dict):
        return config
    triggers = plugins.get('trigger')
    triggers = triggers if isinstance(triggers, dict) else {}
    seeds = {n for n, t in triggers.items() if isinstance(t, dict) and t.get('provider', n) == SEED}
    for name in seeds:
        del triggers[name]
    seeds.add(SEED)
    roles = plugins.get('role')
    for role in roles.values() if isinstance(roles, dict) else ():
        if not isinstance(role, dict):
            continue
        if isinstance(role.get('triggers'), list):
            role['triggers'] = [t for t in role['triggers'] if t not in seeds]
        role.pop('idle_alert', None)
        if role.get('files') == 'seeds':
            role['files'] = 'nothing'
    sections = plugins.get('section')
    pack = sections.get('pack') if isinstance(sections, dict) else None
    if isinstance(pack, dict):
        pack.pop('seeds', None)
    return config
