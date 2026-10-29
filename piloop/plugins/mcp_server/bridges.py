"""The bridges of the mcp_server capability: its keys of an older release, rewritten (``piloop.deprecated``).

This module imports no provider: the bridges run before any provider loads.
"""

from pathlib import Path

from piloop import deprecated
from piloop.deprecated import Move, Prune, Step, Tag
from piloop.version import Version

MCP_PROVIDERS: tuple[Step, ...] = (
    Prune('plugins.mcp_server.*'),
    Tag('plugins.mcp_server.*', 'provider', (('url', 'http'), ('command', 'stdio'))),
)
"""Each MCP server of 0.3 under ``[plugins.mcp_server]`` gets the provider that its keys say, without the other's."""
MCP_TABLES: tuple[Step, ...] = (Move('mcp.*', 'plugins.mcp_server.*'), *MCP_PROVIDERS)
"""The MCP servers, as ``[plugins.mcp_server.<name>]`` with the provider that their keys say."""


@deprecated.bridge('0.5.0', '[mcp.<name>] is [plugins.mcp_server.<name>]: delete mcp_tables and MCP_TABLES')
def mcp_tables(toml: Path, raw: dict[str, object], removed_in: Version) -> None:
    """``[mcp.<name>]`` is ``[plugins.mcp_server.<name>]``, with the provider that its keys say.

    ``provider = "http"`` with ``url``, ``provider = "stdio"`` with ``command``. Its
    ``PILOOP_CAMPAIGN_MCP__<NAME>__<KEY>`` variables move with it.
    """
    deprecated.moved(toml, raw, MCP_TABLES, 'PILOOP_CAMPAIGN_', removed_in)
