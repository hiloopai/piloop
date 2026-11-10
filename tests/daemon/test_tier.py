"""API processes beside the daemon (``[daemon] api_processes``): they serve on its sockets, and live and die with it."""

import os
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

import httpx2 as httpx
import psutil
import pytest

from piloop.actor import System
from piloop.api.client import answers
from piloop.api.contract import Access
from piloop.campaign import inbox
from piloop.daemon import served, tier as tier_module
from piloop.daemon.main import Daemon
from piloop.daemon.tier import CHILD_ARGV, ApiProcesses
from piloop.db.feed import newest
from piloop.db.session import Database
from piloop.otel import Tracer
from piloop.plugins.network.contract import Address
from piloop.settings import Settings
from tests.conftest import register, wait_until
from tests.test_lease_client import secret


@pytest.fixture
def cfg(tmp_path: Path) -> Settings:
    """A home whose daemon serves its API from two API processes, which read the same ``config.toml``."""
    home = tmp_path / 'home'
    home.mkdir(mode=0o700)
    (home / 'config.toml').write_text('[daemon]\napi_processes = 2\n')
    return Settings(home=home)


def on_socket(cfg: Settings) -> httpx.Client:
    return httpx.Client(transport=httpx.HTTPTransport(uds=str(cfg.socket_file)), base_url='http://piloop', timeout=30)


def api_processes(pid: int) -> list[psutil.Process]:
    """The API processes that the daemon ``pid`` started."""
    tier = CHILD_ARGV[-1]
    return [p for p in psutil.Process(pid).children() if tier in p.cmdline()]


def serving(daemon: Daemon, checks: Callable[[], None]) -> None:
    """Serve ``daemon`` in this thread, the main one, while ``checks`` run in another; then stop it as SIGTERM does."""
    failed: list[BaseException] = []

    def run() -> None:
        try:
            checks()
        except BaseException as e:  # the test's failure, raised again in the test's thread
            failed.append(e)
        finally:
            os.kill(os.getpid(), signal.SIGTERM)

    threading.Thread(target=run, daemon=True).start()
    daemon.serve()
    if failed:
        raise failed[0]


def test_api_processes_serve_on_the_daemon_s_sockets_wake_on_its_commits_and_one_that_dies_is_replaced(
    cfg: Settings, campaign_path: Path
) -> None:
    """The daemon serves no HTTP itself. Its term answers, whichever API process serves the call, and a commit of the
    daemon wakes a long-poll that waits in an API process (its doorbell)."""
    daemon = Daemon(cfg, listen=Address(host='127.0.0.1', port=0))
    campaign = register(daemon.db, campaign_path)
    ran: list[int] = []

    def checks() -> None:
        wait_until(lambda: answers(cfg.socket_file), 'an API process answers on the socket')
        [processes] = daemon._servers
        assert isinstance(processes, ApiProcesses)
        first = processes.pids()
        ran.extend(first)
        assert len(first) == 2 and os.getpid() not in first
        assert not any(t.name == 'piloop-server' for t in threading.enumerate()), 'the daemon serves no HTTP itself'
        with on_socket(cfg) as http:
            status = http.get('/v1/status').json()
        assert status['daemon']['pid'] == os.getpid(), 'the daemon of the term answers'
        reader = {'authorization': f'Bearer {secret(daemon.db, "laptop", Access.READ)}'}
        with httpx.Client(base_url=status['daemon']['listen'], timeout=30) as tcp:
            assert tcp.get('/v1/status', headers=reader).is_success, 'on the network too'
        with daemon.db.session() as s:
            after = newest(s)
        woke: list[tuple[float, int]] = []

        def long_poll() -> None:
            with on_socket(cfg) as http:
                answer = http.get('/v1/events', params={'after': after, 'wait': 20, 'kind': 'comment.message'})
            woke.append((time.monotonic(), len(answer.json()['events'])))

        polling = threading.Thread(target=long_poll)
        polling.start()
        time.sleep(1)  # it waits in an API process
        with daemon.db.acting(System.HUMAN) as s:
            inbox.message(s, campaign.id, 'from the daemon', author=System.HUMAN, priority=1)
        committed = time.monotonic()
        polling.join(20)
        [(at, events)] = woke
        assert events == 1 and at - committed < 2, 'the daemon commit wakes the long-poll of another process'
        os.kill(first[0], signal.SIGKILL)
        wait_until(lambda: len(set(processes.pids()) - set(first)) == 1, 'an API process in place of the dead one')
        ran.extend(processes.pids())
        wait_until(lambda: answers(cfg.socket_file), 'the API answers again')

    serving(daemon, checks)
    assert ran and not [p for p in ran if psutil.pid_exists(p)], 'the stop ends every API process'


def test_api_processes_exit_when_their_daemon_dies(cfg: Settings) -> None:
    """A daemon killed with SIGKILL closes the pipe of each API process: none outlives it on the daemon's sockets."""
    env = {**os.environ, 'PILOOP_HOME': str(cfg.home)}
    with subprocess.Popen([sys.executable, '-m', 'piloop', 'serve'], env=env, stdin=subprocess.DEVNULL) as daemon:
        wait_until(lambda: answers(cfg.socket_file) and len(api_processes(daemon.pid)) == 2, 'the API processes serve')
        children = api_processes(daemon.pid)
        daemon.kill()
    _, alive = psutil.wait_procs(children, timeout=20)
    assert not alive, 'each API process exits once its daemon is gone'


def test_an_api_process_serves_only_beside_a_daemon_that_began_a_term(db: Database, tmp_path: Path) -> None:
    cfg = Settings(home=tmp_path)
    with (
        socket.socket(socket.AF_UNIX) as local,
        pytest.raises(SystemExit, match='no daemon began a term on'),
        served.serving(local, None, cfg),
    ):
        pass


@pytest.fixture
def tier(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[[str], ApiProcesses]:
    """An ``ApiProcesses`` of one process that runs ``code`` in place of the API, on a Unix socket of its own."""

    def make(code: str) -> ApiProcesses:
        monkeypatch.setattr(tier_module, 'CHILD_ARGV', (sys.executable, '-c', code))
        listening = socket.socket(socket.AF_UNIX)
        listening.bind(str(tmp_path / 'api.sock'))
        return ApiProcesses(1, [listening, None], tmp_path, Tracer(None))

    return make


def test_an_api_process_that_does_not_end_at_the_stop_is_killed_after_the_grace(
    tier: Callable[[str], ApiProcesses],
) -> None:
    processes = tier(
        "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('up', flush=True); time.sleep(60)"
    )
    processes.start()
    [pid] = processes.pids()
    wait_until(lambda: psutil.Process(pid).status() == psutil.STATUS_SLEEPING, 'it ignores SIGTERM')
    started = time.monotonic()
    processes.close(0.5)
    assert not psutil.pid_exists(pid) and time.monotonic() - started < 10


def test_a_stop_while_the_daemon_waits_to_start_an_api_process_again_starts_none(
    tier: Callable[[str], ApiProcesses], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tier_module, 'RESPAWN_SECONDS', 30.0)
    processes = tier('pass')  # it exits at once: the daemon waits before it starts another
    processes.start()
    [pid] = processes.pids()
    wait_until(lambda: not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE, 'it exits')
    started = time.monotonic()
    processes.close()
    assert processes.pids() == [pid] and time.monotonic() - started < 10, 'the stop ends the wait, and starts none'
