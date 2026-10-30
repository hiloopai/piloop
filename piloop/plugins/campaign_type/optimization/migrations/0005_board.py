"""The board: an idea is ``open``, ``claimed``, ``done`` or ``dropped``, and runs only ``after`` other ideas.

``seed``, ``proposed`` and ``ready`` are ``open``, ``scheduled`` is ``claimed``, and ``parked`` and ``rejected`` are
``dropped``. The ``idea.status`` and ``idea.deleted`` events get the same names. The ``supersedes`` links, their
``idea.linked`` and ``idea.unlinked`` events (an event of a link kind that is gone cannot load), and ``ideas.family``
go. The downgrade names each state as the nearest of the old ones, and puts back no link, no event and no family.

Revision ID: 0005
Revises: 0004
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from piloop.db.triggers import recreate

revision: str = '0005'
down_revision: str | None = '0004'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen here, as of this revision.
UP = {
    'seed': 'open',
    'proposed': 'open',
    'ready': 'open',
    'scheduled': 'claimed',
    'done': 'done',
    'parked': 'dropped',
    'rejected': 'dropped',
}
DOWN = {'open': 'ready', 'claimed': 'scheduled', 'done': 'done', 'dropped': 'parked'}


def _case(column: str, names: dict[str, str]) -> str:
    whens = ' '.join(f"WHEN '{old}' THEN '{new}'" for old, new in names.items())
    return f'CASE {column} {whens} ELSE {column} END'


def _rename(names: dict[str, str]) -> None:
    op.execute(f'UPDATE ideas SET status = {_case("status", names)}')
    for key in ('old', 'new'):
        value = f"json_extract(payload, '$.{key}')"
        op.execute(
            f"UPDATE events SET payload = json_set(payload, '$.{key}', {_case(value, names)})"
            f" WHERE kind = 'idea.status' AND {value} IS NOT NULL"
        )
    value = "json_extract(payload, '$.status')"
    op.execute(
        f"UPDATE events SET payload = json_set(payload, '$.status', {_case(value, names)}) WHERE kind = 'idea.deleted'"
    )


def _status(states: Sequence[str], default: str) -> None:
    with op.batch_alter_table('ideas', recreate='always') as batch:
        batch.create_check_constraint('ideastatus', f'status IN ({", ".join(repr(s) for s in states)})')
        batch.alter_column('status', server_default=default)


def upgrade() -> None:
    op.execute("DELETE FROM idea_links WHERE kind = 'supersedes'")
    op.execute(
        "DELETE FROM events WHERE kind IN ('idea.linked', 'idea.unlinked')"
        " AND json_extract(payload, '$.link_kind') = 'supersedes'"
    )
    with op.batch_alter_table('idea_links', recreate='always') as batch:
        batch.drop_constraint('linkkind', type_='check')
        batch.create_check_constraint('linkkind', "kind IN ('after')")
    with op.batch_alter_table('ideas', recreate='always') as batch:
        batch.drop_constraint('ideastatus', type_='check')
        batch.drop_constraint('ideas.family must be a text value', type_='check')
        batch.drop_column('family')
    _rename(UP)
    _status(tuple(DOWN), 'open')
    recreate(op, 'ideas')
    recreate(op, 'idea_links')


def downgrade() -> None:
    with op.batch_alter_table('idea_links', recreate='always') as batch:
        batch.drop_constraint('linkkind', type_='check')
        batch.create_check_constraint('linkkind', "kind IN ('after', 'supersedes')")
    with op.batch_alter_table('ideas', recreate='always') as batch:
        batch.drop_constraint('ideastatus', type_='check')
        batch.add_column(sa.Column('family', sa.String))
        batch.create_check_constraint('ideas.family must be a text value', "typeof(family) IN ('text', 'null')")
    _rename(DOWN)
    _status(tuple(UP), 'ready')
    recreate(op, 'ideas')
    recreate(op, 'idea_links')
