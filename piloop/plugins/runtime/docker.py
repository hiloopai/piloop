"""``docker``: each turn of an agent in a container of its own, which sees only the agent's files and allowed hosts.

```toml
[plugins.runtime.docker]
image = "ghcr.io/acme/agent:1"    # with the harness CLIs and git; piloop brings its own CLI
allow = ["api.anthropic.com"]     # what agents may reach, besides their role's HTTP MCP servers
```

A container runs as the worker's user, with an empty home at the path of the worker's home, and these bind mounts at
the same paths as on the machine:

- the agent's worktree, its git directory and its log directory, read-write;
- the targets of the worktree's links (``link``, the workspace): the linked inputs read-only, the rest read-write;
- the harness's session directory (``AgentSpec.shares``), so that the worker restores and reads sessions as before;
- this piloop's install and the agent's ``piloop`` (read-only), and the daemon's or the relay's socket.

So the agent loads none of the user's harness setup, logins included: a key comes from the harness's ``env``, and a
subscription from a gateway on the machine that holds it, such as ``ANTHROPIC_BASE_URL = "http://127.0.0.1:8317"``
with ``127.0.0.1:8317`` in ``allow``. The container is on an internal network (``network``) with no route out. Its
only way out is the worker's egress proxy (``piloop.plugins.runtime.kit.egress``), on the network's gateway, which
admits ``allow`` and the role's HTTP MCP servers.
"""

import getpass
import itertools
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

from pydantic import Field

import piloop
from piloop.plugins.executor.kit.relay import RELAY_ARGV
from piloop.plugins.runtime.contract import (
    MESSAGES_ENV,
    QUEUE,
    TAG_ENV,
    AgentExit,
    AgentSpec,
    Deadline,
    Handle,
    RuntimeConfig,
)
from piloop.plugins.runtime.kit.egress import Allowed, EgressProxy
from piloop.plugins.runtime.subprocess import Command, SubprocessRuntime
from piloop.proc import child_env
from piloop.settings import INPUTS_DIR, settings

PROXIED = ('HTTPS_PROXY', 'HTTP_PROXY', 'https_proxy', 'http_proxy')
"""The variables that send an agent's requests to the egress proxy: curl and Go read lowercase, most others both."""
NODE_PROXY = {'NODE_USE_ENV_PROXY': '1'}
"""Node's own ``fetch`` reads ``HTTPS_PROXY`` only with this (pi)."""
CONTAINER = 'piloop-{token}'
"""The name of an agent's container: one turn at a time has it."""
STANDARD_PATH = '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'
"""The ``PATH`` of an image that sets none."""
GONE = 'No such container'


class Docker(RuntimeConfig):
    """``docker``: each agent in a container of its own image, with the files and the network that it needs only."""

    image: str
    """The image of the agents' containers. It has the harness CLIs that the campaigns use and git. A glibc-based
    image: piloop's own CLI runs in it from the worker's install."""
    allow: list[str] = ['api.anthropic.com', 'api.openai.com']
    """What agents may reach: ``host``, ``host:port`` or ``*.domain`` (port 443 when none). The HTTP MCP servers of an
    agent's role are added to it. A model gateway on this machine is ``127.0.0.1:<port>``."""
    network: str = Field(default='piloop', min_length=1)
    """The Docker network of the containers, which piloop creates as ``--internal``: it has no route out."""
    proxy_port: int = Field(default=3129, ge=1, le=65535)
    """The port of the egress proxy on the network's gateway. A fixed one, so that an agent survives a restart of its
    worker: the next worker serves it on the same address."""
    executable: str = 'docker'
    """The Docker CLI, or a CLI that takes the same arguments, such as ``podman``."""
    link_depth: int = Field(default=2, ge=1)
    """How many folder levels of a worktree piloop reads for the links of ``link``, such as ``data/train.csv``."""
    docker_seconds: float = Field(default=600, gt=0)
    """How long a call of the Docker CLI that sets up or removes a container may take, a pull of the image included."""

    def open(self, name: str) -> DockerRuntime:
        return DockerRuntime(name, self)


@dataclass(frozen=True)
class Mount:
    """A bind mount at the same path in the container as on the machine."""

    path: Path
    writable: bool
    target: Path | None = None
    """Where the container sees it, when not at ``path``."""

    def arg(self) -> str:
        target = self.target or self.path
        return f'type=bind,source={self.path},target={target}' + ('' if self.writable else ',readonly')


