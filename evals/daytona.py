"""The Daytona half of a run: a piloop arm's daemon and its evaluators, while its agents' workers run on Modal.

A piloop arm runs as a **team**:

- one control sandbox on Daytona runs ``piloop serve --listen`` and ``piloop cas serve``, holds the repo of record and
  the campaign's sealed split, and takes no job;
- each evaluator is a Daytona sandbox that runs ``piloop worker`` in the pool ``eval``, one evaluation at a time, so no
  agent's load reaches a timing that decides a verdict;
- each worker is a Modal sandbox (``drive.AGENT``) that runs ``piloop worker`` in the pool ``agents``, one agent at a
  time: its builds and dev runs only.

Workers and evaluators dial the control sandbox through Daytona's preview proxy, which ends TLS. A Daytona sandbox's
memory is a hard limit, and so is that of a worker on Modal.

Retirement: the Modal path of the runs (``sandbox.Box`` in ``drive``) goes once a full block runs here. This module goes
with ``evals/``.
"""

import contextlib
import hashlib
import math
import shlex
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

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

from evals.model import PiloopArm, Resources, Settings, Task
from evals.sandbox import PILOOP_HOME, WORK, Copy, Env, Pip, Run, Shell, Step, recipe

PYTHON = '3.13'
"""The Python of Daytona's base image: its newest. The repos run on the recipe's own, through uv."""
CONTROL = Resources(cpu=2, memory_mb=4096)
"""The control sandbox: the daemon, the CAS and the repo of record."""
EVALUATOR = Resources(cpu=1, memory_mb=8192)
"""An evaluator: one single-threaded codec at a time, up to the brief's 8 GiB, which is this sandbox's hard limit."""
API, CAS = 8443, 7701
"""The ports of ``piloop serve`` and ``piloop cas serve`` in the control sandbox."""
AGENTS, EVAL = 'agents', 'eval'
"""The pools of the workers and of the evaluators."""
SNAPSHOT_SECONDS = 3600
"""How long a snapshot may build."""
TOKEN = f'{WORK}/worker.token'
"""Where a worker's sandbox keeps its token: never on a command line."""


def home_config(api: str) -> str:
    """The control sandbox's ``config.toml``: its network behind the preview proxy, and pools that keep jobs off it."""
    return f"""[plugins.network]
provider = "https"
listen = "0.0.0.0:{API}"
tls = "proxy"
url = "{api}"

[plugins.executor]
provider = "workers"

[plugins.executor.pools.serve]
agents = 0
slots = {{ evaluation = 0 }}

[plugins.executor.pools.{AGENTS}]
agents = 1
slots = {{ evaluation = 0 }}

[plugins.executor.pools.{EVAL}]
agents = 0
slots = {{ evaluation = 1 }}
runners = ["process"]
"""


def worker_config(cas: str) -> str:
    """A worker's or an evaluator's ``config.toml``: the control sandbox's CAS, cached on its own disk."""
    return f"""[plugins.cas]
provider = "cached"
local_max_gb = 5
remote = {{ provider = "http", url = "{cas}" }}
"""


@dataclass(frozen=True)
class Plan:
    """The sandboxes of a piloop arm's team."""

    workers: int
    """On Modal."""
    evaluators: int

    @property
    def sizes(self) -> list[Resources]:
        """The team's Daytona sandboxes, which count against its quota: the control one first."""
        return [CONTROL, *[EVALUATOR] * self.evaluators]


def plan(workers: int, settings: Settings) -> Plan:
    """The team of an arm of ``workers``: ``evaluators_per_worker`` evaluators each, at least one."""
    return Plan(workers=workers, evaluators=max(1, math.ceil(workers * settings.evaluators_per_worker)))


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
        return self._sandbox.process.get_session_command(self._session, self._command).exit_code

    @property
    def stderr(self) -> Logs:
        return Logs(self)

    def logs(self) -> str:
        return self._sandbox.process.get_session_command_logs(self._session, self._command).stderr or ''


@dataclass
class Logs:
    """What ``Plain`` reads of a process's error output: ``stderr.read()``."""

    started: Started

    def read(self) -> str:
        return self.started.logs()


class Linked(Shell, Protocol):
    """A sandbox that other sandboxes reach: the control one."""

    def preview(self, port: int) -> str: ...


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
        return self.sandbox.get_preview_link(port).url

    def run(self, script: str, timeout: float | None = None) -> str:
        """Run ``script`` in bash and return its stdout. Raises ``RuntimeError`` with its stderr when it fails."""
        err = f'/tmp/.evals-{uuid.uuid4().hex}.err'
        wrapped = f'bash -c {shlex.quote(script)} 2>{err}'
        done = self.sandbox.process.exec(wrapped, timeout=None if timeout is None else int(timeout))
        if done.exit_code != 0:
            stderr = self.sandbox.process.exec(f'cat {err}').result or ''
            raise RuntimeError(f'{script[:200]!r} exited {done.exit_code}: {stderr[-3000:]}')
        return done.result or ''

    def start(self, script: str) -> Started:
        """Start ``script`` in bash, in a session of its own, in the background."""
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
        self.sandbox.fs.upload_file(data, path)

    def read(self, path: str) -> bytes:
        """The bytes of ``path`` in the sandbox."""
        return self.sandbox.fs.download_file(path)

    def ended(self) -> bool:
        """Whether the sandbox no longer runs."""
        self.sandbox.refresh_data()
        return self.sandbox.state not in (SandboxState.STARTED, SandboxState.STARTING)

    def close(self) -> None:
        """Delete the sandbox."""
        self.client.delete(self.sandbox)


