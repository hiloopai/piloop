"""The bridges of the trigger capability: its keys of an older release, rewritten (``piloop.deprecated``).

This module imports no provider: the bridges run before any provider loads.
"""

from pathlib import Path

from piloop import deprecated
from piloop.version import Version

INCIDENT_TRIGGERS = ('run_failed', 'dead_at_startup')
"""What the ``anomaly`` trigger found: a failed run, and an agent dead at its start."""


@deprecated.bridge(
    '0.5.0',
    'the anomaly trigger is run_failed and dead_at_startup: delete anomaly_trigger and INCIDENT_TRIGGERS',
    after=('plugin_tables',),
    before=('plugin_lists',),
)
def anomaly_trigger(toml: Path, raw: dict[str, object], removed_in: Version) -> None:
    """The ``anomaly`` trigger is two triggers: ``run_failed`` and ``dead_at_startup``, in its place in the list."""
    for name, role in (deprecated.table_at(raw, 'plugins', 'role') or {}).items():
        triggers = role.get('triggers') if isinstance(role, dict) else None
        if not isinstance(role, dict) or not isinstance(triggers, list):
            continue
        old = [t for t in triggers if isinstance(t, dict) and t.get('provider') == 'anomaly']
        if not old:
            continue
        role['triggers'] = [
            new for t in triggers for new in ([{'provider': n} for n in INCIDENT_TRIGGERS] if t in old else [t])
        ]
        deprecated.warn(
            toml,
            f'plugins.role.{name}.triggers',
            'the anomaly trigger is deprecated; write "run_failed", "dead_at_startup"',
            removed_in=removed_in,
            nth=deprecated.nth(toml, f'plugins.role.{name}', 'triggers'),
        )
