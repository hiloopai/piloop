"""The docker runtime: an agent sees its own files and the hosts that it may reach, nothing else of the machine."""

import http.server
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Generator
from pathlib import Path
from uuid import uuid4

import pytest

from piloop.ids import AgentSessionId
from piloop.plugins.executor.kit.door import Call, Relayed
from piloop.plugins.executor.kit.relay import Relay
from piloop.plugins.runtime import docker
from piloop.plugins.runtime.contract import AgentSpec, Deadline, ExitStatus, Handle
from piloop.plugins.runtime.docker import Docker, DockerRuntime, Mount, links, mounts_of
from piloop.plugins.runtime.kit.egress import Allowed, EgressProxy
from piloop.settings import INPUTS_DIR, settings

IMAGE = 'buildpack-deps:noble-scm'
"""A glibc image with bash and git: piloop's own CLI runs in it from the worker's install."""
needs_docker = pytest.mark.skipif(
    shutil.which('docker') is None or subprocess.run(['docker', 'info'], capture_output=True).returncode != 0,
    reason='needs a Docker daemon',
)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


@pytest.fixture
def origin() -> Generator[int]:
    """A web server on this machine's loopback, as a model gateway that holds a login."""

    class Hello(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'hello from the host')

    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Hello)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


def test_the_allowlist_takes_hosts_ports_and_subdomains() -> None:
    allowed = Allowed.of(['api.anthropic.com', '127.0.0.1:8317', '*.example.com:8443'])
    assert ('api.anthropic.com', 443) in allowed and ('API.anthropic.com', 443) in allowed
    assert ('api.anthropic.com', 80) not in allowed, 'port 443 when none'
    assert ('127.0.0.1', 8317) in allowed and ('127.0.0.1', 22) not in allowed
    assert ('mcp.example.com', 8443) in allowed and ('example.com', 8443) not in allowed
    assert ('evil.com', 443) not in allowed


def proxy_get(port: int, request: bytes) -> bytes:
    with socket.create_connection(('127.0.0.1', port), timeout=10) as s:
        s.sendall(request)
        chunks = []
        while data := s.recv(65536):
            chunks.append(data)
        return b''.join(chunks)


def test_the_egress_proxy_forwards_allowed_requests_and_refuses_the_rest(origin: int) -> None:
    proxy = EgressProxy('127.0.0.1', 0, Allowed.of([]))
    port = proxy.start()
    try:
        url = f'http://127.0.0.1:{origin}/v1/x'
        get = f'GET {url} HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n'.encode()
        assert proxy_get(port, get).startswith(b'HTTP/1.1 403'), 'nothing allowed yet'
        proxy.allow([f'127.0.0.1:{origin}'])
        assert proxy_get(port, get).endswith(b'hello from the host'), 'an absolute-form request goes to its origin'
        connect = f'CONNECT 127.0.0.1:{origin} HTTP/1.1\r\n\r\n'.encode()
        with socket.create_connection(('127.0.0.1', port), timeout=10) as s:
            s.sendall(connect)
            assert s.recv(1024).startswith(b'HTTP/1.1 200'), 'a tunnel'
            s.sendall(b'GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n')
            assert b''.join(iter(lambda: s.recv(65536), b'')).endswith(b'hello from the host')
        assert proxy_get(port, b'CONNECT evil.com:443 HTTP/1.1\r\n\r\n').startswith(b'HTTP/1.1 403')
        assert proxy_get(port, b'GET https://x/ HTTP/1.1\r\n\r\n').startswith(b'HTTP/1.1 403'), (
            'only http in absolute form'
        )
        assert proxy_get(port, b'CONNECT nohost HTTP/1.1\r\n\r\n').startswith(b'HTTP/1.1 403')
        assert proxy_get(port, b'HELLO\r\n\r\n').startswith(b'HTTP/1.1 400')
        dead = free_port()
        proxy.allow([f'127.0.0.1:{dead}'])
        assert proxy_get(port, f'CONNECT 127.0.0.1:{dead} HTTP/1.1\r\n\r\n'.encode()).startswith(b'HTTP/1.1 502')
        with socket.create_connection(('127.0.0.1', port), timeout=10) as s:
            s.sendall(b'GET / HTTP/1.1\r\n')  # a head that never ends: the client leaves
        assert proxy_get(port, b'CONNECT evil.com:443 HTTP/1.1\r\n\r\n').startswith(b'HTTP/1.1 403'), 'it serves on'
        tunnel = socket.create_connection(('127.0.0.1', port), timeout=10)
        tunnel.sendall(connect)
        assert tunnel.recv(1024).startswith(b'HTTP/1.1 200')
    finally:
        proxy.close()
    assert tunnel.recv(1024) == b'', 'a tunnel open at the close is closed'
    tunnel.close()


