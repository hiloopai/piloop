"""A run ends ``gave_up`` when its worker gives up, and never ``awaiting_answer``: an escalation holds no run now.

Each ``awaiting_answer`` run, and the ``run.finished`` event that closed it, becomes ``failed``: its error still names
the escalation. The downgrade turns ``gave_up`` into ``failed`` the same way.

Revision ID: 0007
Revises: 0006
"""

from collections.abc import Sequence

from alembic import op

from piloop.db.triggers import recreate

revision: str = '0007'
down_revision: str | None = '0006'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen here, as of this revision.
KEPT = ('finished', 'failed', 'timed_out', 'cancelled', 'campaign_stopped', 'daemon_stopped', 'stale')
REST = ('role_removed', 'not_started', 'worker_lost')
UP = (*KEPT[:1], 'gave_up', *KEPT[1:], *REST)
DOWN = (*KEPT[:3], 'awaiting_answer', *KEPT[3:], *REST)


def _ends(gone: str, ends: Sequence[str]) -> None:
    op.execute(f'UPDATE runs SET "end" = \'failed\' WHERE "end" = \'{gone}\'')
    op.execute(
        "UPDATE events SET payload = json_set(payload, '$.end', 'failed')"
        f" WHERE kind = 'run.finished' AND json_extract(payload, '$.end') = '{gone}'"
    )
    with op.batch_alter_table('runs', recreate='always') as batch:
        batch.drop_constraint('runend', type_='check')
        batch.create_check_constraint('runend', f'"end" IN ({", ".join(repr(e) for e in ends)})')
    recreate(op, 'runs')


def upgrade() -> None:
    _ends('awaiting_answer', UP)


def downgrade() -> None:
    _ends('gave_up', DOWN)
