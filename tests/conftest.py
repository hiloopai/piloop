import tests.isolation  # noqa: F401  # first: piloop reads its home from the environment when it is imported

# isort: split
import contextlib
import json
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
import tomllib
import weakref
from collections.abc import Callable, Generator, Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime
from importlib.metadata import EntryPoint, entry_points
from pathlib import Path
from typing import Concatenate

import httpx2 as httpx
import psutil
import pytest
from git import Repo
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import TypeAdapter
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from piloop.actor import Agent, SignedAgent, System
from piloop.campaign.campaigns import CampaignAdded
from piloop.campaign.stop import left
from piloop.config import CampaignConfig, load_campaign_config
from piloop.daemon.api import Handlers, daemon_state, with_api
from piloop.daemon.main import ACTOR
from piloop.daemon.scheduler import Scheduler
from piloop.db.events import in_order
from piloop.db.models import AgentSession, AgentSessionState, Campaign, Comment, Event, utcnow
from piloop.db.session import Database, migrate
from piloop.ids import AgentSessionId, CampaignId, RoleName
from piloop.net.server import Server, app, local_listener, url
from piloop.net.tokens import Tokens, mint_agent
from piloop.otel import Tracer
from piloop.plugins.archive.local import store as archive_module
from piloop.plugins.campaign_type.consumers import Campaigns
from piloop.plugins.campaign_type.kit.backlog import ops as backlog_ops
from piloop.plugins.campaign_type.kit.backlog.ids import IdeaId
from piloop.plugins.campaign_type.kit.backlog.wire import BacklogClient
from piloop.plugins.campaign_type.kit.candidates.ids import Origin, Outcome
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.candidates.wire import CandidatesClient
from piloop.plugins.campaign_type.kit.evaluation.evaluate import Measured, run_evaluate
from piloop.plugins.campaign_type.kit.evaluation.measure import current_evaluator
from piloop.plugins.campaign_type.kit.evaluation.tables import Measurement
from piloop.plugins.campaign_type.kit.evaluation.wire import EvaluationClient
from piloop.plugins.campaign_type.kit.runs.controller import RunController
from piloop.plugins.campaign_type.kit.runs.ids import RunEnd, RunEnding, RunId, RunStatus
from piloop.plugins.campaign_type.kit.runs.ops import end_run
from piloop.plugins.campaign_type.kit.runs.tables import Run, latest_agent_session
from piloop.plugins.campaign_type.kit.runs.wire import RunsClient
from piloop.plugins.campaign_type.kit.verdicts.wire import VerdictsClient
from piloop.plugins.campaign_type.optimization.planner import OptimizationPlanner
from piloop.plugins.campaign_type.optimization.wire import OptimizationClient
from piloop.plugins.cas.contract import CAS, Cas
from piloop.plugins.contribution import collect, contributions_of, installed
from piloop.plugins.executor.kit.leases import Timing
from piloop.plugins.executor.workers import Workers, WorkersExecutor
from piloop.plugins.executor.workers.worker import Worker
from piloop.plugins.harness.contract import Usage, each_provider
from piloop.plugins.lineage.wire import LineageClient
from piloop.plugins.network.contract import NetworkConfig, NetworkHost
from piloop.plugins.network.https import TOKEN_ENV, Https
from piloop.plugins.role.contract import RoleConfig
from piloop.plugins.runtime.contract import Handle, Runtime, RuntimeFactory, make_runtime
from piloop.plugins.runtime.subprocess import SubprocessRuntime
from piloop.plugins.secrets.contract import HomeSecrets
from piloop.plugins.secrets.local import LocalSecrets
from piloop.settings import Settings, env_names
from piloop.store import tree as tree_module
from piloop.text import iso

# Harness stand-ins: any argv that edits the worktree and commits.
HARNESS_APPEND = '#!/bin/sh\necho line >> solve.txt\ngit commit -qam "add a line"\n'
END_RUN = '"$PILOOP_BIN" run end submitted\n'
"""The last line of a worker that is done: it ends its run, which the API that ``submitting`` serves takes."""
HARNESS_TWO_COMMITS = (
    '#!/bin/sh\necho a >> solve.txt\ngit commit -qam "first"\necho b >> solve.txt\ngit commit -qam "second"\n'
)
HARNESS_FORGETS_COMMIT = '#!/bin/sh\necho line >> solve.txt\n'
HARNESS_COMMITS_AND_LEAVES_DIRT = (
    '#!/bin/sh\necho line >> solve.txt\ngit commit -qam "the idea"\necho more >> solve.txt\n'
)
HARNESS_NOOP = '#!/bin/sh\ntrue\n'
HARNESS_FAILS = '#!/bin/sh\nexit 3\n'
HARNESS_SLEEPS = '#!/bin/sh\nsleep 30 &\necho $! > ../child.pid\nwait\n'
HARNESS_WORSE = '#!/bin/sh\n: > solve.txt\ngit commit -qam "wipe"\n'
HARNESS_TRY_AND_REVERT = (
    '#!/bin/sh\necho line >> solve.txt\ngit commit -qam "try"\ngit revert --no-edit HEAD >/dev/null\n'
)
HARNESS_PROMPT_TO_FILE = '#!/bin/sh\nprintf %s "$1" > prompt.txt\n'
WRITE_PID = 'echo $$ > ../agent.pid.part\nmv ../agent.pid.part ../agent.pid\n'
"""The agent's pid, by a rename: a test that finds the file never reads it empty."""

