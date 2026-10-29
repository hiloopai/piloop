"""``pi``: the pi coding agent, the harness of a campaign that does not set ``harnesses``."""

import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from piloop.plugins.harness.contract import (
    EFFORT,
    MCP,
    PROMPT,
    SESSION_ID,
    USER_CONFIG,
    Harness,
    McpArgs,
    McpLaunch,
    ModelId,
)
from piloop.plugins.harness.kit.jsonl import (
    Counts,
    JsonlSessions,
    Record,
    add,
    as_int,
    child,
    dash_separators_wrapped,
    texts,
    zero,
)
from piloop.plugins.host import ContractBroken
from piloop.plugins.mcp_server.contract import McpServer
from piloop.plugins.mcp_server.http import HttpServer
from piloop.plugins.mcp_server.stdio import StdioServer
from piloop.settings import expand_env

ARGV = [
    '-p',
    '--mode',
    'json',
    '--no-extensions',
    USER_CONFIG,
    '--approve',
    '--session-id',
    SESSION_ID,
    '--model',
    '{model}',
    '--thinking',
    EFFORT,
    MCP,
    PROMPT,
]
"""The argv of a headless run, and of a resumed one: ``--session-id`` continues the session when it exists (verified
with pi 0.99.2), by the id in its header under the folder of the cwd. There is no ``--session-dir``: the usage is read
from pi's own store."""
ADAPTER = 'npm:pi-mcp-adapter@4.0.0'
"""Pinned, because version 5.0 turns pi's built-in MCP off in the user's own pi settings when it first starts: an
agent would change how the user's pi works. The exclusive mode is in the adapter's code, not in its README.

TODO: move the pin when a release leaves the user's settings alone, or drop the adapter when pi's built-in MCP takes
a config for one launch."""
CONFIG_FILE = 'mcp.json'
NO_EXTENSIONS = ('--no-extensions', '-ne')
ISOLATION = ['--no-skills', '--no-context-files']
"""The user's skills and context files (``AGENTS.md``). Extensions stay off whatever ``user_config`` says: piloop pins
the one that its agents need (``ADAPTER``), and with the user's own on, pi's built-in MCP loads the user's servers."""
THINKING = frozenset({'off', 'minimal', 'low', 'medium', 'high', 'xhigh'})
"""The thinking levels that pi takes after a model, as ``sonnet:high``."""


def server(s: McpServer) -> dict[str, object]:
    """One entry of the adapter's ``mcpServers``."""
    match s:
        case HttpServer(url=url):
            headers = s.expanded_headers()
            return {'url': url, **({'headers': headers} if headers else {})}
        case StdioServer(command=[executable, *args]):
            env = s.expanded_env()
            return {'command': executable, 'args': args, **({'env': env} if env else {})}
        case _:
            raise ContractBroken(
                f'{type(s).__qualname__} is neither HttpServer nor StdioServer: the transports that pi takes'
            )


@dataclass(frozen=True)
class PiMcp:
    """Through pi-mcp-adapter, in exclusive mode.

    pi's built-in MCP has no config for one launch. So the renderer writes the role's servers to ``mcp.json`` in the
    agent's log directory (with variable names only), and loads the adapter on that file in exclusive mode. In this
    mode the adapter reads that file and nothing else.

    Only an argv that turns pi's own extensions off can take servers. With them on, a user who installed the adapter
    gets it twice (and pi does not start), and pi's built-in MCP loads the user's ``mcp.json``. API keys are
    ``${NAME}`` references in the headers and in a stdio server's env, which the adapter expands from pi's environment.
    """

    def render(self, launch: McpLaunch) -> McpArgs:
        """Nothing without servers: piloop's headless pi then has no MCP at all."""
        if not launch.servers:
            return McpArgs([])
        config = launch.log_dir / CONFIG_FILE
        config.write_text(json.dumps({'mcpServers': {name: server(s) for name, s in launch.servers.items()}}, indent=2))
        return McpArgs(['-e', ADAPTER, '--mcp-config', str(config)], {'PI_MCP_CONFIG_MODE': 'exclusive'})

    def refuses(self, argv: list[str]) -> str | None:
        """Refuse an argv that keeps pi's extensions on."""
        if any(flag in argv for flag in NO_EXTENSIONS):
            return None
        return (
            "pi takes MCP servers only with its own extensions off (--no-extensions): with them on, pi's built-in MCP"
            " loads the user's servers and an installed pi-mcp-adapter loads twice. Run pi agents of this role under"
            ' the subprocess runtime, or leave pi out of its harnesses'
        )


