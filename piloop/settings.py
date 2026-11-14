"""Process-wide settings. Every environment variable piloop reads is declared here, or named by a placeholder.

Also read from ``<PILOOP_HOME>/config.toml``, the global settings file (``[storage]``, say); an environment variable
wins over the file, and a nested key is ``PILOOP_<TABLE>__<KEY>`` (``PILOOP_STORAGE__MIN_FREE_GB``). A
``{env:NAME:default}`` placeholder in a value of ``piloop.toml`` (a usage glob) reads ``NAME`` when piloop fills it
in (``expand_env``).
"""

import hashlib
import os
import pwd
import re
import stat
import sys
import tomllib
from datetime import timedelta
from functools import cached_property
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    SecretStr,
    TypeAdapter,
    ValidationError,
    model_validator,
)
from pydantic_settings import (
    BaseSettings,
    InitSettingsSource,
    NoDecode,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from piloop import deprecated
from piloop.actor import Actor, System
from piloop.plugins.host import Plugins, PluginTables, Scope, campaign_defaults, refs, unknown, unreached
from piloop.problems import HomeConfigError, explain

DEFAULT_HOME = Path.home() / '.piloop'
REAL_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)
"""The user's home directory from the passwd database: a test that moves ``$HOME`` does not move it."""
UNDER_TEST_ENV = 'PILOOP_UNDER_TEST'
"""Set by piloop's test suite, and inherited by every piloop it starts: ``Settings`` refuses the home of a real
install, so a test can never open, let alone migrate, the database of a user's piloop."""
CONFIG_FILE = 'config.toml'
STORE_DIR = 'store'
INPUTS_DIR = 'inputs'
"""The read-only copies of the blobs of linked inputs that worktrees share (``piloop.store.tree.Copies``)."""
TMP_DIR = 'tmp'
"""``<home>/tmp``: files that wait to be moved into place, on the disk of the home."""
GB = 1024**3


class StorageSettings(BaseModel):
    """``[storage]``: how archiving uses the disk of this PILOOP_HOME. Where the blobs go is ``[plugins.cas]``."""

    model_config = ConfigDict(extra='forbid', frozen=True, use_attribute_docstrings=True)

    min_free_gb: float = Field(default=20, ge=0)
    """Free space, in GB, that archiving keeps on the disk of PILOOP_HOME. Storing a file writes its compressed copy
    first, so the daemon removes the copies of linked inputs that no job uses, then the workspace copies of archived
    files, oldest first, while the disk has less than this free, and it archives in batches that fit above it. A
    worker removes the copies of linked inputs that no job uses after each job, under the same rule."""

    @property
    def reserve(self) -> int:
        """``min_free_gb`` in bytes."""
        return int(self.min_free_gb * GB)


class EvaluationSettings(BaseModel):
    """``[evaluation]``: what the daemon allows the evaluate command of its campaigns."""

    model_config = ConfigDict(extra='forbid', frozen=True, use_attribute_docstrings=True)

    max_evidence_mb: float = Field(default=10, ge=0)
    """The evidence files that one run of the evaluate command may name in its result, in MB, all of them together.
    piloop keeps them with the evaluation's logs. A result with more is a crash."""

    stop_grace_seconds: float = Field(default=10, ge=0)
    """When a rung of the evaluation ladder stops a climb, piloop sends the evaluate command SIGTERM, then SIGKILL this
    many seconds later if it still runs. Put the command's cleanup in a SIGTERM handler."""

    infra_retry: timedelta = Field(default=timedelta(minutes=1), gt=timedelta(0))
    """How long an evaluation that failed for a cause that is not its candidate's waits before it runs again: an
    ``infra_error`` that the evaluate command reported, a worktree or inputs that piloop could not make, or an evaluate
    that another process killed twice. The wait doubles after each failure, up to ``infra_retry_max``. It runs again
    until it measures, someone cancels it (``piloop eval cancel``), a new evaluator replaces its own (the new one
    measures it then), or it failed ``stop_after_tries`` times. Seconds, or an ISO 8601 duration."""
    infra_retry_max: timedelta = Field(default=timedelta(minutes=30), gt=timedelta(0))
    """The longest wait between two runs of an evaluation that keeps failing for a cause that is not its candidate's
    (``infra_retry``)."""
    stop_after_tries: int = Field(default=3, ge=1)
    """Consecutive infrastructure failures before piloop pauses work. Submissions with the same committed single-line
    ``TASK`` ID share a count and pause evaluations of that task. Submissions without a task ID share a count and pause
    the entire campaign. After repair, use ``piloop candidate remeasure <id>`` for a task or ``piloop resume <campaign>`` for the campaign."""

    @property
    def max_evidence_bytes(self) -> int:
        """``max_evidence_mb`` in bytes."""
        return int(self.max_evidence_mb * 1024**2)