EVALUATE = [
    'sh',
    '-c',
    'printf \'{"metrics": {"score": %d, "size": %d}}\\n\' $(wc -l < solve.txt) $(wc -c < solve.txt)'
    ' >> "$PILOOP_RESULT_FILE"',
]


def reports(**metrics: str) -> str:
    """The shell command that reports ``metrics`` as an evaluate command's result line on ``$PILOOP_RESULT_FILE``.

    Each value is a shell word that prints a number, such as ``'$(wc -l < solve.txt)'`` or ``'1'``.
    """
    fields = ', '.join(f'"{name}": %s' for name in metrics)
    values = ' '.join(f'"{value}"' for value in metrics.values())
    return f'printf \'{{"metrics": {{{fields}}}}}\\n\' {values} >> "$PILOOP_RESULT_FILE"'


def escalation_sql(target_kind: str, target_id: object, author: str) -> str:
    """An agent's ``piloop sql`` INSERT of an escalation with its form, ready for a double-quoted shell word."""
    from piloop.plugins.escalation.structured import EXAMPLE  # here: tests import conftest before the plugins load

    form = EXAMPLE.model_dump_json().replace("'", "''")
    sql = (
        'INSERT INTO comments (target_kind, target_id, author, kind, priority, body, form) VALUES'
        f" ('{target_kind}', {target_id}, '{author}', 'escalation', 1, 'run the audit', '{form}')"
    )
    return sql.replace('\\', '\\\\').replace('"', '\\"').replace('$', '\\$').replace('`', '\\`')


def reports_py(**metrics: float) -> str:
    """The Python source of an ``evaluate.py`` that reports ``metrics`` as its result line."""
    return (
        'import json, os\n'
        f"open(os.environ['PILOOP_RESULT_FILE'], 'a').write(json.dumps({{'metrics': {metrics!r}}}) + '\\n')\n"
    )


TOML = """
name = "demo"
evaluate = {evaluate}
evaluate_timeout = 30
link = ["data"]

[plugins.runtime]
use = "subprocess"

[plugins.harness]
use = ["script"]

[plugins.role.implementer]
timeout = {implementer_timeout}
max_concurrent = {max_implementers}

[plugins.role.watchdog]
harnesses = ["script"]
model = {{ script = "watchdog-model" }}
timeout = {sweep_timeout}

[plugins.role.research]
timeout = {sweep_timeout}
max_ideas = 0
max_concurrent = 1

[[metrics]]
name = "score"
direction = "higher"
role = "primary"

[[metrics]]
name = "size"
direction = "lower"

[plugins.harness.script]
model = "test-model"
executable = "{script}"
headless = ["{{prompt}}"]
"""

WATCHDOG = RoleName('watchdog')
RESEARCH = RoleName('research')
"""The names of piloop's sweeping roles. Core names none of its roles but the implementer."""


def spent(total: int, turns: int = 1) -> Usage:
    """The usage of a test's agent that spent ``total`` tokens in ``turns`` turns."""
    return Usage(input=0, output=0, cache_read=0, cache_write=0, total=total, turns=turns)


def end_of(status: RunStatus) -> RunEnd | None:
    """The end of a test's run in ``status``: none while it is open, else the plain end of a run of that status."""
    match status:
        case RunStatus.QUEUED | RunStatus.RUNNING:
            return None
        case RunStatus.SUCCEEDED:
            return RunEnd.FINISHED
        case RunStatus.TIMED_OUT:
            return RunEnd.TIMED_OUT
        case RunStatus.FAILED:
            return RunEnd.FAILED


AGENT_TIMEOUT = 120
"""The role timeout of a test's scripted agents: far past what a loaded host takes to run a script, so only a test of
a timeout, which sets its own, ever reaches it."""