class DockerRuntime(SubprocessRuntime):
    """The subprocess runtime, with each turn in ``docker run --rm``: queue, resume, wait and read are the same."""

    def __init__(self, name: str, config: Docker) -> None:
        super().__init__(name)
        self.config = config

    def _command(self, spec: AgentSpec, args: list[str], token: str) -> Command:
        """``docker run`` of the harness, with the agent's variables by name: their values stay off the argv."""
        proxy = egress(self.config)
        proxy.allow(spec.reaches)
        own = own_files(token)
        door = door_cli(token)
        for share in spec.shares:
            share.mkdir(parents=True, exist_ok=True)
        mounts = list(mounts_of(spec, self.config.link_depth))
        prepare(own[0].path, mounts)
        inside = {
            **{k: v for k, v in spec.env.items() if k != 'PATH'},
            'HOME': str(Path.home()),
            'PILOOP_BIN': str(door),
            'PATH': os.pathsep.join([str(door.parent), image_path(self.config)]),
            TAG_ENV: token,
            MESSAGES_ENV: str(spec.log_dir / QUEUE),
            **dict.fromkeys(PROXIED, f'http://{proxy.host}:{proxy.port}'),
            **NODE_PROXY,
        }
        argv = [
            self.config.executable,
            'run',
            '--rm',
            '--init',
            '--name',
            CONTAINER.format(token=token),
            '--network',
            self.config.network,
            '--user',
            f'{os.getuid()}:{os.getgid()}',
            '--workdir',
            str(spec.cwd),
            *itertools.chain.from_iterable(('--mount', m.arg()) for m in [*own, *mounts]),
            *itertools.chain.from_iterable(('--env', k) for k in inside),
            self.config.image,
            spec.executable,
            *args,
        ]
        return Command(argv, child_env() | inside)

    def wait(self, handle: Handle, deadline: Deadline) -> AgentExit:
        """As the subprocess runtime, then remove the agent's own files: its home, its user database and its CLI."""
        try:
            return super().wait(handle, deadline)
        finally:
            gone(handle)

    def kill(self, handle: Handle) -> None:
        """Remove the agent's container, end the ``docker run`` that waited on it, and remove the agent's own files."""
        argv = [self.config.executable, 'rm', '--force', CONTAINER.format(token=str(handle.data['token']))]
        done = subprocess.run(argv, capture_output=True, text=True, timeout=self.config.docker_seconds, check=False)
        if done.returncode != 0 and GONE not in done.stderr:  # a container that is gone already is what kill wants
            raise RuntimeError(f'`{" ".join(argv)}` failed: {done.stderr.strip()}')
        super().kill(handle)
        gone(handle)


def gone(handle: Handle) -> None:
    """Remove the agent's own folder (``agent_dir``), when it is still there."""
    if (folder := agent_dir(str(handle.data['token']))).exists():
        shutil.rmtree(folder)


def mounts_of(spec: AgentSpec, depth: int) -> Iterator[Mount]:
    """What the agent's container mounts, at the same paths: its files, its links, its sessions, and piloop's CLI."""
    inputs = (settings.home / INPUTS_DIR).resolve()
    writable = [spec.cwd, spec.log_dir, *spec.shares]
    own = Path(piloop.__file__).resolve().parent  # outside sys.prefix when installed editable
    readable = [*interpreter(), own]
    for target in links(spec.cwd, depth):
        (readable if target.is_relative_to(inputs) else writable).append(target)
    sockets = [settings.relay_file.parent] if settings.relay_file.exists() else []
    seen: set[Path] = set()
    for path, rw in [*((p, True) for p in [*writable, *sockets]), *((p, False) for p in sorted(readable))]:
        if path not in seen and not (rw and any(path.is_relative_to(s) and s != path for s in writable)):
            seen.add(path)
            yield Mount(path, rw)


def interpreter() -> list[Path]:
    """The installation of each link on the way from this Python to its binary, as written.

    Each is the folder above its ``bin``, where Python finds its standard library from the path that it was started by.
    uv links a minor version's folder (``cpython-3.14-linux-x86_64-gnu``) to the patch release's, and a venv's python
    links to that: the container needs every step.
    """
    found, step = [], Path(sys.executable)
    while step.is_symlink():
        found.append(step.parent.parent)
        step = step.parent / os.readlink(step)
    return [*found, step.parent.parent]


def links(cwd: Path, depth: int) -> list[Path]:
    """The targets outside ``cwd`` of the links in its first ``depth`` folder levels, ``.git`` aside."""
    found: list[Path] = []
    level = [cwd]
    for _ in range(depth):
        below: list[Path] = []
        for folder in level:
            for entry in folder.iterdir() if folder.is_dir() else ():
                if entry.name == '.git':
                    continue
                if entry.is_symlink():
                    target = entry.resolve()
                    if target.exists() and not target.is_relative_to(cwd):
                        found.append(target)
                elif entry.is_dir():
                    below.append(entry)
        level = below
    return found


