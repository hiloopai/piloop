"""``agent_sessions.deadline``: when an agent's timeout ends it, stored at its launch. A closed agent has no token.

The agents that run at the upgrade have no deadline: an agent that 0.6 launched gets no wrap-up prompt before its
timeout, and its run ends as its process says. The tokens of agents whose session is no longer live are deleted:
one that an earlier release left would act again if an UPDATE set its session live. The downgrade drops the column.

Revision ID: 0083
Revises: 0082
"""

import logging
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import text

revision: str = '0083'
down_revision: str | None = '0082'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ENDED_TOKENS = (
    'DELETE FROM tokens WHERE agent_session_id IS NOT NULL AND agent_session_id NOT IN'
    " (SELECT id FROM agent_sessions WHERE state IN ('starting', 'running', 'ending'))"
)


def upgrade() -> None:
    op.add_column('agent_sessions', sa.Column('deadline', sa.DateTime(), nullable=True))
    if deleted := op.get_bind().execute(text(ENDED_TOKENS)).rowcount:
        logging.getLogger(__name__).info('migration 0083 deleted %d tokens of agents that ended', deleted)


def downgrade() -> None:
    op.execute('ALTER TABLE agent_sessions DROP COLUMN deadline')