def write_script(path: Path, body: str) -> Path:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def write_toml(
    campaign: Path,
    harness_body: str,
    *,
    max_implementers: int = 1,
    implementer_timeout: float = AGENT_TIMEOUT,
    evaluate: list[str] = EVALUATE,
) -> None:
    script = write_script(campaign.parent / 'harness.sh', harness_body)
    (campaign / 'piloop.toml').write_text(
        TOML.format(
            evaluate=json.dumps(evaluate),
            implementer_timeout=implementer_timeout,
            sweep_timeout=AGENT_TIMEOUT,
            max_implementers=max_implementers,
            script=script,
        )
    )


def package(root: Path, name: str, version: str, entry_points: str) -> None:
    """Install a distribution called ``name`` at ``version`` under ``root``, with only ``entry_points`` in it.

    Put ``root`` on ``sys.path`` and ``importlib.metadata`` finds its entry points, as those of an installed package.
    """
    info = root / f'{name.replace("-", "_")}-{version}.dist-info'
    info.mkdir(parents=True)
    (info / 'METADATA').write_text(f'Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n')
    (info / 'entry_points.txt').write_text(entry_points)


EXAMPLES = Path(__file__).parent.parent / 'examples' / 'plugins'
"""The example plugin packages. Their modules import (``pythonpath`` in pyproject.toml); ``example`` installs one."""


def piloop_entry_points(group: str) -> list[EntryPoint]:
    """piloop's own entry points of ``group``: its built-in providers, which every test that fakes the others keeps."""
    return [ep for ep in entry_points(group=group) if ep.dist is not None and ep.dist.name == 'piloop']


def example(root: Path, name: str) -> None:
    """Install the example plugin ``examples/plugins/<name>`` under ``root``, with the entry points of its pyproject."""
    campaign = tomllib.loads((EXAMPLES / name / 'pyproject.toml').read_text())['project']
    groups = campaign['entry-points']
    entries = ''.join(f'[{g}]\n' + ''.join(f'{n} = {v}\n' for n, v in eps.items()) for g, eps in groups.items())
    package(root, campaign['name'], campaign['version'], entries)


def pytest_addoption(parser: pytest.Parser) -> None:
    """The options of tests/e2e. They are here because pytest reads options only from the root conftest."""
    group = parser.getgroup('e2e', 'the end-to-end campaign (make e2e)')
    group.addoption('--e2e-harness', default='claude', help='the harness of every agent (default: claude)')
    group.addoption('--e2e-model', help="the harness's model (default: its cheapest, claude-haiku-4-5 for claude)")
    group.addoption(
        '--e2e-mode',
        choices=('local', 'distributed'),
        action='append',
        help='run the scenarios in this mode only; give it twice for both (default: both)',
    )
    group.addoption('--e2e-server', help='the URL of the server of tests/e2e/test_remote.py; without it, it is skipped')
    group.addoption(
        '--e2e-secret',
        action='append',
        help="a secret of the server that the remote scenario's harness gets in its environment. Repeatable.",
    )
    group.addoption('--e2e-campaign', help="the name of the remote scenario's campaign (default: e2e-remote)")


@pytest.fixture(autouse=True)
def no_real_skill_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every built-in harness has its home under tmp_path, so every piloop a test starts installs skills there."""
    for name, harness in each_provider().items():
        for variable in env_names(harness.home or ''):
            monkeypatch.setenv(variable, str(tmp_path / 'harnesses' / name))
    monkeypatch.setenv('PILOOP_AGENT_SKILLS_DIR', str(tmp_path / 'agents-skills'))


@pytest.fixture
def campaign_path(tmp_path: Path) -> Path:
    """A git repo with solve.txt (one line), an untracked data/ dir, brief.md and a piloop.toml using HARNESS_APPEND."""
    path = tmp_path / 'campaign'
    path.mkdir()
    (path / 'solve.txt').write_text('base\n')
    (path / 'brief.md').write_text('# Demo\nAdd lines.\n')
    (path / 'data').mkdir()
    (path / 'data' / 'x.csv').write_text('1\n')
    (path / '.gitignore').write_text('data\nworkspace\n')
    write_toml(path, HARNESS_APPEND)
    repo = Repo.init(path, initial_branch='main')
    repo.git.add('-A')
    repo.git.commit('-qm', 'baseline')
    return path


@pytest.fixture(scope='session')
def schema(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A database that ``migrate`` brought to head, once per session: a copy of it starts at head for free.

    Migrating from scratch costs about a second (each DDL statement of every revision commits on its own), and most
    tests need a database. ``tests/db/test_migrations.py`` still migrates from scratch.
    """
    path = tmp_path_factory.mktemp('schema') / 'piloop.db'
    database = Database(f'sqlite:///{path}', actor=ACTOR)
    migrate(database.engine, cas=disk(path.parent), backups=0, daemon=None)
    database.close()
    return path


