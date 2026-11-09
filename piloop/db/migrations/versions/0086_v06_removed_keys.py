"""The stored copy of each ``piloop.toml`` (``campaigns.config``) loses each key of a feature that is gone since 0.6.

0.6's validators of ``piloop.config.CampaignConfig`` and ``Structured`` drop these keys from a file and from a stored
copy, with a warning; 0.7 deletes them, and a key that is left fails validation. This revision drops from each stored
copy what they drop:

- the roles ``orchestrator``, ``research`` and ``watchdog``, and each role of the file's own that does no runs;
- ``interval``, ``incident_prompt``, ``routine_prompt`` and ``follow_head`` of each role that is left;
- the removed sections (``REMOVED``) from ``[plugins.section]``, its ``use`` and each role's ``sections``, the
  ``judging`` table, and ``judging`` as ``scoring`` in each role's ``sections``;
- ``[plugins.trigger]``, ``[plugins.verdict_rule]``, ``while_held`` of ``[plugins.stop_rule]``, ``grace`` of a
  ``structured`` ``[plugins.escalation]`` and ``repeats`` of each metric.

A role or a section that an installed package provides keeps its table and its keys, as 0.6's validators keep them;
a package's role loses the removed sections from its ``sections`` too. Research memory's keys (``[plugins.memory]``,
the ``lessons`` section) went in 0085, and ``[agents] wake_budget`` is a key of ``config.toml``, never stored here.
The downgrade puts nothing back: 0.6 reads each dropped key as its default.

Revision ID: 0086
Revises: 0085
"""

import json
import logging
from collections.abc import Sequence
from importlib.metadata import entry_points

from alembic import op
from sqlalchemy import text

revision: str = '0086'
down_revision: str | None = '0085'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen here, as of this revision.
ROLES = ('orchestrator', 'research', 'watchdog')
ROLE_KEYS = ('interval', 'incident_prompt', 'routine_prompt', 'follow_head')
REMOVED = frozenset({'idle_queue', 'yields', 'incidents', 'reported', 'research_commands'})
PLUGINS = ('trigger', 'verdict_rule')


def upgrade() -> None:
    bind = op.get_bind()
    changed = 0
    for campaign_id, config in bind.execute(text('SELECT id, config FROM campaigns')).all():
        stored = json.loads(config)
        if (kept := dropped(json.loads(config))) != stored:
            bind.execute(
                text('UPDATE campaigns SET config = :c WHERE id = :id'), {'c': json.dumps(kept), 'id': campaign_id}
            )
            changed += 1
    logging.getLogger(__name__).info(
        'migration 0086 took the keys of the features that are gone out of %d stored configs', changed
    )


def downgrade() -> None:
    pass


def dropped(config: dict[str, object]) -> dict[str, object]:
    """Return a stored config without the keys that 0.6 removed."""
    metrics = config.get('metrics')
    for metric in metrics if isinstance(metrics, list) else ():
        if isinstance(metric, dict):
            metric.pop('repeats', None)
    plugins = config.get('plugins')
    if not isinstance(plugins, dict):
        return config
    for name in PLUGINS:
        plugins.pop(name, None)
    if isinstance(rule := plugins.get('stop_rule'), dict):
        rule.pop('while_held', None)
    if (
        isinstance(escalation := plugins.get('escalation'), dict)
        and escalation.get('provider', 'structured') == 'structured'
    ):
        escalation.pop('grace', None)
    gone = REMOVED - _packaged('piloop.sections')
    if isinstance(section := plugins.get('section'), dict):
        for name in gone | {'judging'}:
            section.pop(name, None)
        if isinstance(use := section.get('use'), list):
            section['use'] = [n for n in use if n not in gone]
    if isinstance(roles := plugins.get('role'), dict):
        _drop_roles(roles, gone)
    return config


def _drop_roles(roles: dict[str, object], gone: frozenset[str]) -> None:
    """Drop the removed roles from ``roles``, and the removed keys and sections from the others."""
    packaged = _packaged('piloop.roles')
    provided = {ep.name for ep in entry_points(group='piloop.roles')}
    for name, table in list(roles.items()):
        if not isinstance(table, dict):
            continue
        if name not in packaged and (name in ROLES or (name not in provided and table.get('runs') is not True)):
            del roles[name]
            continue
        for key in ROLE_KEYS if name not in packaged else ():
            table.pop(key, None)
        if isinstance(listed := table.get('sections'), list):
            table['sections'] = ['scoring' if n == 'judging' else n for n in listed if n not in gone]


def _packaged(group: str) -> set[str]:
    """The names of the providers of ``group`` that an installed package, not piloop, adds."""
    return {ep.name for ep in entry_points(group=group) if ep.dist is None or ep.dist.name != 'piloop'}
