"""Each stored ``piloop.toml`` (``campaigns.config``) that scores at run end scores what its implementers submit.

0078 pinned ``[evaluator] scoring = "run_end"`` on each campaign that 0.5 stored, and 0.7 removes ``run_end``. Such a
campaign gets ``scoring = "submit"``: from the upgrade on, piloop scores only the versions that an implementer submits
(``piloop submit``), and keeps each other version of a run as ``skipped``. Its other pins (``noise_runs``) stay. The
downgrade puts nothing back: 0.6 scores a campaign of ``submit`` as 0.7 does.

Revision ID: 0087
Revises: 0086
"""

import json
import logging
from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = '0087'
down_revision: str | None = '0086'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen here, as of this revision.
OLD, NEW = 'run_end', 'submit'


def upgrade() -> None:
    bind = op.get_bind()
    changed = 0
    for campaign_id, config in bind.execute(text('SELECT id, config FROM campaigns')).all():
        stored = json.loads(config)
        evaluator = stored.get('evaluator')
        if not isinstance(evaluator, dict) or evaluator.get('scoring') != OLD:
            continue
        evaluator['scoring'] = NEW
        bind.execute(
            text('UPDATE campaigns SET config = :c WHERE id = :id'), {'c': json.dumps(stored), 'id': campaign_id}
        )
        changed += 1
    logging.getLogger(__name__).info('migration 0087 set scoring = "submit" on %d stored configs', changed)


def downgrade() -> None:
    pass