def worktree(tmp_path: Path) -> Path:
    """A git worktree of a repository, as a worker checks one out, with a link to an input and to a workspace."""
    repo, cwd = tmp_path / 'repo', tmp_path / 'logs' / 'worktree'
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    subprocess.run(['git', '-C', str(repo), 'commit', '-q', '--allow-empty', '-m', 'base'], check=True)
    subprocess.run(['git', '-C', str(repo), 'worktree', 'add', '-q', str(cwd)], check=True)
    inputs = settings.home / INPUTS_DIR / 'blob'
    inputs.mkdir(parents=True, exist_ok=True)
    (inputs / 'train.csv').write_text('a,b\n')
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    (cwd / 'data').mkdir()
    (cwd / 'data' / 'train.csv').symlink_to(inputs / 'train.csv')
    (cwd / 'workspace').symlink_to(workspace)
    (cwd / 'dangling').symlink_to(tmp_path / 'nowhere')
    (cwd / 'data' / 'notes.txt').write_text('a file, not a link')
    return cwd


def test_a_container_mounts_the_agent_s_files_its_links_its_sessions_and_piloop_at_their_paths(tmp_path: Path) -> None:
    cwd = worktree(tmp_path)
    sessions = tmp_path / 'home' / '.claude' / 'projects'
    spec = AgentSpec(
        executable='claude',
        args=[],
        cwd=cwd,
        log_dir=cwd.parent,
        env={},
        shares=(sessions, tmp_path / 'repo' / '.git'),
    )
    found = {m.path: m.writable for m in mounts_of(spec, 2)}
    assert found[tmp_path / 'repo' / '.git'] is True, 'the commits go there'
    assert found[cwd.parent] is True and cwd not in found, 'the worktree is in the log directory'
    assert found[sessions] is True
    assert found[(settings.home / INPUTS_DIR / 'blob' / 'train.csv').resolve()] is False, 'inputs are read-only'
    assert found[tmp_path / 'workspace'] is True
    assert found[Path(sys.prefix)] is False
    assert sorted(t.name for t in links(cwd, 2)) == ['train.csv', 'workspace']
    assert [t.name for t in links(cwd, 1)] == ['workspace'], 'data/train.csv is one level down'
    assert Mount(tmp_path, writable=False, target=Path('/x')).arg() == f'type=bind,source={tmp_path},target=/x,readonly'


@pytest.fixture
def runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Generator[DockerRuntime]:
    """A docker runtime on a network of its own, with the user's own files in a home of their own."""
    home = tmp_path / 'user-home'
    (home / '.claude').mkdir(parents=True)
    (home / '.claude' / 'CLAUDE.md').write_text('the user instructions')
    (home / '.claude' / '.credentials.json').write_text('{"token": "secret"}')
    monkeypatch.setenv('HOME', str(home))
    network = f'piloop-test-{uuid4().hex[:8]}'
    config = Docker(image=IMAGE, network=network, proxy_port=free_port(), allow=[])
    yield config.open('docker')
    proxy = docker._proxies.pop((network, config.proxy_port), None)
    if proxy is not None:
        proxy.close()
    subprocess.run(['docker', 'network', 'rm', network], capture_output=True, check=False)


def agent(tmp_path: Path, body: str, origin: int | None = None) -> AgentSpec:
    cwd = worktree(tmp_path)
    return AgentSpec(
        executable='bash',
        args=['-c', body],
        cwd=cwd,
        log_dir=cwd.parent,
        env={
            'PILOOP_ACTOR': 'implementer#1',
            'PILOOP_AGENT_TOKEN': 'the-token',
            'AGENT_SECRET': 'only-by-name',
            'PATH': '/host/only',
        },
        shares=(Path.home() / '.claude' / 'projects', tmp_path / 'repo' / '.git'),
        reaches=() if origin is None else (f'127.0.0.1:{origin}',),
    )


