"""The Modal sandboxes: the image every run and every scoring uses, and running commands in one."""

import shlex
from dataclasses import dataclass
from typing import Literal, Protocol
from urllib.parse import urlsplit

import modal

from evals.model import ROOT, TASKS, PiloopArm, PlainArm, Settings, Task

PYTHON = '3.14'
DEPS = ['numpy==2.3.4', 'scipy==1.16.3', 'uv==0.11.23']
"""What every task repo may import, also in the scorer, which runs without network."""
AGENTS = ['@openai/codex@0.166.0', '@anthropic-ai/claude-code@2.1.310']
NODE = '22'
OPT = '/opt/evals'
WORK = '/work'
PATH = '/root/.local/bin:/usr/local/bin:/usr/bin:/bin'
PILOOP_HOME = '/root/.piloop-evals'
"""A fresh home in the sandbox, so a piloop there never meets the default one."""
WRAP = f'{OPT}/wrap'
"""Where ``wrap/sitecustomize.py`` goes, on ``PYTHONPATH``: it logs each run of a task's evaluator."""
EVALUATIONS = 'evaluations.jsonl'
"""The log of ``wrap/sitecustomize.py`` in a run's export: each run of the task's evaluator."""


def image(tasks: list[Task], arm: PiloopArm | PlainArm | None) -> modal.Image:
    """The tasks' data, the dependencies of their repos, both agents, and the arm's piloop, if any."""
    img = (
        modal.Image.debian_slim(python_version=PYTHON)
        .apt_install('git', 'curl', 'ca-certificates', 'procps', 'sqlite3', 'xz-utils')
        .run_commands(
            f'curl -fsSL https://deb.nodesource.com/setup_{NODE}.x | bash -',
            'apt-get install -y nodejs',
            f'npm install -g {" ".join(AGENTS)}',
            'git config --global user.name evals && git config --global user.email evals@localhost',
            'git config --global init.defaultBranch main',
        )
        .pip_install(*DEPS)
        .env(
            {
                'PATH': PATH,
                'IS_SANDBOX': '1',
                'UV_PYTHON': PYTHON,
                'UV_PYTHON_PREFERENCE': 'only-managed',
                'PYTHONPATH': WRAP,
            }
        )
        .run_commands(f'uv python install {PYTHON}')
    )
    for task in tasks:
        if task.setup:
            img = img.run_commands(*task.setup)
        if (TASKS / task.name / 'build').is_dir():
            img = img.add_local_dir(TASKS / task.name / 'build', f'{OPT}/build/{task.name}', copy=True)
        if task.data:
            here = f'{OPT}/data/{task.name}'
            img = img.run_commands(f'mkdir -p {here}', *(f'cd {here} && {line}' for line in task.data))
    if isinstance(arm, PiloopArm):
        img = img.run_commands(f'uv tool install {shlex.quote(arm.piloop)}')
        img = img.env({'PILOOP_HOME': PILOOP_HOME})
    img = img.add_local_dir(TASKS, f'{OPT}/tasks')
    img = img.add_local_file(ROOT / 'campaign_sealed.py', f'{OPT}/campaign_sealed.py')
    img = img.add_local_file(ROOT / 'gpu-lock', f'{OPT}/gpu-lock')
    img = img.add_local_dir(ROOT / 'wrap', WRAP)
    return img.add_local_file(ROOT / 'meter.py', f'{OPT}/meter.py').add_local_file(
        ROOT / 'scorer.py', f'{OPT}/scorer.py'
    )


Network = Literal['none', 'gateway', 'open']
"""What a sandbox reaches: nothing (a scorer), the model gateway alone, or anything."""


@dataclass(frozen=True)
class Egress:
    """The ``modal.Sandbox.create`` keys of a ``Network``."""

    block_network: bool
    outbound_domain_allowlist: list[str] | None = None
    """The only hosts it reaches. None: any."""


