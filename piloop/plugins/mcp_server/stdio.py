"""``stdio``: a server that the harness starts as a command."""

import shutil

from pydantic import Field

from piloop.ids import EnvName
from piloop.plugins.host import Check
from piloop.plugins.mcp_server.contract import McpServer


class StdioServer(McpServer):
    """``provider = "stdio"``: a server that the harness starts with ``command`` and talks to on its stdin and stdout."""

    command: list[str] = Field(min_length=1)
    """The command (argv), for example ``["python3", "/path/to/server.py"]``."""
    env: list[EnvName] = []
    """The environment variables that the server reads, for example ``["XAI_API_KEY"]``."""

    @property
    def env_names(self) -> list[str]:
        return list(self.env)

    def checks(self) -> list[Check]:
        """Its program is on PATH."""
        name = f'mcp server {self.command[0]}'
        if shutil.which(self.command[0]) is None:
            return [Check(name, 'not on PATH', 'install it, or fix its [plugins.mcp_server.*] table in piloop.toml')]
        return [Check(name, 'on PATH')]

    def expanded_env(self) -> dict[str, str]:
        """The server's environment as ``${NAME}`` references, for Claude Code and pi-mcp-adapter."""
        return {name: f'${{{name}}}' for name in self.env}