def tally(records: Iterable[Record]) -> tuple[Counts, int]:
    """Add up the ``usage`` of every assistant message. ``total`` comes from ``totalTokens``."""
    counts = zero()
    turns = 0
    for rec in records:
        message = child(rec, 'message')
        if rec.get('type') != 'message' or message is None or message.get('role') != 'assistant':
            continue
        usage = child(message, 'usage')
        if usage is None:
            continue
        turns += 1
        add(counts, usage, input='input', output='output', cache_read='cacheRead', cache_write='cacheWrite')
        counts.total += as_int(usage.get('totalTokens'))
    return counts, turns


def said(rec: Record) -> str | None:
    """The ``text`` blocks of an assistant message. ``toolCall`` and thinking blocks are not text for the human."""
    message = child(rec, 'message')
    if rec.get('type') != 'message' or message is None or message.get('role') != 'assistant':
        return None
    return texts(message.get('content'), 'text')


class Pi(Harness):
    """``pi`` in JSON mode, with its extensions, skills and context files off, and the session id that piloop makes.

    Its session file is ``<home>/sessions/--<cwd>--/*_<session id>.jsonl``, with ``message.usage`` on every assistant
    message. The convention is Anthropic's: ``input`` does not include cached tokens, and ``totalTokens`` is pi's own
    sum of the four counts. pi has no memory store of its own, and its bash tool has no default timeout.
    """

    DEFAULT: ClassVar[bool] = True

    executable: str = 'pi'
    headless: list[str] = ARGV
    resume: list[str] = ARGV
    home: str = '{env:PI_CODING_AGENT_DIR:~/.pi/agent}'

    def isolation(self) -> list[str]:
        return ISOLATION

    def model_id(self, model: str) -> ModelId:
        """The vendor of pi's ``<provider>/<id>`` is its provider, and a ``:<thinking>`` level is no part of it."""
        name, _, level = model.rpartition(':')
        bare = name if level in THINKING else model
        match bare.split('/', 1):
            case [vendor, named]:
                return ModelId(vendor, named)
            case _:
                return ModelId(None, bare)

    def mcp(self) -> PiMcp:
        return PiMcp()

    def sessions(self) -> JsonlSessions:
        return JsonlSessions(
            f'{self.home}/sessions/{{cwd}}/*_{{session_id}}.jsonl', dash_separators_wrapped, tally, said
        )

    def restore(self, session_id: str, cwd: Path) -> Path:
        """A file under the session folder of ``cwd``, with a timestamped name.

        ``--session-id`` finds it by the id in its header: the time says nothing (measured with pi 0.99.2).
        """
        name = f'sessions/{dash_separators_wrapped(cwd)}/2000-01-01T00-00-00-000Z_{session_id}.jsonl'
        return Path(expand_env(f'{self.home}/{name}')).expanduser()

    def relocated(self, session: bytes, cwd: Path) -> bytes:
        """The session with ``cwd`` in its header, as pi's own ``--fork`` writes it.

        pi runs a session in the cwd of its header, and refuses one that is gone (measured with pi 0.99.2).

        Raises:
            ValueError: The first line is no session header.
        """
        first, newline, rest = session.partition(b'\n')
        header = json.loads(first)
        if not isinstance(header, dict) or header.get('type') != 'session':
            raise ValueError(f'the first line of the session file is no session header: {first[:80]!r}')
        header['cwd'] = str(cwd)
        return json.dumps(header, separators=(',', ':'), ensure_ascii=False).encode() + newline + rest
