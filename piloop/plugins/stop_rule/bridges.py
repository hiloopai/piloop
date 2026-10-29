"""The bridges of the stop_rule capability: its keys of an older release, rewritten (``piloop.deprecated``).

This module imports no provider: the bridges run before any provider loads.
"""

from pathlib import Path

from piloop import deprecated
from piloop.version import Version


def _held(raw: dict[str, object], limit: object) -> None:
    """Hold the campaign at ``limit`` of the fact ``spend``: ``[plugins.stop_rule] hold_at``, under what it sets."""
    found = deprecated.plugin_table(raw, 'stop_rule')
    rule = found if found is not None else {}  # None: a value on the way is not a table, which the validation refuses
    held = rule.get('hold_at', {})
    rule['hold_at'] = {'spend': limit, **held} if isinstance(held, dict) else held


@deprecated.bridge('0.5.0', 'max_spend is hold_at of the spend: delete the max_spend bridge')
def max_spend(toml: Path, raw: dict[str, object], removed_in: Version) -> None:
    """``[plugins.stop_rule] max_spend`` is ``hold_at = { spend = <value> }``. A value that ``hold_at`` sets too wins."""
    plugins = raw.get('plugins')
    rule = plugins.get('stop_rule') if isinstance(plugins, dict) else None
    if not isinstance(rule, dict) or 'max_spend' not in rule:
        return
    _held(raw, rule.pop('max_spend'))
    deprecated.warn(
        toml,
        'plugins.stop_rule.max_spend',
        'max_spend is deprecated; write hold_at = { spend = ... }',
        removed_in=removed_in,
    )
