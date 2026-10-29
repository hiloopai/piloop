"""The bridges of the executor capability: its keys of an older release, rewritten (``piloop.deprecated``).

This module imports no provider: the bridges run before any provider loads.
"""

from pathlib import Path

from piloop import deprecated
from piloop.deprecated import Move
from piloop.plugins.host import Scope
from piloop.version import Version

POOL_SLOTS = (Move('plugins.executor.pools.*.evaluations', 'plugins.executor.pools.*.slots.evaluation'),)
"""A pool's slots of evaluations, as the slots of the ``evaluation`` job kind."""


@deprecated.bridge('0.5.0', "a pool's evaluations is slots.evaluation: delete pool_slots and POOL_SLOTS", Scope.HOME)
def pool_slots(toml: Path, raw: dict[str, object], removed_in: Version) -> None:
    """In ``config.toml``, ``evaluations`` of ``[plugins.executor.pools.<name>]`` is ``slots = { evaluation = n }``.

    ``PILOOP_PLUGINS__EXECUTOR__POOLS__<NAME>__EVALUATIONS`` is ``…__SLOTS__EVALUATION``.
    """
    deprecated.moved(toml, raw, POOL_SLOTS, 'PILOOP_', removed_in)
