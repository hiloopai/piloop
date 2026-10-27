"""The lists of triggers, sections and fact sources name instances, and the MCP servers are ``plugins.mcp_server``.

The stored copy of each ``piloop.toml`` (``campaigns.config``) rewrites its lists as ``piloop.deprecated`` rewrites the
file's: each role's ``triggers`` and ``sections``, ``status.sections`` (to ``plugins.section.use``) and ``facts`` (to
``plugins.fact_source.use``) become names, and each entry with parameters a table of its own. ``mcp`` moves to
``plugins.mcp_server``, and each server gets the provider that its keys say, ``http`` or ``stdio``. The downgrade turns
the names back into entries, drops the tables, and moves the servers back without their provider.

Revision ID: 0066
Revises: 0065
"""

import json
from collections.abc import Callable, Sequence

from alembic import op
from sqlalchemy import text

from piloop.deprecated import Entries, Listed, Move, Prune, Tag

revision: str = '0066'
down_revision: str | None = '0065'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen here, as of this revision.
LISTS = (
    Entries('plugins.role.*.triggers', 'plugins.role.*.triggers', 'plugins.trigger', '*'),
    Entries('plugins.role.*.sections', 'plugins.role.*.sections', 'plugins.section', '*'),
    Entries('status.sections', 'plugins.section.use', 'plugins.section', 'status'),
    Entries('facts', 'plugins.fact_source.use', 'plugins.fact_source', None),
)
MCP = (
    Move('mcp.*', 'plugins.mcp_server.*'),
    Prune('plugins.mcp_server.*'),
    Tag('plugins.mcp_server.*', 'provider', (('url', 'http'), ('command', 'stdio'))),
)
TABLES = ('trigger', 'section', 'fact_source')
"""The capabilities whose tables the downgrade drops: none of them had one before."""


def upgrade() -> None:
    _rewrite(upgraded)


def downgrade() -> None:
    _rewrite(downgraded)


def _rewrite(change: Callable[[dict[str, object]], dict[str, object]]) -> None:
    bind = op.get_bind()
    for campaign_id, config in bind.execute(text('SELECT id, config FROM campaigns')).all():
        bind.execute(
            text('UPDATE campaigns SET config = :c WHERE id = :id'),
            {'c': json.dumps(change(json.loads(config))), 'id': campaign_id},
        )


def upgraded(config: dict[str, object]) -> dict[str, object]:
    """Return a stored config in the shape of this revision."""
    for listed in LISTS:
        listed.apply(config)
    for step in MCP:
        step.apply(config)
    return config


def downgraded(config: dict[str, object]) -> dict[str, object]:
    """Return a stored config in the shape of the revision before."""
    for back in [step.inverse() for step in reversed(MCP)]:
        if back is not None:  # a pruned key held nothing: nothing to put back
            back.apply(config)
    for named in [Listed(listed) for listed in reversed(LISTS)]:
        named.apply(config)
    plugins = config.get('plugins')
    if isinstance(plugins, dict):
        for capability in TABLES:
            plugins.pop(capability, None)
    return config
