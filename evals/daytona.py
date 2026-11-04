"""The Daytona half of a run: a piloop arm's control sandbox, and the configuration of the team that it starts.

A piloop arm runs as a **team**:

- one control sandbox on Daytona runs ``piloop serve --listen`` and ``piloop cas serve``, holds the repo of record, and
  takes no job;
- its daemon's provisioners start the rest, all at once (``min = max``): the agents' workers in the pool ``agents``,
  Modal sandboxes that run one agent each, and the evaluators in the pool ``eval``, one evaluation at a time each, on
  Daytona for a task scored there (``Task.scorer``), else on Modal. The evaluators' snapshot or image holds the
  campaign's sealed split; no agent's does.

Workers and evaluators dial the control sandbox through Daytona's preview proxy, which ends TLS. A Daytona sandbox's
memory is a hard limit, and so is that of a sandbox on Modal here. This module goes with ``evals/``.
"""

import contextlib
import hashlib
import json
import math
import shlex
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

import tomli_w
from daytona import (
    CreateSandboxFromSnapshotParams,
    CreateSnapshotParams,
    Daytona,
    DaytonaConfig,
    DaytonaConflictError,
    DaytonaNotFoundError,
    Image,
    Resources as Size,
    Sandbox,
    SandboxState,
    SessionExecuteRequest,
)
from daytona_api_client.models.snapshot_state import SnapshotState
from pydantic import JsonValue

from evals.model import Resources, Settings, Task
from evals.sandbox import Copy, Env, Pip, Run, Step, bounded, recipe

PYTHON = '3.13'
"""The Python of Daytona's base image: its newest. The repos run on the recipe's own, through uv."""
CONTROL = Resources(cpu=2, memory_mb=4096)
"""The control sandbox: the daemon, the CAS and the repo of record."""
EVALUATOR = Resources(cpu=1, memory_mb=8192)
"""An evaluator, or a scorer: one single-threaded codec at a time, up to the brief's 8 GiB, which is this sandbox's hard
limit."""
API, CAS = 8443, 7701
"""The ports of ``piloop serve`` and ``piloop cas serve`` in the control sandbox."""
AGENTS, EVAL = 'agents', 'eval'
"""The pools of the workers and of the evaluators."""
SNAPSHOT_SECONDS = 3600
"""How long a snapshot may build."""


def home_config(
    api: str, workers: int, agents: dict[str, JsonValue], evaluators: int, evaluate: dict[str, JsonValue]
) -> str:
    """The control sandbox's ``config.toml``: its network, a daemon that takes no job, and the team's pools.

    The network is behind the preview proxy. The pools' provisioners start the whole team at once: ``workers`` on the
    ``compute`` table ``agents``, and ``evaluators`` on ``evaluate``.
    """

    def fixed(n: int, compute: dict[str, JsonValue]) -> dict[str, JsonValue]:
        return {'min': n, 'max': n, 'max_starting': n, 'compute': compute}

    pools: dict[str, JsonValue] = {
        'serve': {'agents': 0, 'slots': {'evaluation': 0}},
        AGENTS: {'agents': 1, 'slots': {'evaluation': 0}, 'provisioner': fixed(workers, agents)},
        EVAL: {
            'agents': 0,
            'slots': {'evaluation': 1},
            'runners': ['process'],
            'provisioner': fixed(evaluators, evaluate),
        },
    }
    network = {'provider': 'https', 'listen': f'0.0.0.0:{API}', 'tls': 'proxy', 'url': api}
    return tomli_w.dumps({'plugins': {'network': network, 'executor': {'provider': 'workers', 'pools': pools}}})


def worker_plugins(cas: str) -> str:
    """A worker's or an evaluator's ``PILOOP_PLUGINS``: the control sandbox's CAS, cached on its own disk."""
    return json.dumps({'cas': {'provider': 'cached', 'local_max_gb': 5, 'remote': {'provider': 'http', 'url': cas}}})


def evaluators(workers: int, settings: Settings) -> int:
    """The evaluators of a team of ``workers``: ``evaluators_per_worker`` each, at least one."""
    return max(1, math.ceil(workers * settings.evaluators_per_worker))


def rate(r: Resources, settings: Settings) -> float:
    """USD per hour of a sandbox of ``r``, at Daytona's list price."""
    return r.cpu * settings.daytona_usd_per_vcpu_hour + r.memory_mb / 1024 * settings.daytona_usd_per_gib_hour


