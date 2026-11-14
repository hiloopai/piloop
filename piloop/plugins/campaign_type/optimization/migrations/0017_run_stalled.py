"""A run may end ``stalled``: the campaign's liveness policy ended its agent (``[plugins.liveness]``).

The CHECK of ``runs.end`` takes the new value. The downgrade makes each ``stalled`` run, and the ``run.finished`` event
that closed it, ``failed``: its error still says why the policy ended it.

Revision ID: 0017
Revises: 0016
"""

from collections.abc import Sequence

from alembic import op

from piloop.db.ids import rewrite

revision: str = '0017'
down_revision: str | None = '0016'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen here, as of this revision.
BEFORE = "'role_removed', 'not_started', 'worker_lost')"
AFTER = "'role_removed', 'not_started', 'worker_lost', 'stalled')"


def _check(old: str, new: str) -> None:
    def edit(sql: str) -> str:
        assert sql.count(old) == 1, f'runs: no CHECK {old}'
        return sql.replace(old, new)

    rewrite(op, 'runs', edit)


def upgrade() -> None:
    _check(BEFORE, AFTER)


def downgrade() -> None:
    op.execute('UPDATE runs SET "end" = \'failed\' WHERE "end" = \'stalled\'')
    op.execute(
        "UPDATE events SET payload = json_set(payload, '$.end', 'failed')"
        " WHERE kind = 'run.finished' AND json_extract(payload, '$.end') = 'stalled'"
    )
    _check(AFTER, BEFORE)