def at_head(schema: Path, path: Path) -> Path:
    """Put a copy of the ``schema`` database at ``path``; returns ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(schema, path)
    return path


@pytest.fixture(autouse=True)
def repos(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Close each GitPython repo that the test opened, once the test ends.

    GitPython keeps a ``git cat-file`` running for a repo that read an object, and an unclosed repo is cyclic garbage:
    only the GC ends it, and it can finalize the ``Popen`` first, which warns of a subprocess that still runs. A list
    of weak references, not a ``WeakSet``: two repos of one path are equal.
    """
    made: list[weakref.ref[Repo]] = []

    def tracking[**P](init: Callable[Concatenate[Repo, P], None]) -> Callable[Concatenate[Repo, P], None]:
        def tracked(self: Repo, *args: P.args, **kwargs: P.kwargs) -> None:
            init(self, *args, **kwargs)
            made.append(weakref.ref(self))

        return tracked

    monkeypatch.setattr(Repo, '__init__', tracking(Repo.__init__))
    yield
    for ref in made:
        if (repo := ref()) is not None:
            repo.close()


def git_processes(under: Path) -> list[str]:
    """The command lines of this process's ``git cat-file`` children that still run in a directory under ``under``."""
    running: list[str] = []
    for child in psutil.Process().children():
        with contextlib.suppress(psutil.Error):
            line = ' '.join(child.cmdline())
            alive = child.status() != psutil.STATUS_ZOMBIE
            if 'cat-file' in line and alive and Path(child.cwd()).is_relative_to(under.resolve()):
                running.append(line)
    return running


@pytest.fixture
def workers(monkeypatch: pytest.MonkeyPatch) -> weakref.WeakSet[Worker]:
    """Every worker that the test makes."""
    made: weakref.WeakSet[Worker] = weakref.WeakSet()

    def tracking[**P](init: Callable[Concatenate[Worker, P], None]) -> Callable[Concatenate[Worker, P], None]:
        def tracked(self: Worker, *args: P.args, **kwargs: P.kwargs) -> None:
            init(self, *args, **kwargs)
            made.add(self)

        return tracked

    monkeypatch.setattr(Worker, '__init__', tracking(Worker.__init__))
    return made


@pytest.fixture
def db(tmp_path: Path, schema: Path, workers: weakref.WeakSet[Worker]) -> Iterator[Database]:
    database = Database(f'sqlite:///{at_head(schema, tmp_path / "piloop.db")}', actor=ACTOR)
    migrate(
        database.engine, cas=disk(tmp_path), backups=0, daemon=None
    )  # a no-op at head, as when a client opens a database the daemon created
    yield database
    for worker in list(workers):  # a job that the test left running ends before its database closes
        worker.stop()
        worker.join(10)
    database.close()  # and what waits for one gives up


@pytest.fixture
def campaign(db: Database, campaign_path: Path) -> Campaign:
    """Registered campaign with its baseline candidate measured (score=1)."""
    return register(db, campaign_path)


def register(db: Database, campaign_path: Path) -> Campaign:
    cfg = load_campaign_config(campaign_path)
    with Repo(campaign_path) as repo:
        sha, tree_sha = repo.head.commit.hexsha, repo.head.commit.tree.hexsha
    [result] = run_evaluate(
        cfg.evaluate,
        cwd=campaign_path,
        log_dir=campaign_path.parent,
        timeout=30,
        required={m.name for m in cfg.metrics},
        known={m.name for m in cfg.metrics},
        max_evidence_bytes=0,
    )
    assert isinstance(result, Measured), result
    with db.session() as s:
        proj = Campaign(name=cfg.name, path=str(campaign_path), config=cfg)
        s.add(proj)
        s.flush()
        evaluator = current_evaluator(s, proj, cfg)
        base = Candidate(
            campaign_id=proj.id,
            tree_sha=tree_sha,
            revision=sha,
            summary='baseline',
            origin=Origin.BASELINE,
            verdict=Outcome.KEEP,
            verdict_by=System.DAEMON,
        )
        s.add(base)
        s.flush()
        s.add(Measurement(candidate_id=base.id, evaluator_id=evaluator.id, metrics=result.metrics))
        s.commit()
        return proj


def store_config(db: Database, campaign_id: CampaignId, raw: dict[str, object]) -> None:
    """Store ``raw`` as the campaign's config, as an older piloop or a removed package left it: unvalidated."""
    with db.session() as s:
        s.connection().exec_driver_sql('UPDATE campaigns SET config = ? WHERE id = ?', (json.dumps(raw), campaign_id))
        s.commit()


def details(comment: Comment) -> str:
    """The evidence of an escalation that piloop filed: ``details`` of its ``structured`` form."""
    form = comment.form
    assert isinstance(form, dict), form
    found = form['details']
    assert isinstance(found, str), form
    return found


