"""A cancel is a row of ``cancels``, per candidate and evaluator, and no longer makes a run's candidate ``skipped``.

0.6's cancel made a run's candidate without a measurement under the current evaluator ``skipped``, which took it out
of every pool. The upgrade gives each ``skipped`` candidate that an ``evaluation.cancelled`` event names its origin
``run`` back, with a cancel under its campaign's current evaluator, signed as the event was, and logs how many. A
cancel of a waiting evaluation left no event, so its candidate stays ``skipped``.

The downgrade makes each run's candidate with a cancel and no measurement under that evaluator ``skipped`` again, as
0.6 would have, and drops the table.

Revision ID: 0011
Revises: 0010
"""

import logging
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import text

revision: str = '0011'
down_revision: str | None = '0010'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen here, as of this revision.
CANCELLED = (
    "SELECT c.id, p.evaluator_id, json_extract(e.payload, '$.by') FROM candidates c"
    ' JOIN campaigns p ON p.id = c.campaign_id'
    " JOIN events e ON e.target_kind = 'candidate' AND e.target_id = c.id AND e.kind = 'evaluation.cancelled'"
    " WHERE c.origin = 'skipped' AND p.evaluator_id IS NOT NULL"
    " AND e.id = (SELECT max(id) FROM events WHERE target_kind = 'candidate' AND target_id = c.id"
    " AND kind = 'evaluation.cancelled')"
)
"""Each candidate that 0.6 skipped and an ``evaluation.cancelled`` event names, with its campaign's evaluator and
who cancelled it last."""
SKIP_AGAIN = (
    "UPDATE candidates SET origin = 'skipped' WHERE origin = 'run' AND id IN (SELECT k.candidate_id FROM cancels k"
    ' WHERE NOT EXISTS (SELECT 1 FROM measurements m WHERE m.candidate_id = k.candidate_id'
    ' AND m.evaluator_id = k.evaluator_id))'
)


def upgrade() -> None:
    op.create_table(
        'cancels',
        sa.Column('candidate_id', sa.Integer(), nullable=False),
        sa.Column('evaluator_id', sa.Integer(), nullable=False),
        sa.Column('cancelled_by', sa.String(), nullable=False),
        sa.Column('cancelled_at', sa.DateTime(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.CheckConstraint("typeof(cancelled_by) IN ('text')", name='cancels.cancelled_by must be a text value'),
        sa.ForeignKeyConstraint(['candidate_id'], ['candidates.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['evaluator_id'], ['evaluators.id']),
        sa.PrimaryKeyConstraint('candidate_id', 'evaluator_id'),
    )
    bind = op.get_bind()
    found = bind.execute(text(CANCELLED)).all()
    for candidate, evaluator, by in found:
        bind.execute(
            text('INSERT INTO cancels (candidate_id, evaluator_id, cancelled_by) VALUES (:c, :e, :by)'),
            {'c': candidate, 'e': evaluator, 'by': by},
        )
        bind.execute(text("UPDATE candidates SET origin = 'run' WHERE id = :c"), {'c': candidate})
    if found:
        logging.getLogger(__name__).warning(
            'optimization migration 0011 gave %d cancelled candidates their origin run back', len(found)
        )


def downgrade() -> None:
    op.get_bind().execute(text(SKIP_AGAIN))
    op.drop_table('cancels')
