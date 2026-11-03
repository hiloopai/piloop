"""A candidate has no verdict: ``candidates.verdict``, ``cause``, ``verdict_by``, ``verdict_reason`` and ``track`` go,
``excluded_reason`` and ``excluded_by`` come, and ``measurements.pair_id`` goes.

0.6 scores every candidate into a pool, and the best is a query. Each verdict that 0.5 gave stays in the events: the
upgrade first writes a ``candidate.verdict`` event for each candidate whose verdict no event has, as the change log's
trigger would have, and checks that every verdict then has one. A measurement runs next to no bar, so no pair is
recorded. The downgrade gives the columns back, each verdict from the latest ``candidate.verdict`` event of its
candidate, and drops the exclusions.

Revision ID: 0009
Revises: 0008
"""

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import text

from piloop.db.triggers import recreate

revision: str = '0009'
down_revision: str | None = '0008'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen here, as of this revision.
KIND = 'candidate.verdict'
DAEMON = 'daemon'
LATEST = (
    "SELECT e.target_id, json_extract(e.payload, '$.new') FROM events e WHERE e.kind = :kind"
    " AND e.target_kind = 'candidate' AND e.id = (SELECT max(id) FROM events WHERE kind = :kind"
    " AND target_kind = 'candidate' AND target_id = e.target_id)"
)
"""Each candidate's latest ``candidate.verdict`` event: its id, and the verdict that it set."""


class VerdictsLost(RuntimeError):
    """A candidate's verdict has no event after the backfill: dropping the columns would lose it."""


def upgrade() -> None:
    _backfill()
    with op.batch_alter_table('candidates', recreate='always') as batch:
        batch.drop_index('ix_candidates_campaign_verdict')
        for name in (
            'candidates.cause needs interesting',
            'candidates.verdict_by must be a text value',
            'candidates.verdict_reason must be a text value',
            'outcome',
            'candidates.verdict needs a judged origin',
            'cause',
            'candidates.track must be a text value',
        ):
            batch.drop_constraint(name, type_='check')
        for column in ('verdict', 'cause', 'verdict_by', 'verdict_reason', 'track'):
            batch.drop_column(column)
        batch.add_column(sa.Column('excluded_reason', sa.String(), nullable=True))
        batch.add_column(sa.Column('excluded_by', sa.String(), nullable=True))
        batch.create_check_constraint(
            'candidates.excluded_reason must be a text value', "typeof(excluded_reason) IN ('text', 'null')"
        )
        batch.create_check_constraint(
            'candidates.excluded_by must be a text value', "typeof(excluded_by) IN ('text', 'null')"
        )
        batch.create_check_constraint(
            'candidates.an exclusion has its author', '(excluded_reason IS NULL) = (excluded_by IS NULL)'
        )
    recreate(op, 'candidates')
    with op.batch_alter_table('measurements', recreate='always') as batch:
        batch.drop_constraint('measurements_pair', type_='foreignkey')
        batch.drop_column('pair_id')
    recreate(op, 'measurements')


def downgrade() -> None:
    with op.batch_alter_table('measurements', recreate='always') as batch:
        batch.add_column(sa.Column('pair_id', sa.Integer(), nullable=True))
        batch.create_foreign_key('measurements_pair', 'measurements', ['pair_id'], ['id'], ondelete='SET NULL')
    recreate(op, 'measurements')
    with op.batch_alter_table('candidates', recreate='always') as batch:
        batch.drop_constraint('candidates.an exclusion has its author', type_='check')
        batch.drop_constraint('candidates.excluded_by must be a text value', type_='check')
        batch.drop_constraint('candidates.excluded_reason must be a text value', type_='check')
        batch.drop_column('excluded_by')
        batch.drop_column('excluded_reason')
        batch.add_column(sa.Column('verdict', sa.String(16), nullable=True))
        batch.add_column(sa.Column('verdict_by', sa.String(), nullable=True))
        batch.add_column(sa.Column('verdict_reason', sa.String(), nullable=True))
        batch.add_column(sa.Column('cause', sa.String(16), nullable=True))
        batch.add_column(sa.Column('track', sa.String(), nullable=True))
        batch.create_check_constraint('cause', "cause IN ('note', 'moved')")
        batch.create_check_constraint('outcome', "verdict IN ('keep', 'interesting', 'discard', 'crash')")
        batch.create_check_constraint(
            'candidates.verdict needs a judged origin', "verdict IS NULL OR origin IN ('baseline', 'reference', 'run')"
        )
        batch.create_check_constraint('candidates.cause needs interesting', "cause IS NULL OR verdict = 'interesting'")
        for column in ('verdict_by', 'verdict_reason', 'track'):
            batch.create_check_constraint(
                f'candidates.{column} must be a text value', f"typeof({column}) IN ('text', 'null')"
            )
        batch.create_index('ix_candidates_campaign_verdict', ['campaign_id', 'verdict'])
    recreate(op, 'candidates')
    bind = op.get_bind()
    for candidate, verdict, by, reason in bind.execute(
        text(
            "SELECT e.target_id, json_extract(e.payload, '$.new'), json_extract(e.payload, '$.verdict_by'),"
            " json_extract(e.payload, '$.verdict_reason') FROM events e WHERE e.kind = :kind"
            " AND e.target_kind = 'candidate' AND e.id = (SELECT max(id) FROM events WHERE kind = :kind"
            " AND target_kind = 'candidate' AND target_id = e.target_id)"
        ),
        {'kind': KIND},
    ).all():
        bind.execute(
            text(
                'UPDATE candidates SET verdict = :verdict, verdict_by = :by, verdict_reason = :reason'
                " WHERE id = :id AND origin IN ('baseline', 'reference', 'run')"
            ),
            {'verdict': verdict, 'by': by, 'reason': reason, 'id': candidate},
        )


def _backfill() -> None:
    """Write a ``candidate.verdict`` event for each verdict that its candidate's latest such event does not set.

    Raises:
        VerdictsLost: a verdict still has no event, so dropping the column would lose it.
    """
    bind = op.get_bind()
    latest = dict(bind.execute(text(LATEST), {'kind': KIND}).all())
    rows = bind.execute(
        text('SELECT id, verdict, verdict_by, verdict_reason, cause, track FROM candidates WHERE verdict IS NOT NULL')
    ).all()
    for candidate, verdict, by, reason, cause, track in rows:
        if latest.get(candidate) == verdict:
            continue
        payload = {
            'kind': KIND,
            'old': latest.get(candidate),
            'new': verdict,
            'verdict_by': by,
            'verdict_reason': reason,
            'cause': cause,
            'track': track,
        }
        bind.execute(
            text(
                'INSERT INTO events (actor, target_kind, target_id, payload)'
                " VALUES (:actor, 'candidate', :id, :payload)"
            ),
            {'actor': by or DAEMON, 'id': candidate, 'payload': json.dumps(payload)},
        )
    found = dict(bind.execute(text(LATEST), {'kind': KIND}).all())
    lost = [candidate for candidate, verdict, *_ in rows if found.get(candidate) != verdict]
    if lost:
        raise VerdictsLost(f'the verdicts of candidates {lost} have no {KIND} event: the columns stay')
