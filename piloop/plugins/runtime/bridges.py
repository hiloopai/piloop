"""The bridges of the runtime capability: its keys of an older release, rewritten (``piloop.deprecated``).

This module imports no provider: the bridges run before any provider loads.
"""

from pathlib import Path

from piloop import deprecated
from piloop.deprecated import Move
from piloop.plugins.host import Scope
from piloop.version import Version

RUNTIME_TABLES = (Move('runtimes.*', 'plugins.runtime.*'),)
"""The settings of each runtime, as ``[plugins.runtime.<name>]``."""


@deprecated.bridge(
    '0.5.0', '[runtimes.<name>] is [plugins.runtime.<name>]: delete runtime_tables and RUNTIME_TABLES', Scope.HOME
)
def runtime_tables(toml: Path, raw: dict[str, object], removed_in: Version) -> None:
    """In ``config.toml``, ``[runtimes.<name>]`` is ``[plugins.runtime.<name>]``.

    ``PILOOP_RUNTIMES__<NAME>__<KEY>`` is ``PILOOP_PLUGINS__RUNTIME__<NAME>__<KEY>``.
    """
    deprecated.moved(toml, raw, RUNTIME_TABLES, 'PILOOP_', removed_in)
