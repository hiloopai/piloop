"""Each stored ``piloop.toml`` (``campaigns.config``) loses ``[evaluator] scoring``: piloop scores what is submitted.

0.7 has one way to score a run's versions, the submissions, so the key is gone. 0087 set it to ``submit`` where it was
``run_end``, so no campaign scores otherwise after this revision.
The downgrade puts nothing back: 0.6 reads a missing key as ``submit``.

Revision ID: 0088
Revises: 0087
"""

import json
import logging
from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = '0088'
down_revision: str | None = '0087'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen here, as of this revision.
KEY = 'scoring'


def upgrade() -> None:
    bind = op.get_bind()
    changed = 0
    for campaign_id, config in bind.execute(text('SELECT id, config FROM campaigns')).all():
        stored = json.loads(config)
        evaluator = stored.get('evaluator')
        if not isinstance(evaluator, dict) or KEY not in evaluator:
            continue
        del evaluator[KEY]
        bind.execute(
            text('UPDATE campaigns SET config = :c WHERE id = :id'), {'c': json.dumps(stored), 'id': campaign_id}
        )
        changed += 1
    logging.getLogger(__name__).info('migration 0088 took scoring out of %d stored configs', changed)


def downgrade() -> None:
    pass
