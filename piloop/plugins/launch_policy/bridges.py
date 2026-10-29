"""The bridges of the launch policy capability: its keys of an older release, rewritten (``piloop.deprecated``).

This module imports no provider: the bridges run before any provider loads.
"""

from pathlib import Path

from piloop import deprecated
from piloop.plugins.host import Scope
from piloop.version import Version

STARTUP = (
    deprecated.Move('daemon.startup_breaker', 'plugins.launch_policy.startup_breaker'),
    deprecated.Move('daemon.startup_window', 'plugins.launch_policy.startup_window'),
)
"""The startup breaker of ``config.toml``, as the launch policy's."""


@deprecated.bridge(
    '0.5.0',
    "[daemon] startup_breaker and startup_window are the launch policy's: delete startup_breaker and STARTUP",
    Scope.HOME,
    env=STARTUP,
)
def startup_breaker(toml: Path, raw: dict[str, object], removed_in: Version) -> None:
    """In ``config.toml``, ``[daemon] startup_breaker`` and ``startup_window`` are ``[plugins.launch_policy]`` keys.

    Their variables move the same way.
    """
    for move in STARTUP:
        deprecated.move_keys(toml, raw, move, removed_in)
