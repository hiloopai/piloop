"""The MCP servers: the tools that a role's agents get (``[plugins.mcp_server.<name>]`` in ``piloop.toml``).

A server is a provider of ``MCP_SERVERS`` with its parameters: ``http``, a streamable HTTP server (``HttpServer``), or
``stdio``, a command (``StdioServer``). A role lists the servers that its agents get (``[plugins.role.<name>] mcp``), and each
harness provider renders them in its own flags (``Harness.mcp``). The harnesses know these two transports only, so a
package that ships a server subclasses one of them, with its own defaults.

API keys are never written down. A server names the environment variables that hold them, each the name of a secret
(``piloop.plugins.secrets``). Each renderer writes those *names* in its harness's own expansion syntax, and the agent's
job puts the secrets' values in the agent's environment. No value goes into a file, an argv or a log.
"""

from abc import ABC, abstractmethod
from collections.abc import Iterable
from typing import Annotated

from pydantic import StringConstraints

from piloop.plugins.host import Capability, Many, Parameters, Scope

type ServerName = Annotated[str, StringConstraints(pattern=r'^[A-Za-z0-9_-]+$')]
"""Letters, digits, ``_`` and ``-``. pi accepts these, and they are safe in Codex's ``-c mcp_servers.<name>`` key."""


class McpServer(Parameters, ABC):
    """``[plugins.mcp_server.<name>]``: one MCP server, with its ``provider``: ``http`` or ``stdio``.

    Do not write API keys here. Name the environment variables that hold them: each is the name of a secret
    (``piloop secret set``). piloop puts the secrets in the agent's environment, and the harness puts their values into
    the header or the server's environment.
    """

    @property
    @abstractmethod
    def env_names(self) -> list[str]:
        """Every environment variable that this server needs."""

    def missing(self) -> str | None:
        """The program that starts the server, when it is not on PATH, else None. ``piloop doctor`` names it."""
        return None


MCP_SERVERS = Capability(
    'mcp_server', McpServer, table=Many(Scope.CAMPAIGN), group='piloop.mcp_servers', noun='MCP server provider'
)
"""Every MCP server provider by name: ``http`` and ``stdio``, and the servers that packages ship."""

McpServerName = Annotated[ServerName, MCP_SERVERS.ref]
"""The name of an MCP server of the campaign: ``[plugins.mcp_server.<name>]``."""


def env_names(servers: Iterable[McpServer]) -> list[str]:
    """The environment variables that ``servers`` need, each once, in order."""
    return list(dict.fromkeys(name for s in servers for name in s.env_names))
