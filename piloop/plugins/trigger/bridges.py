"""The bridges of the trigger capability: its keys of an older release, rewritten (``piloop.deprecated``).

This module imports no provider: the bridges run before any provider loads.
"""

from pathlib import Path

from piloop import deprecated
from piloop.version import Version

LEAKING_FUNNEL = 'leaking_funnel'
"""The removed trigger: ``low_yield`` finds the runs that reach no measurement."""


@deprecated.bridge('0.7.0', 'the leaking_funnel trigger is removed: delete leaking_funnel and LEAKING_FUNNEL')
def leaking_funnel(toml: Path, raw: dict[str, object], removed_in: Version) -> None:
    """The ``leaking_funnel`` trigger is removed: ``low_yield`` does its work.

    piloop drops it from each role's ``triggers``, and drops each ``[plugins.trigger.<name>]`` of it.
    """
    tables = deprecated.table_at(raw, 'plugins', 'trigger') or {}
    names = {name for name, table in tables.items() if _provider(name, table) == LEAKING_FUNNEL}
    if LEAKING_FUNNEL not in tables:
        names.add(LEAKING_FUNNEL)
    for name in names & tables.keys():
        del tables[name]
        deprecated.warn(toml, f'plugins.trigger.{name}', _REMOVED, removed_in=removed_in)
    for role, table in (deprecated.table_at(raw, 'plugins', 'role') or {}).items():
        triggers = table.get('triggers') if isinstance(table, dict) else None
        if not isinstance(table, dict) or not isinstance(triggers, list):
            continue  # not a table or not a list: the validation refuses it
        kept = [t for t in triggers if not (isinstance(t, str) and t in names)]
        if kept == triggers:
            continue
        table['triggers'] = kept
        deprecated.warn(
            toml,
            f'plugins.role.{role}.triggers',
            _REMOVED,
            removed_in=removed_in,
            nth=deprecated.nth(toml, f'plugins.role.{role}', 'triggers'),
        )


def _provider(name: str, table: object) -> object:
    """The provider of ``[plugins.trigger.<name>]``: its ``provider``, else the built-in of its name."""
    return table.get('provider', name) if isinstance(table, dict) else name


_REMOVED = 'the leaking_funnel trigger is removed; low_yield finds runs that reach no measurement: delete it'
