"""``claude``: Claude Code."""

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from piloop import deprecated
from piloop.plugins.harness.contract import (
    EFFORT,
    MCP,
    PROMPT,
    SESSION_ID,
    USER_CONFIG,
    Carried,
    Harness,
    McpArgs,
    McpLaunch,
    ModelId,
    UserConfig,
)
from piloop.plugins.harness.kit.jsonl import (
    Counts,
    JsonlSessions,
    Record,
    add,
    child,
    dash_non_alnum,
    texts,
    zero,
)
from piloop.plugins.host import ContractBroken
from piloop.plugins.mcp_server.contract import McpServer
from piloop.plugins.mcp_server.http import HttpServer
from piloop.plugins.mcp_server.stdio import StdioServer
from piloop.settings import Settings, expand_env

TAKE_MESSAGES = 'if [ -s "$PILOOP_MESSAGES" ]; then exec "$PILOOP_BIN" agent messages --harness claude; fi'
"""The ``PostToolUse`` hook of every Claude agent: it hands the agent the messages that wait for it, in its turn.

It checks the queue first, so a tool call with no message costs a file check, not a start of piloop."""
SETTINGS = json.dumps(
    {
        'autoMemoryEnabled': False,
        'env': {'BASH_DEFAULT_TIMEOUT_MS': '300000', 'BASH_MAX_TIMEOUT_MS': '600000'},
        'hooks': {'PostToolUse': [{'hooks': [{'type': 'command', 'command': TAKE_MESSAGES}]}]},
    },
    separators=(',', ':'),
)
"""``--settings`` for every Claude agent. They take precedence over the user's own settings.

Auto-memory is off. Claude keeps it per repo (``~/.claude/projects/<cwd>/memory``), so the notes of one agent would
load into every later agent of the campaign, outside piloop's database and brief.

A foreground Bash command moves to the background after 5 minutes, and after 10 minutes at most. Without this, a
user's long ``BASH_DEFAULT_TIMEOUT_MS`` lets an agent block its turn on one command for an hour.

A message reaches the turn in progress (``TAKE_MESSAGES``)."""


SETTINGS_FLAG = '--settings'
ISOLATION = ['--setting-sources', 'project']
"""Load the settings of the campaign repo only (``.claude/settings.json``), not the user's or a local file: no user
``CLAUDE.md``, skills, plugins, hooks or ``env``. The repo's own ``CLAUDE.md`` still loads, and so do ``--settings``
and ``--mcp-config`` (measured with Claude Code 2.1.310). The login is not a setting: it still works."""
AUTH_KEYS = ('apiKeyHelper', 'awsAuthRefresh', 'awsCredentialExport', 'forceLoginMethod', 'forceLoginOrgUUID')
AUTH_ENV = re.compile(
    r'ANTHROPIC_\w+|CLAUDE_CODE_USE_\w+|CLAUDE_CODE_SKIP_\w+_AUTH|AWS_\w+|CLOUD_ML_REGION|VERTEX_REGION_\w+'
    r'|HTTPS?_PROXY|NO_PROXY|NODE_EXTRA_CA_CERTS'
)
"""The keys of the user's ``settings.json``, and of its ``env``, that log Claude in or route it to a gateway."""


def argv(session: str) -> list[str]:
    """The argv of a headless run (``--session-id``) or of a resumed one (``--resume``)."""
    return [
        '-p',
        '--output-format',
        'json',
        session,
        SESSION_ID,
        '--model',
        '{model}',
        '--effort',
        EFFORT,
        '--dangerously-skip-permissions',
        MCP,
        USER_CONFIG,
        SETTINGS_FLAG,
        SETTINGS,
        PROMPT,
    ]


def server(s: McpServer) -> dict[str, object]:
    """One entry of ``mcpServers``."""
    match s:
        case HttpServer(url=url):
            headers = s.expanded_headers()
            return {'type': 'http', 'url': url, **({'headers': headers} if headers else {})}
        case StdioServer(command=[executable, *args]):
            env = s.expanded_env()
            return {'type': 'stdio', 'command': executable, 'args': args, **({'env': env} if env else {})}
        case _:
            raise ContractBroken(
                f'{type(s).__qualname__} is neither HttpServer nor StdioServer: the transports that Claude takes'
            )


@dataclass(frozen=True)
class ClaudeMcp:
    """``--strict-mcp-config --mcp-config '<inline JSON>'``.

    Strict mode gives the agent the role's servers and none of the user's. API keys are ``${NAME}`` references, which
    Claude expands from its own environment when it loads the config. ``--mcp-config`` takes several values, so
    another flag must follow ``{mcp}`` in the argv, not the prompt.
    """

    def render(self, launch: McpLaunch) -> McpArgs:
        """Strict mode also without servers, so that the agent then has no servers at all."""
        if not launch.servers:
            return McpArgs(['--strict-mcp-config'])
        config = {'mcpServers': {name: server(s) for name, s in launch.servers.items()}}
        return McpArgs(['--strict-mcp-config', '--mcp-config', json.dumps(config)])

    def refuses(self, argv: list[str]) -> str | None:
        """Every argv with ``{mcp}`` in it takes servers."""
        return None