@pytest.fixture
def relay() -> Generator[list[Call]]:
    """The worker's relay, which takes the agent's ``piloop`` calls: it answers each with what it got."""
    calls: list[Call] = []

    def send(lease: int, call: Call) -> Relayed:
        calls.append(call)
        return Relayed(stdout=f'relayed on lease {lease}: {" ".join(call.argv)}\n', exit_code=0)

    served = Relay(settings.relay_file, send)
    served.start()
    served.open({AgentSessionId(1): 7})
    yield calls
    served.stop()


@needs_docker
def test_an_agent_sees_its_files_and_allowed_hosts_and_nothing_else_of_the_machine(
    runtime: DockerRuntime, tmp_path: Path, origin: int, relay: list[Call]
) -> None:
    body = r"""
set -u
echo "start $(date +%s.%N)"
echo "home: $(ls -A "$HOME" | tr '\n' ' ')"
test -e "$HOME/.claude/CLAUDE.md" && echo "LEAK claude.md"
test -e "$HOME/.claude/.credentials.json" && echo "LEAK credentials"
echo "secret: $AGENT_SECRET"
echo "path: $PATH"
echo work > made-here.txt && git add made-here.txt && git -c user.email=a@b -c user.name=a commit -qm made && echo committed
echo "input: $(cat data/train.csv | head -1)"
touch workspace/shared && echo "workspace written"
echo "session" > "$HOME/.claude/projects/s.jsonl"
piloop sql "SELECT 1" && test ! -e "$DB" && echo "no database"
gw="${HTTPS_PROXY#http://}"
exec 3<>"/dev/tcp/${gw%:*}/${gw##*:}"; printf 'CONNECT example.com:443 HTTP/1.1\r\n\r\n' >&3; echo "denied: $(head -1 <&3)"
exec 4<>"/dev/tcp/${gw%:*}/${gw##*:}"
printf "GET http://127.0.0.1:$ORIGIN/ HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n" >&4; echo "allowed: $(tail -1 <&4)"
(exec 5<>/dev/tcp/1.1.1.1/443) 2>/dev/null && echo "LEAK route out" || echo "no route out"
"""
    (settings.home / 'piloop.db').touch()
    spec = agent(
        tmp_path, body.replace('$ORIGIN', str(origin)).replace('$DB', str(settings.home / 'piloop.db')), origin
    )
    began = time.time()
    handle = runtime.start(spec)
    assert docker.egress(runtime.config) is docker.egress(runtime.config), 'one proxy for the network'
    result = runtime.wait(handle, Deadline.after(120))
    out = (spec.log_dir / 'agent_session.stdout').read_text()
    err = (spec.log_dir / 'agent_session.stderr').read_text()
    assert (result.status, result.exit_code) == (ExitStatus.EXITED, 0), err
    started = float(next(line for line in out.splitlines() if line.startswith('start ')).split()[1])
    print(f'docker start latency: {started - began:.2f}s')
    assert started - began < 30
    assert 'LEAK' not in out
    assert 'secret: only-by-name' in out and '/host/only' not in out
    assert 'committed' in out and (spec.cwd / 'made-here.txt').is_file()
    assert (
        subprocess.run(['git', '-C', str(spec.cwd), 'log', '-1', '--format=%s'], capture_output=True, text=True).stdout
        == 'made\n'
    )
    assert 'input: a,b' in out and (tmp_path / 'workspace' / 'shared').is_file()
    assert (Path.home() / '.claude' / 'projects' / 's.jsonl').read_text() == 'session\n', 'the worker reads sessions'
    assert 'relayed on lease 7: sql SELECT 1' in out and 'no database' in out, err
    assert [(c.argv, c.token.get_secret_value()) for c in relay] == [(['sql', 'SELECT 1'], 'the-token')]
    assert 'denied: HTTP/1.1 403 Forbidden' in out and 'allowed: hello from the host' in out and 'no route out' in out
    assert (spec.cwd / 'made-here.txt').stat().st_uid == os.getuid(), "the worker's user"
    assert not docker.agent_dir(str(handle.data['token'])).exists(), 'its home goes with the agent'


