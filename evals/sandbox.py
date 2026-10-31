"""The Modal sandboxes: the image every run and every scoring uses, and running commands in one."""

import shlex
from dataclasses import dataclass

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
        .env({'PATH': PATH, 'IS_SANDBOX': '1', 'UV_PYTHON': PYTHON, 'UV_PYTHON_PREFERENCE': 'only-managed'})
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
    return img.add_local_file(ROOT / 'meter.py', f'{OPT}/meter.py').add_local_file(
        ROOT / 'scorer.py', f'{OPT}/scorer.py'
    )


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
        network: bool,
        gpu: str | None = None,
        env: dict[str, str] | None = None,
        **tags: str,
    ) -> Box:
        """Start a sandbox that lives at most ``hours``. Without ``network``, it reaches nothing outside.

        With ``network``, its environment has ``GATEWAY_TOKEN`` and ``env``, as a secret: never in a command line.
        """
        app = modal.App.lookup(settings.modal_app, create_if_missing=True, environment_name=settings.modal_environment)
        own = {'GATEWAY_TOKEN': settings.gateway_token.get_secret_value(), **(env or {})}
        secrets = [modal.Secret.from_dict(dict(own))] if network else []
        sandbox = modal.Sandbox.create(
            'sleep',
            'infinity',
            app=app,
            image=img,
            secrets=secrets,
            cpu=cpu,
            memory=memory_mb,
            gpu=gpu,
            timeout=int(hours * 3600),
            block_network=not network,
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

    def close(self) -> None:
        """End the sandbox."""
        self.sandbox.terminate()