def image(steps: Sequence[Step]) -> Image:
    """``sandbox.recipe`` as a Daytona image."""
    img = Image.debian_slim(PYTHON)
    for step in steps:
        match step:
            case Run(lines):
                img = img.run_commands(*lines)
            case Env(values):
                img = img.env(values)
            case Pip(packages):
                img = img.pip_install(*packages)
            case Copy(local, remote):
                img = img.add_local_dir(local, remote) if local.is_dir() else img.add_local_file(local, remote)
    return img


def fingerprint(steps: Sequence[Step]) -> str:
    """A name for the snapshot of ``steps``: it changes with any step or any copied file."""
    digest = hashlib.sha256()
    for step in steps:
        digest.update(repr(step).encode())
        if isinstance(step, Copy):
            files = sorted(step.local.rglob('*')) if step.local.is_dir() else [step.local]
            for f in (f for f in files if f.is_file() and '__pycache__' not in f.parts):
                digest.update(str(f.relative_to(step.local.parent)).encode() + f.read_bytes())
    return f'evals-{digest.hexdigest()[:16]}'


class Started:
    """A command that a ``DaytonaBox`` started in the background, as ``Plain`` reads a Modal process."""

    def __init__(self, sandbox: Sandbox, session: str, command: str) -> None:
        self._sandbox, self._session, self._command = sandbox, session, command

    def poll(self) -> int | None:
        """Its exit code, or None while it runs."""
        return bounded(
            None, lambda: self._sandbox.process.get_session_command(self._session, self._command).exit_code, 'poll'
        )

    @property
    def stderr(self) -> Logs:
        return Logs(self)

    def logs(self) -> str:
        logs = bounded(
            None, lambda: self._sandbox.process.get_session_command_logs(self._session, self._command), 'logs'
        )
        return logs.stderr or ''


@dataclass
class Logs:
    """What ``Plain`` reads of a process's error output: ``stderr.read()``."""

    started: Started

    def read(self) -> str:
        return self.started.logs()


@dataclass
class DaytonaBox:
    """A running Daytona sandbox: ``sandbox.Shell``."""

    client: Daytona
    sandbox: Sandbox
    size: Resources

    @property
    def id(self) -> str:
        return self.sandbox.id

    def preview(self, port: int) -> str:
        """The public URL of ``port``, through Daytona's preview proxy, which ends TLS."""
        return bounded(None, lambda: self.sandbox.get_preview_link(port).url, 'DaytonaBox.preview')

    def run(self, script: str, timeout: float | None = None) -> str:
        """Run ``script`` in bash and return its stdout. Raises ``RuntimeError`` with its stderr when it fails."""
        return bounded(timeout, lambda: self._run(script, timeout), 'DaytonaBox.run')

    def _run(self, script: str, timeout: float | None) -> str:
        err = f'/tmp/.evals-{uuid.uuid4().hex}.err'
        wrapped = f'bash -c {shlex.quote(script)} 2>{err}'
        done = self.sandbox.process.exec(wrapped, timeout=None if timeout is None else int(timeout))
        if done.exit_code != 0:
            stderr = self.sandbox.process.exec(f'cat {err}').result or ''
            raise RuntimeError(f'{script[:200]!r} exited {done.exit_code}: {stderr[-3000:]}')
        return done.result or ''

    def start(self, script: str) -> Started:
        """Start ``script`` in bash, in a session of its own, in the background."""
        return bounded(None, lambda: self._start(script), 'DaytonaBox.start')

    def _start(self, script: str) -> Started:
        session = uuid.uuid4().hex
        self.sandbox.process.create_session(session)
        command = f'bash -c {shlex.quote(script)}'
        started = self.sandbox.process.execute_session_command(
            session, SessionExecuteRequest(command=command, run_async=True)
        )
        return Started(self.sandbox, session, started.cmd_id)

    def write(self, path: str, data: bytes) -> None:
        """Write ``data`` to ``path`` in the sandbox."""
        self.run(f'mkdir -p {shlex.quote(path.rsplit("/", 1)[0])}')
        bounded(None, lambda: self.sandbox.fs.upload_file(data, path), 'DaytonaBox.write')

    def read(self, path: str) -> bytes:
        """The bytes of ``path`` in the sandbox."""
        return bounded(None, lambda: self.sandbox.fs.download_file(path), 'DaytonaBox.read')

    def ended(self) -> bool:
        """Whether the sandbox no longer runs."""
        bounded(None, self.sandbox.refresh_data, 'DaytonaBox.ended')
        return self.sandbox.state not in (SandboxState.STARTED, SandboxState.STARTING)

    def close(self) -> None:
        """Delete the sandbox."""
        bounded(None, lambda: self.client.delete(self.sandbox), 'DaytonaBox.close')