def network_policy(settings: Settings, network: Network) -> Egress:
    """The egress of ``network``.

    Raises:
        ValueError: a gateway-only sandbox, without a gateway host to reach.
    """
    match network:
        case 'none':
            return Egress(block_network=True)
        case 'open':
            return Egress(block_network=False)
        case 'gateway':
            host = urlsplit(settings.gateway).hostname
            if not host:
                raise ValueError(f'EVALS_GATEWAY {settings.gateway!r} names no host for a gateway-only sandbox')
            return Egress(block_network=False, outbound_domain_allowlist=[host])


def memory(request_mb: int, limit_mb: int | None) -> int | tuple[int, int]:
    """The ``memory`` of a sandbox that asks for ``request_mb``, of a task with ``limit_mb``: reserved and capped."""
    if limit_mb is None:
        return request_mb
    held = max(request_mb, limit_mb)
    return (held, held)


class Shell(Protocol):
    """What the driver does in a sandbox. ``Box`` is one; a test gives a fake."""

    def run(self, script: str, timeout: float | None = None) -> str: ...

    def start(self, script: str) -> object: ...

    def read(self, path: str) -> bytes: ...

    def write(self, path: str, data: bytes) -> None: ...

    def ended(self) -> bool: ...


@dataclass
class Box:
    """A running sandbox."""

    sandbox: modal.Sandbox

    @classmethod
    def create(
        cls,
        settings: Settings,
        img: modal.Image,
        *,
        cpu: float,
        memory_mb: int,
        hours: float,
        network: Network,
        memory_limit_mb: int | None = None,
        gpu: str | None = None,
        cloud: str | None = None,
        env: dict[str, str] | None = None,
        **tags: str,
    ) -> Box:
        """Start a sandbox that lives at most ``hours``, reaching what ``network`` names.

        One that reaches anything has ``GATEWAY_TOKEN`` and ``env`` in its environment, as a secret: never in a command
        line. ``memory_limit_mb`` is the task's (``memory``).
        """
        app = modal.App.lookup(settings.modal_app, create_if_missing=True, environment_name=settings.modal_environment)
        own = {'GATEWAY_TOKEN': settings.gateway_token.get_secret_value(), **(env or {})}
        secrets = [modal.Secret.from_dict(dict(own))] if network != 'none' else []
        egress = network_policy(settings, network)
        sandbox = modal.Sandbox.create(
            'sleep',
            'infinity',
            app=app,
            image=img,
            secrets=secrets,
            cpu=cpu,
            memory=memory(memory_mb, memory_limit_mb),
            gpu=gpu,
            cloud=cloud,
            timeout=int(hours * 3600),
            block_network=egress.block_network,
            outbound_domain_allowlist=egress.outbound_domain_allowlist,
            workdir=WORK,
        )
        sandbox.set_tags({'purpose': 'piloop-evals', **tags})
        return cls(sandbox)

    def run(self, script: str, timeout: float | None = None) -> str:
        """Run ``script`` in bash and return its stdout. Raises ``RuntimeError`` with its stderr when it fails."""
        proc = self.sandbox.exec('bash', '-c', script, timeout=None if timeout is None else int(timeout))
        out, err = proc.stdout.read(), proc.stderr.read()
        if proc.wait() != 0:
            raise RuntimeError(f'{script[:200]!r} exited {proc.returncode}: {err[-3000:]}')
        return out

    def start(self, script: str) -> modal.container_process.ContainerProcess[str]:
        """Start ``script`` in bash, in the background: its output goes where it redirects it."""
        return self.sandbox.exec('bash', '-c', script)

    def write(self, path: str, data: bytes) -> None:
        """Write ``data`` to ``path`` in the sandbox."""
        self.run(f'mkdir -p {shlex.quote(path.rsplit("/", 1)[0])}')
        self.sandbox.filesystem.write_bytes(data, path)

    def read(self, path: str) -> bytes:
        """The bytes of ``path`` in the sandbox."""
        return self.sandbox.filesystem.read_bytes(path)

    def ended(self) -> bool:
        """Whether the sandbox has ended."""
        return self.sandbox.poll() is not None

    def close(self) -> None:
        """End the sandbox."""
        self.sandbox.terminate()