@dataclass
class Sandboxes:
    """A team's Daytona sandboxes: its control one, and its evaluators."""

    control: DaytonaBox
    evaluators: list[DaytonaBox] = field(default_factory=list)

    @property
    def boxes(self) -> list[DaytonaBox]:
        return [self.control, *self.evaluators]

    def close(self) -> None:
        for b in self.boxes:
            b.close()


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
        found = daytona.snapshot.get(name)
    except DaytonaNotFoundError:
        sized = Size(cpu=int(size.cpu), memory=size.memory_mb // 1024, disk=10)
        with contextlib.suppress(DaytonaConflictError):  # another caller started its build first: wait for it below
            daytona.snapshot.create(
                CreateSnapshotParams(name=name, image=image(steps), resources=sized), timeout=SNAPSHOT_SECONDS
            )
        found = daytona.snapshot.get(name)
    deadline = time.monotonic() + SNAPSHOT_SECONDS
    while found.state != SnapshotState.ACTIVE:
        if found.state in (SnapshotState.ERROR, SnapshotState.BUILD_FAILED):
            raise RuntimeError(f'snapshot {name} is {found.state}: {found.error_reason}')
        if time.monotonic() > deadline:
            raise TimeoutError(f'snapshot {name} is still {found.state} after {SNAPSHOT_SECONDS} s')
        time.sleep(10)
        found = daytona.snapshot.get(name)
    return name


def box(
    daytona: Daytona,
    name: str,
    size: Resources,
    env: dict[str, str],
    *,
    public: bool,
    labels: dict[str, str],
    offline: bool = False,
) -> DaytonaBox:
    """A sandbox of ``size`` from the snapshot ``name``, deleted when it stops. ``offline``: it reaches nothing."""
    sandbox = daytona.create(
        CreateSandboxFromSnapshotParams(
            snapshot=name,
            env_vars=env,
            public=public,
            labels=labels,
            network_block_all=offline or None,
            auto_stop_interval=0,
            auto_delete_interval=0,
        ),
        timeout=600,
    )
    return DaytonaBox(daytona, sandbox, size)


def sandboxes(
    task: Task, arm: PiloopArm, workers: int, env: dict[str, str], settings: Settings, run: str
) -> tuple[Sandboxes, Plan]:
    """The Daytona sandboxes of a team of ``workers``: its control sandbox and its evaluators.

    Only the control sandbox gets the gateway's token in its environment, for ``piloop secret set``: a worker gets each
    job's keys from piloop's secrets when the job starts. (A Daytona secret would not do: it is a placeholder that only
    that sandbox's own requests replace, and the agents' requests leave from the workers.)
    """
    daytona = client(settings)
    steps = recipe([task], arm)
    labels = {'purpose': 'piloop-evals', 'run': run.replace('/', '_')}
    team = plan(workers, settings)
    holder = env | {'GATEWAY_TOKEN': settings.gateway_token.get_secret_value()}
    made = Sandboxes(
        control=box(daytona, snapshot(daytona, steps, CONTROL), CONTROL, holder, public=True, labels=labels)
    )
    try:
        name = snapshot(daytona, steps, EVALUATOR)
        made.evaluators = [
            box(daytona, name, EVALUATOR, env, public=False, labels=labels) for _ in range(team.evaluators)
        ]
    except Exception:
        made.close()  # the sandboxes made so far: none outlives a run that never started
        raise
    return made, team


def scorer(daytona: Daytona, task: Task, labels: dict[str, str]) -> DaytonaBox:
    """A sandbox that scores one version of ``task``: the size of an evaluator, without network."""
    name = snapshot(daytona, recipe([task], None), EVALUATOR)
    return box(daytona, name, EVALUATOR, {}, public=False, labels=labels, offline=True)


def configure(control: Linked) -> None:
    """Write the control sandbox's ``config.toml`` before its daemon starts."""
    control.write(f'{PILOOP_HOME}/config.toml', home_config(control.preview(API)).encode())


def enlist(
    control: Linked, agents: Sequence[Shell], evaluators: Sequence[Shell], sealed: bytes | None, sealed_dir: str
) -> None:
    """Serve the CAS, and start each worker and evaluator with a token of its own, once the daemon answers.

    Evaluators get the campaign's sealed split at ``sealed_dir``, where the campaign's sealed command reads it.
    """
    api, cas = control.preview(API), control.preview(CAS)
    control.start(f'piloop cas serve --listen 0.0.0.0:{CAS} >> {WORK}/out/cas.out 2>&1')
    for i, (pool, member) in enumerate([(AGENTS, b) for b in agents] + [(EVAL, b) for b in evaluators]):
        name = f'{pool}{i}'
        token = control.run(f'piloop token create {name} --access worker').strip()
        member.write(TOKEN, token.encode())
        member.write(f'{PILOOP_HOME}/config.toml', worker_config(cas).encode())
        if pool == EVAL and sealed is not None:
            member.write(f'{WORK}/campaign.tgz', sealed)
            member.run(
                f'mkdir -p {sealed_dir} && tar xzf {WORK}/campaign.tgz -C {sealed_dir} && rm {WORK}/campaign.tgz'
            )
        member.start(
            f'mkdir -p {WORK}/out && PILOOP_HTTPS_TOKEN="$(cat {TOKEN})" piloop worker --server {shlex.quote(api)}'
            f' --pool {pool} >> {WORK}/out/worker.out 2>&1'
        )


def described(team: Plan, boxes: Sandboxes) -> dict[str, object]:
    """What ``run.json`` records of the team."""
    return {
        'platform': 'daytona',
        'daytona_sandboxes': [b.id for b in boxes.boxes],
        'workers': team.workers,
        'evaluators': team.evaluators,
    }