def client(settings: Settings) -> Daytona:
    """Daytona's client, with ``daytona_api_key``."""
    if not settings.daytona_api_key.get_secret_value():
        raise ValueError('set EVALS_DAYTONA_API_KEY: the runs need Daytona')
    return Daytona(DaytonaConfig(api_key=settings.daytona_api_key.get_secret_value()))


def snapshot(daytona: Daytona, steps: Sequence[Step], size: Resources) -> str:
    """The name of the snapshot of ``steps`` whose sandboxes are of ``size``, once it is ready.

    The first caller builds it. One that finds it building, another thread's or another driver's, waits for it: a
    sandbox from a snapshot that still builds is refused.

    Raises:
        RuntimeError: Its build failed.
        TimeoutError: It is not ready after ``SNAPSHOT_SECONDS``.
    """
    name = f'{fingerprint(steps)}-{size.cpu:g}c{size.memory_mb // 1024}g'
    try:
        found = bounded(None, lambda: daytona.snapshot.get(name), 'snapshot.get')
    except DaytonaNotFoundError:
        sized = Size(cpu=int(size.cpu), memory=size.memory_mb // 1024, disk=10)
        with contextlib.suppress(DaytonaConflictError):  # another caller started its build first: wait for it below
            daytona.snapshot.create(
                CreateSnapshotParams(name=name, image=image(steps), resources=sized), timeout=SNAPSHOT_SECONDS
            )
        found = bounded(None, lambda: daytona.snapshot.get(name), 'snapshot.get')
    deadline = time.monotonic() + SNAPSHOT_SECONDS
    while found.state != SnapshotState.ACTIVE:
        if found.state in (SnapshotState.ERROR, SnapshotState.BUILD_FAILED):
            raise RuntimeError(f'snapshot {name} is {found.state}: {found.error_reason}')
        if time.monotonic() > deadline:
            raise TimeoutError(f'snapshot {name} is still {found.state} after {SNAPSHOT_SECONDS} s')
        time.sleep(10)
        found = bounded(None, lambda: daytona.snapshot.get(name), 'snapshot.get')
    return name


def params(
    name: str, env: dict[str, str], *, public: bool, labels: dict[str, str], offline: bool, ttl_minutes: int
) -> CreateSandboxFromSnapshotParams:
    """How to create a sandbox from the snapshot ``name``: deleted when it stops, and ended after ``ttl_minutes``."""
    return CreateSandboxFromSnapshotParams(
        snapshot=name,
        env_vars=env,
        public=public,
        labels=labels,
        network_block_all=offline or None,
        auto_stop_interval=0,
        auto_delete_interval=0,
        ttl_minutes=ttl_minutes,
    )


def box(
    daytona: Daytona,
    name: str,
    size: Resources,
    env: dict[str, str],
    *,
    public: bool,
    labels: dict[str, str],
    ttl_minutes: int,
    offline: bool = False,
) -> DaytonaBox:
    """A sandbox of ``size`` from the snapshot ``name``, deleted when it stops.

    ``offline``: it reaches nothing. ``ttl_minutes``: Daytona ends it then, so a sandbox whose process died, or that
    was kept for inspection after a failed export, costs a bounded amount.
    """
    made = params(name, env, public=public, labels=labels, offline=offline, ttl_minutes=ttl_minutes)
    return DaytonaBox(daytona, daytona.create(made, timeout=600), size)


def control(
    daytona: Daytona, steps: Sequence[Step], env: dict[str, str], labels: dict[str, str], hours: float
) -> DaytonaBox:
    """The control sandbox of ``steps``: public, so that the team reaches its preview URLs.

    It ends two hours after the run's ``hours`` at the latest.
    """
    name = snapshot(daytona, steps, CONTROL)
    return box(daytona, name, CONTROL, env, public=True, labels=labels, ttl_minutes=int((hours + 2) * 60))


def scorer(daytona: Daytona, task: Task, labels: dict[str, str], minutes: float) -> DaytonaBox:
    """A sandbox that scores one version of ``task`` within ``minutes``: the size of an evaluator, without network.

    It ends an hour after that at the latest, for its uploads and reads.
    """
    name = snapshot(daytona, recipe([task], None), EVALUATOR)
    ttl = math.ceil(minutes) + 60
    return box(daytona, name, EVALUATOR, {}, public=False, labels=labels, offline=True, ttl_minutes=ttl)
