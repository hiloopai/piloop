"""Each stored ``piloop.toml`` (``campaigns.config``) keeps 0.5's scoring: at run end, by ``bootstrap``, on one evaluation
of its baseline.

0.6 scores what an implementer submits, by the noise of its baseline. A campaign that 0.5 stored gets
``[evaluator] scoring = "run_end"`` and ``noise_runs = 1``, and ``[plugins.verdict_rule] provider = "bootstrap"``,
unless it set them, so nothing changes under a running campaign. Its ``piloop.toml`` read again (``piloop init``,
``piloop campaign sync``) gives the 0.6 defaults. The downgrade takes off each pin that still has its pinned value.

Revision ID: 0078
Revises: 0077
"""

import json
from collections.abc import Callable, Sequence

from alembic import op
from sqlalchemy import text

revision: str = '0078'
down_revision: str | None = '0077'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen here, as of this revision.
EVALUATOR = {'scoring': 'run_end', 'noise_runs': 1}
RULE = {'provider': 'bootstrap'}


def upgrade() -> None:
    _each(_pin)


def downgrade() -> None:
    _each(_unpin)


def _pin(stored: dict[str, object]) -> None:
    evaluator = stored.setdefault('evaluator', {})
    if isinstance(evaluator, dict):
        for key, value in EVALUATOR.items():
            evaluator.setdefault(key, value)
    plugins = stored.setdefault('plugins', {})
    if isinstance(plugins, dict):
        rule = plugins.setdefault('verdict_rule', {})
        if isinstance(rule, dict):
            rule.setdefault('provider', RULE['provider'])


def _unpin(stored: dict[str, object]) -> None:
    for table, pins in ((stored, {'evaluator': EVALUATOR}), (stored.get('plugins'), {'verdict_rule': RULE})):
        if not isinstance(table, dict):
            continue
        for name, values in pins.items():
            found = table.get(name)
            if not isinstance(found, dict):
                continue
            for key, value in values.items():
                if found.get(key) == value:
                    del found[key]
            if not found:
                del table[name]


def _each(change: Callable[[dict[str, object]], None]) -> None:
    bind = op.get_bind()
    for campaign_id, config in bind.execute(text('SELECT id, config FROM campaigns')).all():
        stored = json.loads(config)
        change(stored)
        bind.execute(
            text('UPDATE campaigns SET config = :c WHERE id = :id'), {'c': json.dumps(stored), 'id': campaign_id}
        )
