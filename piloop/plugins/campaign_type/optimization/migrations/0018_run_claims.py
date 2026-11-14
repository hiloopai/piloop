"""A run claims any number of ideas: ``run_claims`` replaces ``runs.idea_id`` and ``runs.attempt``.

Each run that claimed an idea gets its claim, with the idea's attempt and the run's start as its time. Each
submission (``run.submitted``) gets its row in ``submissions``, for its run's claim, which settles on it. A candidate
names the idea of the claim that it was made for, ``candidates.idea_id``: a submitted candidate gets the idea of the
run that submitted it last, which may be another than the run that made it, and any other candidate its run's idea.
The downgrade refuses a database where a run has more than one claim, which ``runs.idea_id`` cannot hold. Else it
gives each run its claim back, and drops the tables and the column: the events keep the submissions.

Revision ID: 0018
Revises: 0017
"""

import re
from collections.abc import Callable, Sequence

import sqlalchemy as sa
from alembic import op

from piloop.db.ids import rewrite

revision: str = '0018'
down_revision: str | None = '0017'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen here, as of this revision: the parts of the tables' SQL that change. A table's SQL depends on the revisions
# that made it, so each is found by pattern.
RUN_UNIQUE = r'UNIQUE \(idea_id, attempt\)'
RUN_FK = r'FOREIGN KEY ?\(idea_id\) REFERENCES "?ideas"? \(id\) ON DELETE CASCADE'
RUN_FK_SQL = 'FOREIGN KEY(idea_id) REFERENCES ideas (id) ON DELETE CASCADE'
"""The table-level constraint that the downgrade gives ``runs`` back: ``RUN_FK`` finds it again."""
CANDIDATE_FK = 'FOREIGN KEY(idea_id) REFERENCES ideas (id) ON DELETE SET NULL'
SUBMISSIONS = (
    'INSERT INTO submissions (run_id, candidate_id, idea_id, event_id, submitted_at)'
    ' SELECT e.target_id, c.id, r.idea_id, e.id, e.at FROM events e JOIN runs r ON r.id = e.target_id'
    " JOIN candidates c ON c.id = json_extract(e.payload, '$.candidate')"
    " WHERE e.kind = 'run.submitted' AND e.target_kind = 'run' ORDER BY e.id"
)
"""Each submission, from its event, for the claim of the run that made it: a run of 0.8 had one claim at most."""
LAST_SUBMITTER = (
    'UPDATE candidates SET idea_id = (SELECT s.idea_id FROM submissions s WHERE s.candidate_id = candidates.id'
    ' ORDER BY s.id DESC LIMIT 1) WHERE id IN (SELECT candidate_id FROM submissions)'
)
"""A submitted candidate names the idea of the run that submitted it last: a run may submit another run's
``skipped`` or ``recovered`` candidate. A lookup by ``ix_submissions_candidate``, not a scan of the events for each
candidate, which is quadratic: no index reads ``payload.candidate``."""
MAKER = 'SELECT idea_id FROM runs WHERE runs.id = candidates.run_id'


def _drop(item: str) -> Callable[[str], str]:
    """An edit of a table's SQL that drops the one definition that ``item`` matches, and its comma."""

    def edit(sql: str) -> str:
        found, n = re.subn(rf',\s*{item}(?=\s*[,)])', '', sql)
        assert n == 1, f'no {item} in {sql}'
        return found

    return edit


def _append(item: str) -> Callable[[str], str]:
    """An edit of a table's SQL that adds ``item`` as its last definition."""

    def edit(sql: str) -> str:
        found, n = re.subn(r'\s*\)\s*$', f', \n\t{item}\n)', sql)
        assert n == 1, sql
        return found

    return edit


def upgrade() -> None:
    op.create_table(
        'run_claims',
        sa.Column('run_id', sa.Integer(), nullable=False),
        sa.Column('idea_id', sa.Integer(), nullable=False),
        sa.Column('attempt', sa.Integer(), nullable=False),
        sa.Column('claimed_at', sa.DateTime(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.ForeignKeyConstraint(['idea_id'], ['ideas.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['run_id'], ['runs.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('run_id', 'idea_id'),
        sa.UniqueConstraint('idea_id', 'attempt'),
    )
    op.execute(
        'INSERT INTO run_claims (run_id, idea_id, attempt, claimed_at)'
        ' SELECT id, idea_id, attempt, coalesce(started_at, created_at) FROM runs WHERE idea_id IS NOT NULL'
    )
    op.execute('ALTER TABLE candidates ADD COLUMN idea_id INTEGER')
    rewrite(op, 'candidates', _append(CANDIDATE_FK))
    op.create_table(
        'submissions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('run_id', sa.Integer(), nullable=False),
        sa.Column('candidate_id', sa.Integer(), nullable=False),
        sa.Column('idea_id', sa.Integer(), nullable=True),
        sa.Column('event_id', sa.Integer(), nullable=False),
        sa.Column('submitted_at', sa.DateTime(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.ForeignKeyConstraint(['candidate_id'], ['candidates.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['idea_id'], ['ideas.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['run_id'], ['runs.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sqlite_autoincrement=True,
    )
    op.create_index('ix_submissions_candidate', 'submissions', ['candidate_id'])
    op.create_index('ix_submissions_idea', 'submissions', ['idea_id', 'run_id'])
    op.create_index('ix_submissions_run', 'submissions', ['run_id'])
    op.execute(SUBMISSIONS)
    op.execute(f'UPDATE candidates SET idea_id = ({MAKER})')
    op.execute(LAST_SUBMITTER)
    rewrite(op, 'runs', lambda sql: _drop(RUN_FK)(_drop(RUN_UNIQUE)(sql)))
    op.execute('ALTER TABLE runs DROP COLUMN idea_id')
    op.execute('ALTER TABLE runs DROP COLUMN attempt')


def downgrade() -> None:
    many = op.get_bind().execute(
        sa.text('SELECT run_id FROM run_claims GROUP BY run_id HAVING count(*) > 1 ORDER BY run_id')
    )
    if found := [row[0] for row in many]:
        runs = ', '.join(f'run#{n}' for n in found)
        raise RuntimeError(f'cannot downgrade: {runs} claimed more than one idea, which runs.idea_id cannot hold')
    op.execute('ALTER TABLE runs ADD COLUMN idea_id INTEGER')
    op.execute('ALTER TABLE runs ADD COLUMN attempt INTEGER')
    op.execute('UPDATE runs SET idea_id = c.idea_id, attempt = c.attempt FROM run_claims c WHERE c.run_id = runs.id')
    rewrite(op, 'runs', lambda sql: _append(RUN_FK_SQL)(_append('UNIQUE (idea_id, attempt)')(sql)))
    rewrite(op, 'candidates', _drop(re.escape(CANDIDATE_FK)))
    op.execute('ALTER TABLE candidates DROP COLUMN idea_id')
    op.drop_table('submissions')
    op.drop_table('run_claims')