def reconfigured(cfg: CampaignConfig, **keys: object) -> CampaignConfig:
    """Return ``cfg`` with ``keys`` in place of its top-level keys of the same name, validated again.

    ``plugins`` merges into the tables of ``cfg``, key by key, as ``load_campaign_config`` merges an override.
    """
    dumped = cfg.model_dump(mode='json')
    plugins = keys.pop('plugins', {})
    assert isinstance(plugins, Mapping), 'plugins is a table'
    return CampaignConfig.model_validate(dumped | {'plugins': deep_merged(dumped['plugins'], plugins)} | keys)


def deep_merged(base: object, over: Mapping[str, object]) -> dict[str, object]:
    """``over`` on top of ``base``, table by table: a table merges into the table of the same key."""
    out = dict(base) if isinstance(base, dict) else {}
    for key, value in over.items():
        out[key] = deep_merged(out.get(key), value) if isinstance(value, Mapping) else value
    return out


def bare_config(name: str) -> CampaignConfig:
    """Return the smallest valid config: for a campaign row that a test needs, with no repo behind it."""
    metric = {'name': 'score', 'direction': 'higher', 'role': 'primary'}
    return CampaignConfig.model_validate(
        {'name': name, 'evaluate': ['true'], 'metrics': [metric], 'plugins': {'harness': {'pi': {'model': 'm'}}}}
    )


def pinned(repo: Repo, prefix: str) -> list[str]:
    """The commits pinned as ``<prefix><n>``, in ``n`` order."""
    lines = repo.git.for_each_ref('--format=%(refname) %(objectname)', prefix).splitlines()
    refs = [line.split() for line in lines]
    return [sha for _, sha in sorted(refs, key=lambda ref: int(ref[0].removeprefix(prefix)))]


def baseline(db: Database, campaign_id: CampaignId) -> Candidate:
    with db.session() as s:
        return s.scalars(
            select(Candidate).where(Candidate.campaign_id == campaign_id).order_by(Candidate.id).limit(1)
        ).one()


def console_lines(err: str) -> list[str]:
    """The tracer's console lines (``piloop.otel``) without the clock: no time of day, and each span's duration as ``Ns``.

    A duration is wall-clock time, so a loaded machine changes it: ``0.0s`` on a quiet one is ``0.1s`` on a busy one.
    """
    return [re.sub(r'^(\S+) \d+\.\ds\b', r'\1 Ns', line.partition(' ')[2]) for line in err.splitlines()]


class ScriptRuntime(SubprocessRuntime):
    """The real subprocess runtime, recording kills so tests can assert on them."""

    def __init__(self) -> None:
        super().__init__('subprocess')
        self.killed: list[Handle] = []

    def kill(self, handle: Handle) -> None:
        self.killed.append(handle)
        super().kill(handle)


@pytest.fixture
def runtime() -> ScriptRuntime:
    return ScriptRuntime()


class Gate:
    """Holds a wrapped callable at its entry until the test releases it: a deterministic race window."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()

    def wrap[**P, R](self, fn: Callable[P, R]) -> Callable[P, R]:
        def paused(*args: P.args, **kwargs: P.kwargs) -> R:
            self.entered.set()
            self.release.wait(10)
            return fn(*args, **kwargs)

        return paused


HOLD_LOCK = Path(__file__).parent / 'fixtures' / 'hold_lock.py'
"""A stand-in daemon: holds the pid file's lock with its pid inside, like ``piloop.daemon.serve.acquire`` does."""


@pytest.fixture
def lock_holder(tmp_path: Path) -> Iterator[subprocess.Popen[str]]:
    yield from hold_lock(Settings(home=tmp_path).pid_file)


def hold_lock(pid_file: Path, *args: str) -> Iterator[subprocess.Popen[str]]:
    """Run ``HOLD_LOCK`` on ``pid_file`` until the test ends; yields once the lock is held."""
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    with subprocess.Popen(
        [sys.executable, str(HOLD_LOCK), str(pid_file), *args], stdout=subprocess.PIPE, text=True
    ) as proc:
        assert proc.stdout is not None and proc.stdout.readline() == 'held\n'
        yield proc
        proc.kill()


WRITER = (
    'import sqlite3, sys; con = sqlite3.connect(sys.argv[1], isolation_level=None);'
    " con.execute('PRAGMA journal_mode=WAL'); con.execute('BEGIN IMMEDIATE'); print('held', flush=True); sys.stdin.read()"
)
"""A client that opens a write transaction and holds it until its stdin closes."""