class SqlSettings(BaseModel):
    """``[sql]``: the limits of ``piloop sql``. They bind every caller: the agents, the orchestrator and you."""

    model_config = ConfigDict(extra='forbid', frozen=True, use_attribute_docstrings=True)

    statement_seconds: float = Field(default=5, gt=0)
    """How long one statement may run, in seconds. A longer one is interrupted, and changes nothing. A wait for the
    write lock does not count."""

    team_mb: float = Field(default=256, gt=0)
    """The size of a campaign's team tables (``team.db``), in MiB. A write that would grow the file past it fails with
    ``database or disk is full``."""

    @property
    def team_bytes(self) -> int:
        """``team_mb`` in bytes."""
        return int(self.team_mb * 1024**2)


class TracingSettings(BaseModel):
    """``[tracing]``: what this home measures for the spans that it exports (``OTEL_EXPORTER_OTLP_ENDPOINT``)."""

    model_config = ConfigDict(extra='forbid', frozen=True, use_attribute_docstrings=True)

    slow_write: timedelta = Field(default=timedelta(milliseconds=1), ge=timedelta(0))
    """A write transaction that lasts this long or longer, waiting for the database's write lock and holding it,
    exports a ``piloop.db.write`` span. A faster one exports nothing: its span costs about 35 µs, a fifth of a small
    write. 0 exports every write. Seconds, or an ISO 8601 duration such as ``PT0.001S``."""


class DaemonSettings(BaseModel):
    """``[daemon]``: the timings of the daemon's loop and of its launches on this home."""

    model_config = ConfigDict(extra='forbid', frozen=True, use_attribute_docstrings=True)

    feed_page: int = Field(default=1000, gt=0)
    """How many events a turn of the daemon's loop reads at most. A turn with more reads again at once."""
    prompt_retry: timedelta = Field(default=timedelta(seconds=2), gt=timedelta(0))
    """How soon a comment that the executor did not take, because its database was locked, goes to its agent again."""
    kill_retry: timedelta = Field(default=timedelta(seconds=10), gt=timedelta(0))
    """How soon the kill of an agent that the daemon ended goes again while the agent has not ended: a kill can fail."""
    home_check: timedelta = Field(default=timedelta(seconds=10), gt=timedelta(0))
    """How often the daemon checks that its home is still there. A daemon whose home is gone stops: no client can find
    it there to stop it."""
    inbox_page: int = Field(default=500, gt=0)
    """How many events one read of the inbox's news examines at most (``piloop inbox wait``). A read with more reads
    again at once."""
    token_trust: timedelta = Field(default=timedelta(seconds=10), ge=timedelta(0))
    """How long a server (``piloop serve``, ``piloop cas serve``) trusts a token that it verified before it reads the
    token again. A token revoked through the server's own API stops at once; one revoked in another process, or an
    agent's whose session ended, within this time. 0 reads the token on every call."""
    api_processes: Annotated[int, Field(ge=0)] | Literal['auto'] = 'auto'
    """How many processes serve the daemon's API beside it, on the sockets that it binds. 0: the daemon serves it in
    its own process. ``auto``: half the cores that the daemon may run on (its CPU affinity), up to 8, on Linux, and 0
    on other systems, where the relayed calls do not fork from a template. Each one holds its own connections and its relay template, and the
    relayed calls (``[plugins.executor] relayed_calls``) are shared out among them, so there are no more of them than
    relayed calls."""
    api_start: timedelta = Field(default=timedelta(seconds=60), gt=timedelta(0))
    """How long an API process may take to serve once it started. The daemon does not start when one of the first ones
    does not serve in time, or says why it cannot."""
    api_respawn: timedelta = Field(default=timedelta(seconds=1), ge=timedelta(0))
    """How long the daemon waits before it starts an API process in place of one that exited: one that crashes at once
    does not spin."""
    api_respawns: int = Field(default=3, gt=0)
    """How many API processes started again in a row may fail to serve before the daemon stops."""
    advertise_url: str | None = None
    """Where a standby on another host reaches this daemon while it holds the home's term, such as
    ``http://piloop-0.piloop:8443``: what the term names (``piloop.plugins.leader``). None: the network's ``listen``,
    with this host's name in place of a wildcard address. Never a load balancer's URL, which may lead to the standby."""
    forward_refresh: timedelta = Field(default=timedelta(seconds=2), gt=timedelta(0))
    """How often a standby reads which daemon holds the home's term, which it forwards each request to. Its 503s carry
    a Retry-After of this, rounded up."""
    forward_connect: timedelta = Field(default=timedelta(seconds=5), gt=timedelta(0))
    """How long a standby waits to connect to the holder before it answers 503. Once connected, it waits as long as the
    holder takes, a long-poll's wait too, until the client goes or the term ends."""
    agent_reread: timedelta = Field(default=timedelta(seconds=60), gt=timedelta(0))
    """How often the daemon reads an agent's row again while it waits for its end, without an event that wakes it: a
    close that adds no event, by ``piloop sql`` say, is seen this late."""

    @property
    def api_process_count(self) -> int:
        """``api_processes``, with ``auto`` resolved on this host (``auto_api_processes``)."""
        if self.api_processes == 'auto':
            return auto_api_processes(sys.platform, os.process_cpu_count())
        return self.api_processes


