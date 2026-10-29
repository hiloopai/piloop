"""An archiving's ``subject_kind`` is a row of core's ``target_kinds``, as the kinds of events and comments are.

Its enum CHECK becomes a foreign key: a kind that a plugin declares can be a subject.

Revision ID: 0003
Revises: 0002
"""

from collections.abc import Sequence

from alembic import op

revision: str = '0003'
down_revision: str | None = '0002'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen here, as of this revision.
OLD = ('campaign', 'idea', 'run', 'candidate', 'measurement', 'agent_session', 'comment', 'instance', 'home')


def upgrade() -> None:
    with op.batch_alter_table('archives', recreate='always') as batch:
        batch.drop_constraint('targetkind', type_='check')
        batch.create_foreign_key('targetkind', 'target_kinds', ['subject_kind'], ['id'])


def downgrade() -> None:
    known = ', '.join(repr(k) for k in OLD)
    op.execute(f'DELETE FROM archives WHERE subject_kind NOT IN ({known})')
    with op.batch_alter_table('archives', recreate='always') as batch:
        batch.drop_constraint('targetkind', type_='foreignkey')
        batch.create_check_constraint('targetkind', f'subject_kind IN ({known})')
