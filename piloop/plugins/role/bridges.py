"""The bridges of the role capability: its keys of an older release, rewritten (``piloop.deprecated``).

This module imports no provider: the bridges run before any provider loads.
"""

from pathlib import Path

from piloop import deprecated
from piloop.plugins.host import Scope
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


@deprecated.bridge('0.7.0', 'a role that does runs files no ideas for others: delete run_role_ideas and RUN_ROLE_KEYS')
def run_role_ideas(toml: Path, raw: dict[str, object], removed_in: Version) -> None:
    """``files`` and ``plan_words`` of a role that does runs are removed: its agents file no seeds, and no ideas for others.

    piloop drops them. An implementer's hunch goes in a note, which research reads.
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
                f'{key} of a role that does runs is removed, as its agents file no ideas for others; delete it',
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


ASSIGNMENT_KEYS = ('takes', 'explore_every', 'recovery_requeues')
"""The keys of a role that does runs that steered which idea the scheduler gave its runs, and from where."""


@deprecated.bridge('0.7.0', 'workers claim their ideas: delete run_role_assignment and ASSIGNMENT_KEYS')
def run_role_assignment(toml: Path, raw: dict[str, object], removed_in: Version) -> None:
    """``takes``, ``explore_every`` and ``recovery_requeues`` are removed: the scheduler assigns no idea.

    Each run starts from the head, and its worker claims an idea. piloop drops the keys.
    """
    roles = deprecated.table_at(raw, 'plugins', 'role') or {}
    for name, table in [(n, t) for n, t in roles.items() if isinstance(t, dict)]:
        for key in [k for k in ASSIGNMENT_KEYS if k in table]:
            del table[key]
            deprecated.warn(
                toml,
                f'plugins.role.{name}.{key}',
                f'workers claim their ideas now, so {key} is removed; delete it',
                removed_in=removed_in,
                nth=deprecated.nth(toml, f'plugins.role.{name}', key),
            )


END_GRACE = deprecated.Move('plugins.role.*.finding_grace', 'plugins.role.*.end_grace')
"""``finding_grace`` of a role that does runs, as its ``end_grace``: a finding ends no run now."""


@deprecated.bridge(
    '0.7.0', 'finding_grace is end_grace: delete end_grace and END_GRACE', after=('campaign_finding_grace',)
)
def end_grace(toml: Path, raw: dict[str, object], removed_in: Version) -> None:
    """A role's ``finding_grace`` is its ``end_grace``: the daemon ends a worker that grace after it ended its run."""
    deprecated.move_keys(toml, raw, END_GRACE, removed_in)


@deprecated.bridge(
    '0.7.0',
    'finding_grace is end_grace in config.toml too: delete home_end_grace',
    Scope.HOME,
    after=('implementer_timings',),
)
def home_end_grace(toml: Path, raw: dict[str, object], removed_in: Version) -> None:
    """In ``config.toml``, a role's ``finding_grace`` is its ``end_grace``, as in ``piloop.toml``."""
    deprecated.move_keys(toml, raw, END_GRACE, removed_in)
