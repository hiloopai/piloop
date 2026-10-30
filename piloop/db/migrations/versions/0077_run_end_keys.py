"""A kind of comment does nothing when it is written: ``comment_kinds.after_end`` goes, and ``finding_grace`` is
``end_grace`` in each stored ``piloop.toml`` (``campaigns.config``).

A finding ended a run, so a late one was refused. A worker ends its run with ``piloop run end`` now. The
``end_grace`` bridge of ``piloop.deprecated`` renames the key in the file. The downgrade gives each kind back
``kept``, ``finding`` ``refused``, and the old key name.

Revision ID: 0077
Revises: 0076
"""

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import text

revision: str = '0077'
down_revision: str | None = '0076'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen here, as of this revision.
OLD, NEW = 'finding_grace', 'end_grace'


def upgrade() -> None:
    with op.batch_alter_table('comment_kinds', recreate='always') as batch:
        batch.drop_constraint('afterend', type_='check')
        batch.drop_column('after_end')
    _rename(OLD, NEW)


def downgrade() -> None:
    with op.batch_alter_table('comment_kinds', recreate='always') as batch:
        batch.add_column(sa.Column('after_end', sa.String(16), server_default='kept', nullable=False))
        batch.create_check_constraint('afterend', "after_end IN ('kept', 'refused')")
    op.execute("UPDATE comment_kinds SET after_end = 'refused' WHERE id = 'finding'")
    _rename(NEW, OLD)


def _rename(old: str, new: str) -> None:
    bind = op.get_bind()
    for campaign_id, config in bind.execute(text('SELECT id, config FROM campaigns')).all():
        stored = json.loads(config)
        plugins = stored.get('plugins')
        roles = plugins.get('role') if isinstance(plugins, dict) else None
        for role in roles.values() if isinstance(roles, dict) else ():
            if isinstance(role, dict) and old in role:
                role[new] = role.pop(old)
        bind.execute(
            text('UPDATE campaigns SET config = :c WHERE id = :id'), {'c': json.dumps(stored), 'id': campaign_id}
        )