@needs_docker
def test_a_killed_agent_s_container_is_gone(runtime: DockerRuntime, tmp_path: Path) -> None:
    handle = runtime.start(agent(tmp_path, 'echo up; sleep 300'))
    name = docker.CONTAINER.format(token=handle.data['token'])
    deadline = time.monotonic() + 60
    while name not in subprocess.run(['docker', 'ps', '--format', '{{.Names}}'], capture_output=True, text=True).stdout:
        assert time.monotonic() < deadline, 'the container never ran'
        time.sleep(0.2)
    runtime.kill(handle)
    assert (
        name
        not in subprocess.run(['docker', 'ps', '-a', '--format', '{{.Names}}'], capture_output=True, text=True).stdout
    )
    assert not docker.agent_dir(str(handle.data['token'])).exists()
    runtime.kill(handle)  # again: a container that is gone is not an error


def test_a_kill_that_docker_refuses_says_why(tmp_path: Path) -> None:
    broken = Docker(image=IMAGE, executable='false').open('docker')
    with pytest.raises(RuntimeError, match='`false rm --force piloop-t` failed'):
        broken.kill(Handle('docker', {'token': 't', 'pid': 1, 'start_time': 0.0, 'log_dir': str(tmp_path)}))


def test_a_mount_point_under_the_home_is_made_as_this_user_a_file_as_a_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv('HOME', str(tmp_path / 'user'))
    (Path.home() / 'cache').mkdir(parents=True)
    (Path.home() / 'cache' / 'model.bin').write_bytes(b'x')
    empty = tmp_path / 'empty'
    mounts = [
        Mount(Path.home() / 'cache' / 'model.bin', False),
        Mount(Path.home() / 'cache', True),
        Mount(tmp_path, True),
    ]
    docker.prepare(empty, mounts)
    assert (empty / 'cache' / 'model.bin').is_file() and (empty / 'cache').is_dir()
    assert sorted(p.name for p in empty.iterdir()) == ['cache'], 'a path outside the home needs no mount point there'


def fake_docker(tmp_path: Path) -> Path:
    """A Docker CLI whose image is missing until a pull, and whose network exists once created."""
    cli = tmp_path / 'docker'
    cli.write_text(
        f"""#!/bin/sh
echo "$*" >> {tmp_path}/calls
case "$1 $2" in
  "image inspect") test -f {tmp_path}/pulled && echo '["PATH=/opt/bin:/usr/bin","LANG=C"]' || exit 1 ;;
  "pull --quiet") touch {tmp_path}/pulled ;;
  "network inspect") test -f {tmp_path}/created && echo 172.30.0.1 || exit 1 ;;
  "network create") touch {tmp_path}/created ;;
esac
"""
    )
    cli.chmod(0o755)
    return cli


def test_a_missing_image_is_pulled_once_and_a_missing_network_is_created_once(tmp_path: Path) -> None:
    config = Docker(image='acme/agent:1', executable=str(fake_docker(tmp_path)), network='n')
    assert docker.image_path(config) == '/opt/bin:/usr/bin'
    assert docker.image_path(config) == '/opt/bin:/usr/bin'
    present = Docker(image='acme/present:1', executable=str(fake_docker(tmp_path)), network='n')
    assert docker.image_path(present) == '/opt/bin:/usr/bin', 'an image that is there is not pulled'
    assert docker.network_gateway(config) == '172.30.0.1'
    assert docker.network_gateway(config) == '172.30.0.1'
    assert (tmp_path / 'calls').read_text().splitlines() == [
        'image inspect acme/agent:1 --format {{json .Config.Env}}',
        'pull --quiet acme/agent:1',
        'image inspect acme/agent:1 --format {{json .Config.Env}}',
        'image inspect acme/present:1 --format {{json .Config.Env}}',
        'network inspect n --format {{(index .IPAM.Config 0).Gateway}}',
        'network create --internal n',
        'network inspect n --format {{(index .IPAM.Config 0).Gateway}}',
        'network inspect n --format {{(index .IPAM.Config 0).Gateway}}',
    ]
