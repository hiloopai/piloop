"""``http``: a streamable HTTP server."""

from typing import Self
from urllib.parse import urlsplit

from pydantic import model_validator

from piloop.ids import EnvName
from piloop.plugins.mcp_server.contract import McpServer

AUTHORIZATION = 'authorization'


class HttpServer(McpServer):
    """``provider = "http"``: a streamable HTTP server at ``url``. SSE is not supported, because Codex and pi reject it."""

    url: str
    """The URL of the server."""
    headers_env: dict[str, EnvName] = {}
    """Maps each header name to the environment variable whose value is sent in it, for example
    ``{ "x-api-key" = "EXA_API_KEY" }``."""
    bearer_token_env: EnvName | None = None
    """The environment variable whose value is sent as ``Authorization: Bearer <value>``."""

    @model_validator(mode='after')
    def _check(self) -> Self:
        if self.bearer_token_env and AUTHORIZATION in {h.lower() for h in self.headers_env}:
            raise ValueError('bearer_token_env sets Authorization; drop it from headers_env')
        return self

    @property
    def address(self) -> str:
        """``host:port`` of ``url``: what a sandbox's network lets through (``AgentSpec.reaches``)."""
        url = urlsplit(self.url)
        return f'{url.hostname}:{url.port or (443 if url.scheme == "https" else 80)}'

    @property
    def env_names(self) -> list[str]:
        return [*self.headers_env.values(), *([self.bearer_token_env] if self.bearer_token_env else [])]

    def expanded_headers(self) -> dict[str, str]:
        """The headers as ``${NAME}`` references, which Claude Code and pi-mcp-adapter expand in header values."""
        bearer = {'Authorization': f'Bearer ${{{self.bearer_token_env}}}'} if self.bearer_token_env else {}
        return {h: f'${{{name}}}' for h, name in self.headers_env.items()} | bearer
