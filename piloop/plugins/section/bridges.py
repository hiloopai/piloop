"""The bridges of the section capability: its keys of an older release, rewritten (``piloop.deprecated``).

This module imports no provider: the bridges run before any provider loads.
"""

from pathlib import Path

from piloop import deprecated
from piloop.version import Version

LESSONS = 'lessons'
"""The section that ``wake_budget`` sized."""
OLD_BUDGETS = {'watchdog': 0}
"""The roles whose ``wake_budget`` had another default than ``[memory]``'s."""


@deprecated.bridge(
    '0.5.0',
    'wake_budget is the budget of the lessons section: delete wake_budget, LESSONS and OLD_BUDGETS',
    after=('plugin_tables',),
    before=('plugin_lists',),
)
def wake_budget(toml: Path, raw: dict[str, object], removed_in: Version) -> None:
    """``[memory] wake_budget`` and ``[plugins.role.<name>] wake_budget`` are the ``budget`` of the ``lessons`` section.

    A role's own value wins over ``[memory]``'s. A budget that differs from the role's old default sets the role's
    ``sections`` to its defaults with the lessons at that budget, or without them at 0. A role that sets ``sections``
    keeps them. The watchdog's old default was 0: it now reads the lessons too.
    """
    from piloop.plugins.role.contract import ROLES, new_role  # here: the roles import this module
    from piloop.plugins.section.contract import defaults  # here: the sections import this module

    shared = deprecated.popped(raw, 'memory', 'wake_budget')
    if (roles := deprecated.roles(raw)) is None:
        return
    owned = {n: t.pop('wake_budget') for n, t in roles.items() if isinstance(t, dict) and 'wake_budget' in t}
    if shared is not None:
        deprecated.warn(toml, 'memory.wake_budget', _MOVED, removed_in=removed_in)
    for name in owned:
        deprecated.warn(
            toml,
            f'plugins.role.{name}.wake_budget',
            _MOVED,
            removed_in=removed_in,
            nth=deprecated.nth(toml, f'plugins.role.{name}', 'wake_budget'),
        )
    for name in dict.fromkeys([*ROLES.providers(), *roles]):
        table = roles.setdefault(name, {})
        budget = owned.get(name, None if name in OLD_BUDGETS else shared)
        if not isinstance(table, dict) or 'sections' in table or budget in (None, OLD_BUDGETS.get(name, 400)):
            continue
        model = ROLES.model_of(name) if name in ROLES.providers() else new_role(name, table)
        # a role of its own sections, which wake_budget never sized
        if model.model_fields['sections'].default is not None:
            continue
        listed = [{'provider': name} for name in defaults(model.SURFACE)]
        others = [e for e in listed if e['provider'] != LESSONS]
        table['sections'] = [{'provider': LESSONS, 'budget': budget}, *others] if budget else others


_MOVED = "wake_budget is deprecated; set budget in a [plugins.section.<name>] of lessons, in the role's sections"
