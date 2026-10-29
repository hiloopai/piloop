"""The bridges of the network capability: its keys of an older release, rewritten (``piloop.deprecated``).

This module imports no provider: the bridges run before any provider loads.
"""

from pathlib import Path

from piloop import deprecated
from piloop.plugins.host import Scope
from piloop.version import Version


@deprecated.bridge('0.5.0', 'name_claim of the proxy network is name_claims: delete the name_claim bridge', Scope.HOME)
def name_claim(toml: Path, raw: dict[str, object], removed_in: Version) -> None:
    """In ``config.toml``, ``[plugins.network] name_claim`` of the ``proxy`` network is ``name_claims``, a list of one.

    A ``name_claims`` that the table sets too wins.
    """
    network = deprecated.table_at(raw, 'plugins', 'network')
    if network is None or network.get('provider') != 'proxy' or 'name_claim' not in network:
        return
    network.setdefault('name_claims', [network.pop('name_claim')])
    deprecated.warn(
        toml,
        'plugins.network.name_claim',
        'name_claim is deprecated; write name_claims = ["<claim>"]',
        removed_in=removed_in,
    )
