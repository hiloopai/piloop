"""The bridges of the memory capability: its keys of an older release, rewritten (``piloop.deprecated``).

This module imports no provider: the bridges run before any provider loads.
"""

from pathlib import Path

from piloop import deprecated
from piloop.version import Version


@deprecated.bridge('0.5.0', '[memory] documents is [plugins.memory] documents: delete the memory_documents bridge')
def memory_documents(toml: Path, raw: dict[str, object], removed_in: Version) -> None:
    """``[memory] documents`` is ``documents`` in ``[plugins.memory]``, of the ``local`` provider.

    A value that ``[plugins.memory]`` sets too wins.
    """
    memory = deprecated.table_at(raw, 'memory')
    if memory is None or 'documents' not in memory:
        return
    deprecated.into(raw, 'memory', {'documents': deprecated.popped(raw, 'memory', 'documents')})
    deprecated.warn(
        toml,
        'memory.documents',
        '[memory] documents is deprecated; write documents in [plugins.memory]',
        removed_in=removed_in,
        nth=deprecated.nth(toml, 'memory', 'documents'),
    )