def prepare(home: Path, mounts: Iterable[Mount]) -> None:
    """Make, in the empty home, each mount point under the machine's home as this user: Docker would make them root's."""
    real = Path.home()
    for mount in mounts:
        if not mount.path.is_relative_to(real):
            continue
        point = home / mount.path.relative_to(real)
        if mount.path.is_dir():
            point.mkdir(parents=True, exist_ok=True)
        else:
            point.parent.mkdir(parents=True, exist_ok=True)
            point.touch(exist_ok=True)


def agent_dir(token: str) -> Path:
    """The folder on this machine of the agent of ``token``: its home, and the user database of its container."""
    return Path(tempfile.gettempdir()) / f'piloop-agent-{token}'


def door_cli(token: str) -> Path:
    """The agent's ``piloop``: the relay's door (``RELAY_ARGV``), which sends each call to its worker's relay socket.

    The container sees none of the home: no database, no CAS. Its own folder holds it, mounted read-only.
    """
    cli = agent_dir(token) / 'bin' / 'piloop'
    cli.parent.mkdir(parents=True, exist_ok=True)
    cli.write_text(f'#!/bin/sh\nPILOOP_HOME={shlex.quote(str(settings.home))} exec {shlex.join(RELAY_ARGV)} "$@"\n')
    cli.chmod(0o755)
    return cli


def own_files(token: str) -> list[Mount]:
    """The agent's own empty home, kept across its turns, and an ``/etc/passwd`` and ``/etc/group`` that name its user.

    A program that asks who it is (piloop, git, ssh) fails on a uid that the image does not know.
    """
    folder = agent_dir(token)
    home = folder / 'home'
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    uid, gid, user = os.getuid(), os.getgid(), getpass.getuser()
    files = {
        'passwd': f'root:x:0:0:root:/root:/bin/sh\n{user}:x:{uid}:{gid}::{Path.home()}:/bin/sh\n',
        'group': f'root:x:0:\n{user}:x:{gid}:\n',
    }
    for name, text in files.items():
        (folder / name).write_text(text)
    return [
        Mount(home, writable=True, target=Path.home()),
        Mount(folder / 'bin', writable=False),
        *(Mount(folder / name, writable=False, target=Path('/etc') / name) for name in files),
    ]


_proxies: dict[tuple[str, int], EgressProxy] = {}
_proxies_lock = threading.Lock()


def egress(config: Docker) -> EgressProxy:
    """The egress proxy of ``config``'s network in this process: started with the network on first use, then shared.

    Raises:
        subprocess.CalledProcessError: Docker cannot create or inspect the network.
        OSError: the proxy cannot bind the network's gateway.
    """
    with _proxies_lock:
        key = (config.network, config.proxy_port)
        if (proxy := _proxies.get(key)) is None:
            gateway = network_gateway(config)
            proxy = EgressProxy(gateway, config.proxy_port, Allowed.of(config.allow))
            proxy.start()
            _proxies[key] = proxy
        return proxy


def network_gateway(config: Docker) -> str:
    """The gateway of the internal network ``config.network``, which this creates when it does not exist."""
    inspect = [
        config.executable,
        'network',
        'inspect',
        config.network,
        '--format',
        '{{(index .IPAM.Config 0).Gateway}}',
    ]
    found = subprocess.run(inspect, capture_output=True, text=True, check=False, timeout=config.docker_seconds)
    if found.returncode != 0:
        create = [config.executable, 'network', 'create', '--internal', config.network]
        subprocess.run(create, capture_output=True, text=True, check=True, timeout=config.docker_seconds)
        found = subprocess.run(inspect, capture_output=True, text=True, check=True, timeout=config.docker_seconds)
    return found.stdout.strip()


_paths: dict[str, str] = {}


def image_path(config: Docker) -> str:
    """The ``PATH`` of the image, where its harness CLIs are. A missing image is pulled first.

    Raises:
        subprocess.CalledProcessError: the image cannot be pulled.
    """
    if config.image not in _paths:
        inspect = [config.executable, 'image', 'inspect', config.image, '--format', '{{json .Config.Env}}']
        found = subprocess.run(inspect, capture_output=True, text=True, check=False, timeout=config.docker_seconds)
        if found.returncode != 0:
            pull = [config.executable, 'pull', '--quiet', config.image]
            subprocess.run(pull, capture_output=True, text=True, check=True, timeout=config.docker_seconds)
            found = subprocess.run(inspect, capture_output=True, text=True, check=True, timeout=config.docker_seconds)
        env = dict(e.split('=', 1) for e in json.loads(found.stdout) or [] if '=' in e)
        _paths[config.image] = env.get('PATH', STANDARD_PATH)
    return _paths[config.image]