@pytest.fixture
def writer(db: Database, tmp_path: Path) -> Iterator[subprocess.Popen[str]]:
    """Another process holding the write lock of the ``db`` fixture's database until the test ends."""
    argv = [sys.executable, '-c', WRITER, str(tmp_path / 'piloop.db')]
    with subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True) as proc:
        assert proc.stdout is not None and proc.stdout.readline() == 'held\n'
        yield proc
        proc.kill()


type Dial = Callable[[str, str], httpx.Client]


@pytest.fixture
def dial(monkeypatch: pytest.MonkeyPatch) -> Dial:
    """``https``'s client of a server with a secret, which it reads from ``TOKEN_ENV`` as on a worker's machine."""

    def make(server: str, secret: str) -> httpx.Client:
        monkeypatch.setenv(TOKEN_ENV, secret)
        return Https().client(server)

    return make


def role_timeout(db: Database, agent_session_id: int) -> float:
    """The role timeout of the agent: what the daemon launches it with while its campaign has no stop time."""
    with db.session() as s:
        agent_session = s.get_one(AgentSession, agent_session_id)
        return s.get_one(Campaign, agent_session.campaign_id).config.role(agent_session.kind).timeout


def wait_until(predicate: Callable[[], object], what: str = 'the condition', *, timeout: float = 30) -> None:
    """Poll ``predicate`` until it is true; fail the test, naming ``what`` it waited for, once ``timeout`` passes.

    The timeout only bounds a test that would fail anyway, so it is generous: the suite runs in parallel, often on a
    loaded host, where a step that takes a second alone can take ten.
    """
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, f'timed out waiting for {what}'
        time.sleep(0.05)


def has_lines(text: str, *lines: str) -> None:
    """Fail unless each of ``lines`` is a whole line of ``text``, in this order, with any lines between them.

    For a rendered screen or report: a test names the facts it is about, so a new line or section breaks no test.
    """
    rest = text.splitlines()
    for line in lines:
        assert line in rest, f'{line!r} is not a line of the text, after the lines before it:\n{text}'
        rest = rest[rest.index(line) + 1 :]


def wait_for_agent_session_handle(db: Database) -> dict[str, object]:
    """Poll until some implementer is ``running`` with a published handle; returns the handle.

    As generous as ``wait_until``: with a daemon process, this waits for its start too. That takes about two seconds
    alone, and more than ten on a loaded host.
    """
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        with db.session() as s:
            agent_session = s.scalars(
                select(AgentSession).where(AgentSession.state == AgentSessionState.RUNNING)
            ).first()
            if agent_session is not None and agent_session.handle:
                return agent_session.handle
        time.sleep(0.05)
    raise AssertionError('implementer never reached running')


@pytest.fixture
def spans() -> InMemorySpanExporter:
    """Every span the ``tracer`` fixture finishes."""
    return InMemorySpanExporter()


@pytest.fixture
def tracer(spans: InMemorySpanExporter) -> Tracer:
    return Tracer(None, spans)


def events(spans: InMemorySpanExporter, name: str) -> list[dict[str, object]]:
    """Attributes of every span event called ``name``, across all finished spans."""
    return [dict(e.attributes or {}) for span in spans.get_finished_spans() for e in span.events if e.name == name]


def span_named(spans: InMemorySpanExporter, name: str) -> ReadableSpan:
    [span] = [s for s in spans.get_finished_spans() if s.name == name]
    return span


def timed(timing: Timing) -> Workers:
    """``[plugins.executor]`` for ``workers`` with the lease timing ``timing``."""
    return Workers(renew_seconds=timing.renew, grace_seconds=timing.grace, toleration_seconds=timing.toleration)


def still() -> float:
    """A worker's clock that stands still (``Worker.clock``): no silence reaches its self-stop.

    For a test that is not about the self-stop: a loaded host can hold one heartbeat past ``Timing.self_stop``, and the
    worker would then stop its jobs.
    """
    return 0.0


def serving(controller: RunController) -> RunController:
    """``controller`` with its executor's worker started, as the daemon's ``reap`` leaves it: its slots count."""
    controller.executor.adopt([])
    return controller


def workers_of(controller: RunController) -> WorkersExecutor:
    """The ``workers`` executor of ``controller``: its worker and its leases."""
    assert isinstance(controller.executor, WorkersExecutor)
    return controller.executor


def disk(home: Path) -> Cas:
    """The default CAS of the home ``home``: ``disk``, under ``<home>/store``."""
    return CAS.selected({}).open(home)


def home_cas(db: Database) -> Cas:
    """The default CAS of the home of ``db``, the directory of its file."""
    return disk(Path(db.engine.url.database or '').parent)


def cas_of(s: Session) -> Cas:
    """The default CAS of the home of the database that ``s`` reads."""
    return disk(Path(s.get_bind().engine.url.database or '').parent)


