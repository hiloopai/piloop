"""The bridges of the harness capability: its keys of an older release, rewritten (``piloop.deprecated``).

This module imports no provider: the bridges run before any provider loads.
"""

from pathlib import Path

from piloop import deprecated
from piloop.version import Version

OLD_HARNESSES = ('pi', 'claude', 'codex')
"""The built-in harnesses of 0.3, whose tables named their own renderer and tally."""


@deprecated.bridge(
    '0.5.0',
    'mcp and usage of a [harness.<name>] table are its provider: delete the harness_providers bridge',
    after=('plugin_tables',),
)
def harness_providers(toml: Path, raw: dict[str, object], removed_in: Version) -> None:
    """``mcp`` and ``[plugins.harness.<name>.usage]`` are the harness's provider: it renders the servers, reads usage.

    A table named after a built-in harness drops them: the provider's own come from ``home``. A table with a new name
    gets ``provider``: its usage's ``tally``, else its ``mcp``. A table that sets neither runs its argv and does
    nothing else, as before.
    """
    tables = deprecated.table_at(raw, 'plugins', 'harness')
    for name, table in tables.items() if tables is not None else ():
        if not isinstance(table, dict) or not {'mcp', 'usage'} & table.keys():
            continue
        mcp, usage = table.pop('mcp', None), table.pop('usage', None)
        tally = usage.get('tally') if isinstance(usage, dict) else None
        if name in OLD_HARNESSES:
            message = (
                f'mcp and usage of [plugins.harness.{name}] are deprecated: the harness renders its servers and reads'
                ' its usage itself, under its home'
            )
        else:
            table['provider'] = named = tally or mcp
            message = f'mcp and usage of [plugins.harness.{name}] are deprecated; write provider = "{named}"'
        deprecated.warn(toml, f'plugins.harness.{name}', message, removed_in=removed_in)
