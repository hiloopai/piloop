"""The bridges of the role capability: its keys of an older release, rewritten (``piloop.deprecated``).

This module imports no provider: the bridges run before any provider loads.
"""

from pathlib import Path

from piloop import deprecated
from piloop.version import Version


@deprecated.bridge('0.7.0', 'the scout role is removed: delete scout_role')
def scout_role(toml: Path, raw: dict[str, object], removed_in: Version) -> None:
    """``[plugins.role.scout]`` is removed with the scout role: piloop drops the table, and no scout starts."""
    roles = deprecated.table_at(raw, 'plugins', 'role')
    if roles is None or 'scout' not in roles:
        return
    del roles['scout']
    deprecated.warn(
        toml, 'plugins.role.scout', 'the scout role is removed; delete [plugins.role.scout]', removed_in=removed_in
    )


RUN_ROLE_KEYS = ('files', 'plan_words')
"""The keys of a role that does runs that set how its agents filed ideas."""


@deprecated.bridge('0.7.0', 'a role that does runs files no ideas: delete run_role_ideas and RUN_ROLE_KEYS')
def run_role_ideas(toml: Path, raw: dict[str, object], removed_in: Version) -> None:
    """``files`` and ``plan_words`` of a role that does runs are removed: its agents file no ideas and no seeds.

    piloop drops them. An implementer's hunch goes in its finding, which research reads.
    """
    from piloop.plugins.role.contract import ROLES, ImplementerRole  # here: a bridges module imports no provider

    providers = ROLES.providers()
    roles = deprecated.table_at(raw, 'plugins', 'role') or {}
    for name, table in [(n, t) for n, t in roles.items() if isinstance(t, dict)]:  # the validation refuses the rest
        runs = issubclass(providers[name].model, ImplementerRole) if name in providers else table.get('runs') is True
        for key in RUN_ROLE_KEYS:
            if not runs or key not in table:
                continue
            del table[key]
            deprecated.warn(
                toml,
                f'plugins.role.{name}.{key}',
                f'{key} of a role that does runs is removed, as its agents file no ideas; delete it',
                removed_in=removed_in,
                nth=deprecated.nth(toml, f'plugins.role.{name}', key),
            )


@deprecated.bridge('0.7.0', 'an idea waits for no review: delete idle_alert')
def idle_alert(toml: Path, raw: dict[str, object], removed_in: Version) -> None:
    """``idle_alert`` of a role that does runs is removed: an idea is never ``proposed``, so no slot waits for a review.

    piloop drops it.
    """
    roles = deprecated.table_at(raw, 'plugins', 'role') or {}
    for name, table in [(n, t) for n, t in roles.items() if isinstance(t, dict) and 'idle_alert' in t]:
        del table['idle_alert']
        deprecated.warn(
            toml,
            f'plugins.role.{name}.idle_alert',
            'an idea waits for no review now, so idle_alert is removed; delete it',
            removed_in=removed_in,
            nth=deprecated.nth(toml, f'plugins.role.{name}', 'idle_alert'),
        )


@deprecated.bridge('0.7.0', 'files = "seeds" is removed: delete seed_files')
def seed_files(toml: Path, raw: dict[str, object], removed_in: Version) -> None:
    """``files = "seeds"`` is ``files = "nothing"``: there are no seeds. An idea is open on the board from the start."""
    roles = deprecated.table_at(raw, 'plugins', 'role') or {}
    for name, table in [(n, t) for n, t in roles.items() if isinstance(t, dict) and t.get('files') == 'seeds']:
        table['files'] = 'nothing'
        deprecated.warn(
            toml,
            f'plugins.role.{name}.files',
            'a seed is an open idea now, so files = "seeds" files nothing; write "nothing" or "ideas"',
            removed_in=removed_in,
            nth=deprecated.nth(toml, f'plugins.role.{name}', 'files'),
        )