def tally(records: Iterable[Record]) -> tuple[Counts, int]:
    """Add up the usage of each distinct ``message.id``. ``total`` is the sum of the four fields."""
    counts = zero()
    seen: set[object] = set()
    for rec in records:
        message = child(rec, 'message')
        if rec.get('type') != 'assistant' or message is None:
            continue
        usage = child(message, 'usage')
        if usage is None or message.get('id') in seen:
            continue
        seen.add(message.get('id'))
        add(
            counts,
            usage,
            input='input_tokens',
            output='output_tokens',
            cache_read='cache_read_input_tokens',
            cache_write='cache_creation_input_tokens',
        )
    counts.total = counts.input + counts.output + counts.cache_read + counts.cache_write
    return counts, len(seen)


def said(rec: Record) -> str | None:
    """The ``text`` blocks of an ``assistant`` line. Thinking and ``tool_use`` blocks are not text for the human."""
    message = child(rec, 'message')
    return texts(message.get('content'), 'text') if rec.get('type') == 'assistant' and message is not None else None


class Claude(Harness):
    """``claude -p`` with JSON output, the session id that piloop makes, and ``--settings`` of its own (``SETTINGS``).

    Its session file is ``<home>/projects/-<cwd>/<session id>.jsonl``. Streaming writes one ``assistant`` line for
    each content block, each with the usage of the whole response, so each ``message.id`` counts once. The convention
    is Anthropic's: ``input`` does not include cached tokens. Checked against Claude Code 2.1. Its hook takes the
    messages for the agent in the turn, and it reads user skills from its own directory only.
    """

    executable: str = 'claude'
    headless: list[str] = argv('--session-id')
    resume: list[str] = argv('--resume')
    home: str = '{env:CLAUDE_CONFIG_DIR:~/.claude}'
    # measured in the shell of a Claude Code 2.1 session; an agent with all of them empty still saves its transcript
    session_env: list[str] = [
        'CLAUDECODE',
        'CLAUDE_CODE_CHILD_SESSION',
        'CLAUDE_CODE_ENTRYPOINT',
        'CLAUDE_CODE_EXECPATH',
        'CLAUDE_CODE_MESSAGING_SOCKET',
        'CLAUDE_CODE_MESSAGING_TOKEN',
        'CLAUDE_CODE_SESSION_ATTENDED',
        'CLAUDE_CODE_SESSION_ID',
        'CLAUDE_EFFORT',
        'CLAUDE_PID',
    ]

    def model_id(self, model: str) -> ModelId:
        """``model`` at ``anthropic``, whose API it calls."""
        return ModelId('anthropic', model)

    def mcp(self) -> ClaudeMcp:
        return ClaudeMcp()

    def isolation(self) -> list[str]:
        return ISOLATION

    @deprecated.until(
        '0.5.0', "auth keys of the user's ~/.claude/settings.json pass to an isolated agent: delete carried"
    )
    def carried(self, args: list[str], resume_args: list[str]) -> Carried | None:
        """The login keys of the user's ``settings.json`` on this machine, which ``--setting-sources project`` skips.

        Its ``AUTH_KEYS`` go into piloop's ``--settings``, and the ``AUTH_ENV`` of its ``env`` into the agent's
        environment, never into the argv. None when the agent loads the user's settings, or they have none.
        """
        if self.user_config is UserConfig.LOAD or USER_CONFIG not in self.headless:
            return None
        found = Path(expand_env(f'{self.home}/settings.json')).expanduser()
        try:
            user = json.loads(found.read_text())
        except FileNotFoundError:
            return None
        except ValueError:  # Claude Code ignores a settings file that it cannot parse: so does its login
            return None
        if not isinstance(user, dict):
            return None
        keys = {k: user[k] for k in AUTH_KEYS if k in user}
        env = user.get('env')
        env = {k: str(v) for k, v in env.items() if AUTH_ENV.fullmatch(k)} if isinstance(env, dict) else {}
        if not keys and not env:
            return None
        return Carried(
            args=_with_settings(args, keys),
            resume_args=_with_settings(resume_args, keys),
            env=env,
            names=(*keys, *(f'env.{k}' for k in env)),
        )

    def sessions(self) -> JsonlSessions:
        return JsonlSessions(f'{self.home}/projects/{{cwd}}/{{session_id}}.jsonl', dash_non_alnum, tally, said)

    def restore(self, session_id: str, cwd: Path) -> Path:
        """Its session file under the project folder of ``cwd``.

        ``--resume`` finds a restored file by its id from any cwd (measured with Claude Code 2.1.310).
        """
        return Path(expand_env(f'{self.home}/projects/{dash_non_alnum(cwd)}/{session_id}.jsonl')).expanduser()

    @classmethod
    def messages(cls, lines: list[str]) -> str:
        """The output of a ``PostToolUse`` hook, which gives the agent the messages (``additionalContext``)."""
        return json.dumps(
            {'hookSpecificOutput': {'hookEventName': 'PostToolUse', 'additionalContext': '\n'.join(lines)}}
        )

    def skills_dir(self, settings: Settings) -> Path:
        """``claude_skills_dir``, else ``<home>/skills``."""
        return settings.claude_skills_dir or Path(expand_env(f'{self.home}/skills')).expanduser()


def _with_settings(argv: list[str], keys: dict[str, object]) -> list[str]:
    """``argv`` with ``keys`` under piloop's own ``--settings``, which win: Claude takes only the last ``--settings``."""
    if not keys or not argv:
        return argv
    if SETTINGS_FLAG not in argv:
        return [SETTINGS_FLAG, json.dumps(keys, separators=(',', ':')), *argv]
    at = argv.index(SETTINGS_FLAG) + 1
    return [*argv[:at], json.dumps({**keys, **json.loads(argv[at])}, separators=(',', ':')), *argv[at + 1 :]]