AUTO_API_PROCESSES_MAX = 8
"""The most API processes that ``[daemon] api_processes = "auto"`` starts: as many as the default
``[plugins.executor] relayed_calls``, each of which needs one of them."""


def auto_api_processes(platform: str, cpus: int | None) -> int:
    """How many API processes ``auto`` starts on a host of ``platform`` (``sys.platform``) with ``cpus`` usable cores.

    Half the cores, up to ``AUTO_API_PROCESSES_MAX``, on Linux. None elsewhere: there each relayed call is a process
    of its own, not forked from a template (``piloop.plugins.executor.workers.FORKED``), and the daemon serves a
    laptop's agents alone.
    """
    return min(AUTO_API_PROCESSES_MAX, (cpus or 0) // 2) if platform == 'linux' else 0


class AgentSettings(BaseModel):
    """``[agents]``: how the daemon steers and ends the agents of this home.

    A campaign's ``[agents]`` in its ``piloop.toml`` can set ``wrap_up_grace`` and ``brief_words`` for its own agents.
    """

    model_config = ConfigDict(extra='forbid', frozen=True, use_attribute_docstrings=True)

    wrap_up_grace: timedelta = Field(default=timedelta(minutes=10), ge=timedelta(0))
    """How long an agent that was told to wrap up, because its campaign stopped or someone cancelled it, has to save
    its work before the daemon ends it."""
    steer_line_chars: int = Field(default=200, gt=1)
    """How many characters of a comment's first line a running agent gets, in the one line that the daemon sends it
    for the comment. The agent can read the rest with SQL."""
    brief_words: int = Field(default=400, ge=1)
    """How many words a campaign's ``brief.md`` may have before ``piloop init`` and ``piloop doctor`` warn. A brief that
    grows rule by rule teaches agents caution instead of saving scored versions: rewrite it to this size."""
    exit_output_chars: int = Field(default=500, ge=0)
    """How many characters of its last output lines an agent that exits non-zero leaves in its error, which
    ``piloop status`` and the escalation of paused launches show, and one that exits 0 early with nothing done in the
    escalation. The values of its secrets read as their names. 0 leaves none."""
    startup_seconds: float = Field(default=120, ge=0)
    """An agent that ends this soon after its start and left nothing (no change and no comment) never worked: it failed
    at startup, a fault of the host or the harness, not an attempt. Unless it took more than ``startup_turns``."""
    startup_turns: int = Field(default=3, ge=0)
    """An agent whose harness counted more model turns than this worked, however soon it ended and whatever it left:
    an agent whose launch failed reads its brief and stops in a turn or two, and one that tries and gives up takes more."""
    detail_chars: int = Field(default=160, gt=1)
    """How many characters of a comment or an error one line of a brief shows: an inbox item, an incident."""
    attach_lines: int = Field(default=40, gt=0)
    """How many lines of an agent's output ``piloop attach`` prints."""


class WatchSettings(BaseModel):
    """``[watch]``: how ``piloop watch`` reads the API, and how much it shows."""

    model_config = ConfigDict(extra='forbid', frozen=True, use_attribute_docstrings=True)

    wait: timedelta = Field(default=timedelta(seconds=20), gt=timedelta(0), le=timedelta(seconds=25))
    """How long the server holds each read of the event feed while no event comes. The API holds one 25 s at most."""
    retry: timedelta = Field(default=timedelta(seconds=2), gt=timedelta(0))
    """How soon a read of the event feed that failed, because the server did not answer, goes again."""
    tail: int = Field(default=500, ge=0, le=1000)
    """How many events before the first read the campaigns screen reads, to show each campaign's last event."""
    agent_lines: int = Field(default=60, gt=0, le=1000)
    """How many of the selected agent's last lines of output its detail shows."""
    candidates: int = Field(default=50, gt=0)
    """How many of a campaign's own candidates its panel lists, newest first."""
    spark: int = Field(default=40, gt=1)
    """How many of the last new bests the sparkline of the candidates panel draws."""
    notice: timedelta = Field(default=timedelta(seconds=6), gt=timedelta(0))
    """How long a note stays on the screen: what a key did, or why a read failed."""


SUN_PATH = 104 if sys.platform == 'darwin' else 108
"""The bytes of a Unix socket's path, its NUL included (``sockaddr_un.sun_path``): 104 on macOS, 108 on Linux."""
SHORT_SOCKETS = Path('/tmp')
"""Where a socket goes whose path in its home is too long: ``/tmp/piloop-<uid>/``, as tmux keeps ``/tmp/tmux-<uid>/``."""
RELAY_SOCKET = 'relay/piloop.sock'
"""``<home>/relay/piloop.sock``: a worker's relay, in a directory that only the worker's user reaches."""


def socket_at(path: Path) -> Path:
    """``path``, or when it does not fit in a Unix socket address, a short path that only it maps to.

    The short one is ``SHORT_SOCKETS/piloop-<uid>/<hash of path>.sock``, so the server and its clients find it from the
    home alone, whatever their environment. ``socket_dir`` makes its directory.
    """
    if len(os.fsencode(path)) < SUN_PATH:
        return path
    digest = hashlib.blake2b(os.fsencode(path), digest_size=10).hexdigest()
    return SHORT_SOCKETS / f'piloop-{os.getuid()}' / f'{digest}.sock'


def socket_dir(path: Path) -> None:
    """Make the directory of the socket ``path`` (``socket_at``), and check a short one.

    ``SHORT_SOCKETS`` is shared with other users, so a short socket's directory must be this user's, reached by it only.

    Raises:
        PermissionError: The short directory is a link, another user's, or open to others: someone else could take
            the socket's place.
    """
    directory = path.parent
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.parent != SHORT_SOCKETS:
        return
    found = directory.lstat()
    if not stat.S_ISDIR(found.st_mode) or found.st_uid != os.getuid() or found.st_mode & 0o077:
        raise PermissionError(
            f'{directory} is not a directory that only this user reaches (mode {stat.filemode(found.st_mode)}, owner'
            f' {found.st_uid}): remove it, and piloop makes it again'
        )


class Settings(BaseSettings):
    """Settings of the daemon and the CLI. ``PILOOP_*`` environment variables override them."""

    model_config = SettingsConfigDict(
        env_prefix='PILOOP_', env_nested_delimiter='__', frozen=True, use_attribute_docstrings=True
    )

    home: Path = DEFAULT_HOME
    """The directory that holds the database, the artifact store and the directories of each run (worktrees, logs,
    workspaces)."""

    actor: Annotated[Actor, NoDecode] = System.HUMAN
    """Who this process is. Every row it updates records this value in ``updated_by``. The daemon launches each agent
    with ``<role>#<agent_session_id>``, for example ``implementer#12``."""

    agent_token: SecretStr | None = None
    """The token of the agent that the daemon launched as ``actor``, which its worker sets at its start: its calls act
    as that agent with it, and as nobody else."""

    server: str | None = None
    """The daemon that the commands which call the API ask, such as ``https://piloop.example.com``: its network's
    client dials it, with the credential of this machine. Unset: the daemon of this home, on its socket."""

    messages: Path | None = None
    """The queue of messages for this agent's turn, set by a runtime that keeps one. ``piloop agent messages`` takes
    them."""

    plugins: dict[str, dict[str, JsonValue]] = {}
    """``[plugins.<capability>]``: the instance of each capability of this home that ``config.toml`` selects, its
    provider and its parameters, or ``[plugins.<capability>.<name>]``: the instances of one that has several, by name.
    ``providers`` validates them."""

    migration_backups: int = Field(default=3, ge=0)
    """The number of copies of the database to keep, one written before each migration: ``piloop.db.bak-<revision>-<UTC
    time>`` next to it, the database as that revision left it. A new copy removes the oldest beyond this number. 0
    writes none. Each copy is about the size of the database."""

    otel_exporter_otlp_endpoint: str | None = Field(default=None, validation_alias='OTEL_EXPORTER_OTLP_ENDPOINT')
    """The standard OTLP endpoint. piloop exports traces only when this is set."""

    uv_tool_dir: Path = Field(default=Path.home() / '.local' / 'share' / 'uv' / 'tools', validation_alias='UV_TOOL_DIR')
    """Where uv keeps its tools (uv's own ``UV_TOOL_DIR``). The default is uv's default when ``XDG_DATA_HOME`` is not
    set. ``piloop upgrade`` reads the ``uv-receipt.toml`` of the piloop tool there to learn where the CLI was installed
    from."""

    cli: str = 'piloop'
    """The command that runs this install. ``piloop doctor`` names it in what it tells you to run. A second install,
    such as a build of ``main`` next to a release, sets it in its wrapper (``PILOOP_CLI=piloop-canary``), so that an
    orchestrator restarts and upgrades that install, never the release."""

    auto_upgrade: bool = False
    """Whether the orchestrator may upgrade piloop by itself when ``piloop doctor`` reports a newer version at its
    source. Off, it asks you in the inbox: an upgrade migrates the database one way, and the runs after it are scored
    by the new code. A daemon that runs older code than the installed CLI is restarted either way."""

    debug: bool = False
    """Show the full traceback of an unexpected error in the CLI, instead of a short message."""

    agent_skills_dir: Path = Path.home() / '.agents' / 'skills'
    """The user directory of Agent Skills, which Codex, pi, Gemini CLI and Cursor read. piloop installs the orchestrator
    skills there for them."""

    claude_skills_dir: Path | None = None
    """The directory in which piloop installs the orchestrator skills for Claude Code. Unset, it is the one Claude Code
    reads: ``skills`` in its config directory (``CLAUDE_CONFIG_DIR``, by default ``~/.claude``). A second piloop on
    the same machine, such as a build of ``main`` next to a release, sets this and ``agent_skills_dir`` to directories
    of its own, so that its skills do not replace those of the release."""

    storage: StorageSettings = StorageSettings()
    """``[storage]`` in ``config.toml``: how archiving uses the disk of this home."""

    tracing: TracingSettings = TracingSettings()
    """``[tracing]`` in ``config.toml``: what this home measures for its exported spans."""

    evaluation: EvaluationSettings = EvaluationSettings()
    """``[evaluation]`` in ``config.toml``: what the daemon allows the evaluate command of its campaigns."""

    daemon: DaemonSettings = DaemonSettings()
    """``[daemon]`` in ``config.toml``: the timings of the daemon's loop and launches."""

    agents: AgentSettings = AgentSettings()
    """``[agents]`` in ``config.toml``: how the daemon steers and ends agents."""

    watch: WatchSettings = WatchSettings()
    """``[watch]`` in ``config.toml``: how ``piloop watch`` reads the API."""

    sql: SqlSettings = SqlSettings()
    """``[sql]`` in ``config.toml``: the limits of ``piloop sql``."""

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Arguments, then the environment, then ``config.toml`` under the home the arguments or environment name."""
        given = init_settings.init_kwargs.get('home') if isinstance(init_settings, InitSettingsSource) else None
        home = Path(given) if given else home_of_env()
        return init_settings, env_settings, InitSettingsSource(settings_cls, config_file(home))

    @model_validator(mode='after')
    def _not_a_real_home_under_test(self) -> Self:
        """Under test, refuse ``~/.piloop`` and its siblings (``~/.piloop-canary``) in the real home directory."""
        home = self.home.resolve()
        if UNDER_TEST_ENV in os.environ and home.parent == REAL_HOME.resolve() and home.name.startswith('.piloop'):
            raise ValueError(f'{home} is the home of a real install, and {UNDER_TEST_ENV} is set: use a temporary home')
        return self

    @cached_property
    def providers(self) -> Plugins:
        """``plugins``, validated on first use: the plugins cannot load while this module loads, and ``settings`` does.

        Raises:
            ValidationError: A table names no capability of the home, no provider of it, or parameters that the
                provider refuses.
            ValueError: A ``use`` names no instance, or a table configures an instance that no ``use`` names.
        """
        found = HOME_PLUGINS.validate_python(self.plugins)
        named = refs(found)
        if problems := [*unknown(named, found, Scope.HOME), *map(str, unreached(found, named))]:
            raise ValueError(f'{self.home / CONFIG_FILE}: {"; ".join(problems)}')
        return found

    @property
    def campaign_defaults(self) -> dict[str, object]:
        """The tables of ``plugins`` that are defaults of every campaign's: unvalidated (``host.campaign_defaults``)."""
        return campaign_defaults(self.plugins)

    @property
    def database(self) -> Path:
        """The home's database file."""
        return self.home / 'piloop.db'

    @property
    def database_url(self) -> str:
        """The SQLAlchemy URL of the database."""
        return f'sqlite:///{self.database}'

    @property
    def pid_file(self) -> Path:
        """The file with the pid of the running daemon. ``piloop serve`` writes it and removes it when it exits."""
        return self.home / 'serve.pid'

    @property
    def socket_file(self) -> Path:
        """The Unix socket that the running daemon serves the API on, to this user only. Every client command calls it.

        ``<home>/piloop.sock``, or a short path for it when that is too long for a socket (``socket_at``).
        """
        return socket_at(self.home / 'piloop.sock')

    @property
    def relay_file(self) -> Path:
        """The Unix socket of this home's worker relay, which its agents' ``piloop`` calls go through (``socket_at``)."""
        return socket_at(self.home / RELAY_SOCKET)

    @property
    def code_file(self) -> Path:
        """The digest of the code that the running daemon started on (``code_digest``), for ``piloop doctor``."""
        return self.home / 'serve.code'

    @property
    def log_file(self) -> Path:
        """The file that ``piloop serve --detach`` appends the daemon's stdout and stderr to."""
        return self.home / 'serve.log'


HOME_PLUGINS: TypeAdapter[Plugins] = TypeAdapter(
    Annotated[Plugins, PluginTables(Scope.HOME)], config=ConfigDict(title='[plugins]')
)


def home_of_env() -> Path:
    """Return the piloop home that the environment names (``PILOOP_HOME``), else the default."""
    return Path(os.environ.get('PILOOP_HOME', DEFAULT_HOME))


def config_file(home: Path) -> dict[str, object]:
    """Return the parsed ``config.toml`` under ``home``, its deprecated keys bridged, or nothing without the file.

    Raises:
        HomeConfigError: The file is not valid TOML, or sets a key that a release removed.
    """
    path = home / CONFIG_FILE
    if not path.is_file():
        return {}
    try:
        raw = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as e:
        raise HomeConfigError(f'{path} is not valid TOML: {e}') from e
    if removed := deprecated.removed_keys(path, raw, Scope.HOME):
        raise HomeConfigError('\n'.join(removed))
    deprecated.apply(path, raw, deprecated.bridges(Scope.HOME))
    return raw


ENV_PLACEHOLDER = re.compile(r'\{env:(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?::(?P<default>[^}]*))?\}')
"""``{env:NAME:default}``: the value of the environment variable ``NAME``, or ``default`` when it is unset or empty."""


def expand_env(text: str) -> str:
    """Fill in the ``{env:NAME:default}`` placeholders of ``text``. A default that starts with ``~`` is in the home."""

    def value(m: re.Match[str]) -> str:
        return os.environ.get(m['name']) or os.path.expanduser(m['default'] or '')

    return ENV_PLACEHOLDER.sub(value, text)


def env_names(text: str) -> list[str]:
    """Return the environment variables that the placeholders of ``text`` name, once each, in order."""
    return list(dict.fromkeys(m['name'] for m in ENV_PLACEHOLDER.finditer(text)))


def load() -> Settings:
    """The settings of the home that the environment names.

    Raises:
        HomeConfigError: Its ``config.toml`` or a ``PILOOP_*`` variable does not load.
    """
    try:
        return Settings()
    except ValidationError as e:
        raise _not_loaded(e) from e


def reload(found: Settings) -> None:
    """Read ``found`` again, in place, from this process's environment: every module holds the same object.

    Raises:
        HomeConfigError: Its ``config.toml`` or a ``PILOOP_*`` variable does not load.
    """
    try:
        found.__init__()
    except ValidationError as e:
        raise _not_loaded(e) from e


def _not_loaded(error: ValidationError) -> HomeConfigError:
    return HomeConfigError(explain(f'{home_of_env() / CONFIG_FILE} (or a PILOOP_* variable)', error))


settings = load()
