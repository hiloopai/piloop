"""The bridges of the section capability: its keys of an older release, rewritten (``piloop.deprecated``).

This module imports no provider: the bridges run before any provider loads.
"""

from pathlib import Path

from piloop import deprecated
from piloop.version import Version


@deprecated.bridge('0.7.0', 'the pack lists no seeds: delete pack_seeds')
def pack_seeds(toml: Path, raw: dict[str, object], removed_in: Version) -> None:
    """``[plugins.section.pack] seeds`` is removed: there are no seeds, so the research pack lists none."""
    table = deprecated.table_at(raw, 'plugins', 'section', 'pack')
    if table is None or 'seeds' not in table:
        return
    del table['seeds']
    deprecated.warn(
        toml, 'plugins.section.pack.seeds', 'the research pack lists no seeds now; delete it', removed_in=removed_in
    )
