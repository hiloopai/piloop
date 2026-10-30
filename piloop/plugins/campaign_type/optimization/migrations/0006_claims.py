"""A run is one fresh worker session: it has a campaign, and an idea only once its worker claims one.

``runs.campaign_id`` is new, from each run's idea. ``runs.idea_id`` and ``runs.attempt`` may be null until the claim.
The scheduler assigns no idea now, so the idea keys that steered it go: ``ideas.harness``, ``ideas.base_candidate_id``
and ``ideas.max_runs``. The ``run.queued`` events lose the keys of the idea that the scheduler picked, which the run
row keeps. The downgrade deletes the runs that claimed no idea, gives back each idea's keys as their defaults, and
each ``run.queued`` event its idea, attempt and track. It refuses a database where a run that claimed no idea made a
candidate: deleting that run would leave the candidate, maybe the head, without its run.

Revision ID: 0006
Revises: 0005
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from piloop.db.triggers import recreate

revision: str = '0006'
down_revision: str | None = '0005'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen here, as of this revision.
QUEUED_KEYS = ('idea', 'attempt', 'track', 'priority_pick', 'pinned_base', 'computed_base')
IDEA_CHECKS = {'harness': 'ideas.harness must be a text value'}


def upgrade() -> None:
    with op.batch_alter_table('runs', recreate='always') as batch:
        batch.add_column(sa.Column('campaign_id', sa.Integer(), nullable=True))
    op.execute('UPDATE runs SET campaign_id = (SELECT campaign_id FROM ideas WHERE ideas.id = runs.idea_id)')
    with op.batch_alter_table('runs', recreate='always') as batch:
        batch.alter_column('campaign_id', existing_type=sa.Integer(), nullable=False)
        batch.create_foreign_key(
            'fk_runs_campaign_id_campaigns', 'campaigns', ['campaign_id'], ['id'], ondelete='CASCADE'
        )
        batch.alter_column('idea_id', existing_type=sa.Integer(), nullable=True)
        batch.alter_column('attempt', existing_type=sa.Integer(), nullable=True)
        batch.create_index('ix_runs_campaign_id', ['campaign_id'])
    op.execute('DROP TRIGGER idea_links_campaign')  # it reads ideas, which the rebuild renames; recreated below
    with op.batch_alter_table('ideas', recreate='always') as batch:
        batch.drop_constraint(IDEA_CHECKS['harness'], type_='check')
        batch.drop_column('harness')
        batch.drop_column('base_candidate_id')
        batch.drop_column('max_runs')
    removed = ', '.join(f"'$.{k}'" for k in QUEUED_KEYS)
    op.execute(f"UPDATE events SET payload = json_remove(payload, {removed}) WHERE kind = 'run.queued'")
    for table in ('runs', 'ideas', 'idea_links'):
        recreate(op, table)


def downgrade() -> None:
    made = op.get_bind().execute(
        sa.text('SELECT DISTINCT r.id FROM runs r JOIN candidates c ON c.run_id = r.id WHERE r.idea_id IS NULL')
    )
    if found := [row[0] for row in made]:
        runs = ', '.join(f'run#{n}' for n in found)
        raise RuntimeError(f'cannot downgrade: {runs} claimed no idea and made candidates, which would lose their run')
    op.execute('DELETE FROM runs WHERE idea_id IS NULL')
    op.execute(
        "UPDATE events SET payload = json_set(payload, '$.idea', r.idea_id, '$.attempt', r.attempt, '$.track', i.track)"
        ' FROM runs r JOIN ideas i ON i.id = r.idea_id'
        " WHERE events.kind = 'run.queued' AND events.target_kind = 'run' AND events.target_id = r.id"
    )
    op.execute('DROP TRIGGER idea_links_campaign')
    with op.batch_alter_table('ideas', recreate='always') as batch:
        batch.add_column(sa.Column('harness', sa.String(), nullable=True))
        batch.add_column(sa.Column('base_candidate_id', sa.Integer(), nullable=True))
        batch.add_column(sa.Column('max_runs', sa.Integer(), server_default=sa.text('2'), nullable=False))
        batch.create_check_constraint(IDEA_CHECKS['harness'], "typeof(harness) IN ('text', 'null')")
    with op.batch_alter_table('runs', recreate='always') as batch:
        batch.drop_index('ix_runs_campaign_id')
        batch.drop_constraint('fk_runs_campaign_id_campaigns', type_='foreignkey')
        batch.drop_column('campaign_id')
        batch.alter_column('idea_id', existing_type=sa.Integer(), nullable=False)
        batch.alter_column('attempt', existing_type=sa.Integer(), nullable=False)
    for table in ('runs', 'ideas', 'idea_links'):
        recreate(op, table)