def factory(rt: Runtime) -> RuntimeFactory:
    def make(_kind: str) -> Runtime:
        return rt

    return make


@pytest.fixture(autouse=True)
def plenty_of_disk(monkeypatch: pytest.MonkeyPatch) -> None:
    """The archiver keeps ``[storage] min_free_gb`` free: a test must not depend on the free space of its machine."""
    monkeypatch.setattr(archive_module, 'free', lambda home: 1 << 50)
    monkeypatch.setattr(tree_module, 'free', lambda home: 1 << 50)


@pytest.fixture(autouse=True)
def git_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in {
        'GIT_AUTHOR_NAME': 'piloop-test',
        'GIT_AUTHOR_EMAIL': 'test@piloop',
        'GIT_COMMITTER_NAME': 'piloop-test',
        'GIT_COMMITTER_EMAIL': 'test@piloop',
    }.items():
        monkeypatch.setenv(key, value)


@pytest.fixture
def no_git_identity(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A machine with no git identity, as a fresh worker container: no ``user.*`` anywhere, and git guesses none.

    Request it after the fixtures that commit, such as ``campaign_path``: they commit as ``git_identity``.
    """
    home = tmp_path / 'empty-home'
    home.mkdir()
    for key in ('GIT_AUTHOR_NAME', 'GIT_AUTHOR_EMAIL', 'GIT_COMMITTER_NAME', 'GIT_COMMITTER_EMAIL', 'EMAIL'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(home))
    monkeypatch.setenv('GIT_CONFIG_NOSYSTEM', '1')
    # else git makes one up from the user's passwd entry and the host name, where they have a domain
    for key, value in {
        'GIT_CONFIG_COUNT': '1',
        'GIT_CONFIG_KEY_0': 'user.useConfigOnly',
        'GIT_CONFIG_VALUE_0': 'true',
    }.items():
        monkeypatch.setenv(key, value)


def role_of[R: RoleConfig](cfg: CampaignConfig, kind: type[R]) -> R:
    """The role of ``cfg`` whose class is ``kind``: a built-in role, by its class."""
    [found] = [role for role in cfg.roles.all().values() if type(role) is kind]
    assert isinstance(found, kind)
    return found


@contextmanager
def serving_api(
    cfg: Settings, listen: str | None = None, network_config: NetworkConfig | None = None
) -> Generator[list[Server]]:
    """Serve the API of ``cfg``'s home, as the daemon does: on its socket, and on the network's ``listen``.

    The network is ``https`` on ``listen``, unless ``network_config`` is another.
    """
    db = Database(cfg.database_url, actor=ACTOR)
    tokens = Tokens(db)
    config = network_config or Https.model_validate({'listen': listen})
    network = config.open(NetworkHost(tokens, HomeSecrets(db, LocalSecrets())))
    listener = network.listener()
    listeners = [local_listener(cfg.socket_file), *([] if listener is None else [listener])]
    api = Handlers(db, cfg, daemon_state(iso(utcnow()), None if listener is None else url(listener)), disk(cfg.home))
    routers = collect(contributions_of(installed()), ()).routers
    servers = [Server(with_api(app(tokens, network, routers), api), each) for each in listeners]
    for server in servers:
        server.start()
    try:
        yield servers
    finally:
        for server in servers:
            server.close()
        db.close()


@pytest.fixture
def daemon_api(tmp_path: Path) -> Iterator[None]:
    """Serve the API of the test's home, as the daemon does: a worker's ``piloop run end`` reaches it."""
    with serving_api(Settings(home=tmp_path)):
        yield


@pytest.fixture
def submitting(daemon_api: None, campaign_path: Path) -> None:
    """The campaign's worker adds a line, commits it and ends its run as submitted, through the served API."""
    write_script(campaign_path.parent / 'harness.sh', HARNESS_APPEND + END_RUN)


@contextmanager
def measuring(cfg: Settings) -> Generator[None]:
    """Measure the baseline of each campaign that registers in ``cfg``'s home, as the daemon's next pass does.

    That is ``RunController.remeasure``, on an in-process worker: ``ledger.due`` puts an unmeasured head first. The
    worker starts with the first registration, so a test that registers nothing finds no worker and no event of one.
    """
    db = Database(cfg.database_url, actor=ACTOR)
    controllers: list[RunController] = []
    done = threading.Event()

    def follow() -> None:
        cursor = 0
        while not done.wait(0.01):
            with db.session() as s:
                added = in_order(s, CampaignAdded, Event.id > cursor)
            for event, payload in added:
                if not controllers:
                    controllers.append(
                        serving(RunController(db, Tracer(None), cfg.home, make_runtime, executor=Workers()))
                    )
                cursor = event.id
                controllers[0].remeasure(payload.baseline)

    follower = threading.Thread(target=follow, name='measuring')
    follower.start()
    try:
        yield
    finally:
        done.set()
        follower.join()
        for controller in controllers:
            controller.stop(5)
        db.close()


def launched(home: Path, signature: str) -> dict[str, object]:
    """The settings of an agent that the daemon launched as ``signature`` in ``home``'s first campaign: its session is
    running, and it has its token, as its worker's start leaves it. ``Settings.model_copy(update=...)`` takes them."""
    agent = TypeAdapter(SignedAgent).validate_python(signature)
    db = Database(f'sqlite:///{home / "piloop.db"}', actor=System.DAEMON)
    with db.session() as s:
        if s.get(AgentSession, agent.session) is None:
            campaign = s.scalars(select(Campaign.id).order_by(Campaign.id)).first()
            s.add(AgentSession(id=agent.session, kind=agent.role, campaign_id=campaign, reason='test'))
        s.get_one(AgentSession, agent.session).state = AgentSessionState.RUNNING
        token = mint_agent(s, agent)
        s.commit()
    db.close()
    return {'actor': agent, 'agent_token': token}


def trace(db: Database) -> list[str]:
    """The feed as ``<kind> <target kind>`` lines, in order: what the daemon did, without ids, actors or times."""
    with db.session() as s:
        return [
            f'{kind} {target}' for kind, target in s.execute(select(Event.kind, Event.target_kind).order_by(Event.id))
        ]


SETTLE = 120.0
"""How long a test waits for the daemon's threads in flight (``Scheduler.wait``): generous for a loaded host, since the
wait returns as soon as nothing is in flight."""


def campaigns_of(scheduler: Scheduler) -> Campaigns:
    """The campaigns' pass of ``scheduler``: the campaign type capability's machinery."""
    return next(each for each in scheduler.contributions.lifecycles if isinstance(each, Campaigns))


def claim(db: Database, run_id: RunId, idea_id: IdeaId) -> None:
    """Claim ``idea_id`` for the open run ``run_id``, as its latest worker does with ``piloop idea claim``."""
    with db.session() as s:
        worker = latest_agent_session(s, run_id)
        assert worker is not None, f'run#{run_id} has a worker'
        backlog_ops.claim(s, idea_id, actor=Agent(role=worker.kind, session=worker.id))
        s.commit()


def end(db: Database, run_id: RunId, how: RunEnding = RunEnding.SUBMITTED, reason: str | None = None) -> None:
    """End the running run ``run_id`` as ``how``, as its latest worker does with ``piloop run end``."""
    with db.session() as s:
        worker = latest_agent_session(s, run_id)
        assert worker is not None, f'run#{run_id} has a worker'
        end_run(s, how, reason, actor=Agent(role=worker.kind, session=worker.id))
        s.commit()


def queued(db: Database, scheduler: Scheduler, campaign_id: CampaignId, *ideas: IdeaId) -> list[RunId]:
    """Queue a fresh run on each free slot of the campaign (``Planner.queue``), and claim ``ideas`` for them in order.

    Its worker would claim once it runs; claimed before the launch, the run cannot end before its claim.
    """
    with db.session() as s:
        campaign = s.get_one(Campaign, campaign_id)
        before = s.scalar(select(func.max(Run.id))) or 0
        campaigns_of(scheduler)._planner(campaign).queue(s, campaign, left(s, campaign, campaign.config))
        s.commit()
        runs = list(s.scalars(select(Run.id).where(Run.id > before).order_by(Run.id)))
    for run_id, idea_id in zip(runs, ideas, strict=False):
        claim(db, run_id, idea_id)
    return runs


@pytest.fixture
def no_fresh_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start no run on a free slot, for a test of sweeps that adds each run it needs by hand.

    The scheduler fills every free slot of a role that does runs with a fresh run (``OptimizationPlanner.queue``), and
    each of those would run, fail and wake the triggers under test.
    """
    monkeypatch.setattr(OptimizationPlanner, 'queue', lambda _planner, _s, _campaign, _left: None)


def run_all(scheduler: Scheduler, now: datetime | None = None) -> list[AgentSessionId]:
    """Run every consumer once, as the loop does when it starts, and return what the campaigns' pass launched."""
    scheduler.run_all(now)
    return campaigns_of(scheduler).launched


def active(scheduler: Scheduler) -> list[AgentSessionId]:
    """The agents whose threads run now."""
    return sorted(AgentSessionId(int(name.partition('#')[2])) for name in campaigns_of(scheduler).in_flight())


class EveryClient(
    BacklogClient, CandidatesClient, VerdictsClient, OptimizationClient, EvaluationClient, RunsClient, LineageClient
):
    """A client of every route: core's, those of optimization and its components, and the lineage's."""
