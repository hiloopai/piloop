"""API processes beside the daemon (``[daemon] api_processes``): they serve on its sockets, and live and die with it."""

import os
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Generator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from uuid import uuid4

import httpx2 as httpx
import psutil
import pytest
from covalence import Cid
from git import Repo
from sqlalchemy import func, select

from piloop.actor import System
from piloop.api.client import answers
from piloop.api.contract import MAX_POLL_SECONDS, Access
from piloop.campaign import inbox
from piloop.daemon import served, tier as tier_module
from piloop.daemon.loop import Turn
from piloop.daemon.main import Daemon
from piloop.daemon.tier import CHILD_ARGV, ApiProcesses, NotServing
from piloop.db import terms
from piloop.db.feed import newest
from piloop.db.models import utcnow
from piloop.db.session import Database
from piloop.ids import Revision
from piloop.net.server import local_listener
from piloop.otel import Tracer
from piloop.plugins.campaign_type.kit.evaluation.tables import Evaluation, Measurement
from piloop.plugins.executor.contract import RepoId
from piloop.plugins.executor.kit.client import WorkerInfo
from piloop.plugins.executor.kit.leases import Grant
from piloop.plugins.executor.kit.remote import Remote
from piloop.plugins.executor.kit.tables import FirstTransfer
from piloop.plugins.network.contract import Address
from piloop.settings import DaemonSettings, Settings, socket_at
from tests.conftest import Dial, baseline, disk, register, wait_until
from tests.isolation import API_PROCESSES_ENV
from tests.test_lease import HOST, MEASURED, PROBE, evaluation_job, queue
from tests.test_lease_client import secret


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """A home whose daemon serves its API from two API processes, which read the same ``config.toml``."""
    monkeypatch.delenv(API_PROCESSES_ENV)
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
        wait_until(lambda: bool(daemon._servers), 'every API process said that it serves')
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
        served.serving(local, None, 0, cfg),
    ):
        pass


@contextmanager
def listening(cfg: Settings) -> Generator[Daemon]:
    """A daemon of ``cfg`` whose API processes serve, on the socket and on a port of the loopback, with no loop."""
    daemon = Daemon(cfg, listen=Address(host='127.0.0.1', port=0))
    try:
        with daemon.tracer.span('test') as span:
            daemon._listen(span)
        yield daemon
    finally:
        daemon.shutdown(timeout=5)


@contextmanager
def remote(daemon: Daemon, cfg: Settings, dial: Dial, worker: str, pool: str = 'remote') -> Generator[Remote]:
    """A worker on another machine, registered in ``pool``, which calls the daemon's network listener."""
    with on_socket(cfg) as http:
        server = http.get('/v1/status').json()['daemon']['listen']
    assert disk(cfg.home).put(b'piloop worker cas probe\n') == PROBE, 'it shares the CAS of the home'
    with dial(server, secret(daemon.db, worker)) as http:
        client = Remote(http, threading.Event(), Tracer(None))
        client.register(WorkerInfo(version='0.2.0', pool=pool, runners=['process'], sample=HOST, cas_probe=PROBE))
        yield client


REMOTE_POOL = '[plugins.executor.pools.remote]\nagents = 0\nslots = { evaluation = 1 }\n'


def test_a_send_that_waits_in_an_api_process_wakes_on_the_build_of_the_daemon(
    cfg: Settings, campaign_path: Path, dial: Dial
) -> None:
    """The request writes the row in the API process; the daemon's consumer builds; the end wakes the long-poll."""
    (cfg.home / 'config.toml').write_text('[daemon]\napi_processes = 2\n' + REMOTE_POOL)
    cfg = Settings(home=cfg.home)
    with listening(cfg) as daemon:
        campaign = register(daemon.db, campaign_path)
        repo, head = RepoId(campaign.id), Revision(Repo(campaign_path).head.commit.hexsha)
        sent: list[list[Cid]] = []
        with remote(daemon, cfg, dial, 'w1') as worker:
            polling = threading.Thread(target=lambda: sent.append(worker.send(repo, [head], [])))
            polling.start()
            wait_until(lambda: asked(daemon.db, repo), 'the API process asks for the first transfer')
            [builds] = [c for c in daemon.scheduler.consumers() if c.name == 'first_transfers']
            builds.run(Turn(utcnow()))
            polling.join(MAX_POLL_SECONDS)
        assert [len(blobs) for blobs in sent] == [1], 'the long-poll in the API process answered with the build'


def asked(db: Database, repo: RepoId) -> bool:
    with db.session() as s:
        return s.get(FirstTransfer, repo) is not None


def test_a_report_that_an_api_process_applies_while_the_term_stops_records_nothing(
    cfg: Settings, campaign_path: Path, tmp_path: Path, dial: Dial
) -> None:
    """The API process reads the stop from the term row, not from the daemon's memory."""
    (cfg.home / 'config.toml').write_text('[daemon]\napi_processes = 2\n' + REMOTE_POOL)
    cfg = Settings(home=cfg.home)
    with ExitStack() as stack:
        daemon = stack.enter_context(listening(cfg))
        campaign = register(daemon.db, campaign_path)
        worker = stack.enter_context(remote(daemon, cfg, dial, 'w1'))
        base = baseline(daemon.db, campaign.id)
        recorded = select(func.count()).select_from(Measurement).where(Measurement.candidate_id == base.id)

        def measure() -> int:
            job = evaluation_job(daemon.db, campaign, tmp_path)
            with daemon.db.session() as s:
                s.get_one(Evaluation, job.evaluation).started_at = utcnow()  # the daemon's queue started it
                s.commit()
            queue(daemon.db, job)
            grant = worker.acquire(uuid4().hex, HOST)
            assert isinstance(grant, Grant)
            worker.report(grant.lease, uuid4().hex, MEASURED)
            with daemon.db.session() as s:
                return s.execute(recorded).scalar_one()

        before = measure()
        with daemon.db.session() as s:
            terms.stop(s)
            s.commit()
        assert measure() == before, 'the term stops: what it measured may be cut short, and the next daemon asks again'


def test_more_api_processes_than_relayed_calls_do_not_start(cfg: Settings) -> None:
    """Each API process runs its share of ``relayed_calls``, at least one: the processes refuse more of them."""
    (cfg.home / 'config.toml').write_text('[daemon]\napi_processes = 3\n[plugins.executor]\nrelayed_calls = 2\n')
    refused = r'PluginError: 3 API processes serve .* Set \[daemon\] api_processes to 2 or less, or relayed_calls to 3'
    with pytest.raises(SystemExit, match=refused), listening(Settings(home=cfg.home)):
        pass


def test_an_api_process_whose_code_or_settings_are_not_the_daemon_s_does_not_serve(
    cfg: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tier_module, 'digest', lambda _cfg: 'the code of another install')
    with pytest.raises(SystemExit, match="NotServing: its code or its settings are not the daemon's"), listening(cfg):
        pass


FAKE = (
    "import os, sys, time; *_, control, ready = os.environ['PILOOP_API_FDS'].split(',');"
    ' control, ready = int(control), int(ready);'
)
"""The start of an API process's stand-in: it finds its pipes, as an API process does."""


@pytest.fixture
def tier(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[..., ApiProcesses]:
    """An ``ApiProcesses`` of one process that runs ``code`` after ``FAKE`` in place of the API, with ``failed``."""

    def make(code: str, failed: Callable[[str], None] = lambda why: None, **daemon: object) -> ApiProcesses:
        # The stand-in uses only the stdlib: site/coverage startup must not consume its readiness deadline.
        monkeypatch.setattr(tier_module, 'CHILD_ARGV', (sys.executable, '-S', '-c', FAKE + code))
        settings = DaemonSettings.model_validate({'api_processes': 1, 'api_start': 10, **daemon})
        cfg = Settings(home=tmp_path).model_copy(update={'daemon': settings})
        listening = local_listener(socket_at(tmp_path / f'api-{uuid4().hex[:8]}.sock')).socket
        return ApiProcesses(cfg, [listening, None], Tracer(None), failed)

    return make


def test_an_api_process_that_does_not_end_at_the_stop_is_killed_after_its_close(
    tier: Callable[..., ApiProcesses],
) -> None:
    processes = tier(
        "os.write(ready, b'ready\\n'); import signal; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
    )
    processes.start()
    [pid] = processes.pids()
    started = time.monotonic()
    processes.close(0.5)
    assert not psutil.pid_exists(pid) and time.monotonic() - started < 10


@pytest.mark.parametrize(
    ('code', 'start', 'said'),
    [
        (
            "os.write(ready, b'the route GET /v1/status is taken\\n'); sys.exit(1)",
            30,
            'the route GET /v1/status is taken',
        ),
        ('sys.exit(3)', 30, 'it exited with 3'),
        ('os.read(control, 1)', 0.5, 'not within 0.5 s'),
        ('os.close(ready); os.read(control, 1)', 0.5, 'it closed its ready pipe, and runs on'),
    ],
    ids=['says why', 'exits', 'hangs', 'closes its pipe'],
)
def test_the_start_fails_with_why_the_first_api_process_does_not_serve(
    tier: Callable[..., ApiProcesses], code: str, start: float, said: str
) -> None:
    processes = tier(code, api_start=start)
    with pytest.raises(NotServing, match=rf'^API process 0 \(pid \d+\) does not serve: {said}$'):
        processes.start()
    assert processes.pids() and not [p for p in processes.pids() if alive(p)], 'and none is left'


def alive(pid: int) -> bool:
    return psutil.pid_exists(pid) and psutil.Process(pid).status() != psutil.STATUS_ZOMBIE


def test_a_stop_while_the_daemon_waits_to_start_an_api_process_again_starts_none(
    tier: Callable[..., ApiProcesses],
) -> None:
    processes = tier("os.write(ready, b'ready\\n')", api_respawn=30)  # it exits once it served
    processes.start()
    [pid] = processes.pids()
    wait_until(lambda: not alive(pid), 'it exits')
    started = time.monotonic()
    processes.close()
    assert processes.pids() == [pid] and time.monotonic() - started < 10, 'the stop ends the wait, and starts none'


def test_api_processes_started_again_that_keep_failing_stop_the_daemon(
    tier: Callable[..., ApiProcesses], tmp_path: Path
) -> None:
    """The first one served, then exited: the ones started in its place fail to serve, two in a row."""
    once = tmp_path / 'served-once'
    code = (
        f'from pathlib import Path; once = Path({str(once)!r})\n'
        "if once.exists(): os.write(ready, b'the CAS is gone\\n'); sys.exit(1)\n"
        "once.touch(); os.write(ready, b'ready\\n')"
    )
    why: list[str] = []
    processes = tier(code, failed=why.append, api_respawn=0, api_respawns=2)
    processes.start()
    wait_until(lambda: why, 'the daemon is told to stop')
    processes.close()
    assert why == [f'API process 0 (pid {processes.pids()[0]}) does not serve: the CAS is gone, and 2 in a row']


def test_a_stop_that_comes_while_api_processes_are_started_again_leaks_no_process_pipe_or_thread(
    tier: Callable[..., ApiProcesses], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each one serves and exits at once, and is started again at once: stops land at every point of that."""
    raised: list[BaseException | None] = []
    monkeypatch.setattr(threading, 'excepthook', lambda hook: raised.append(hook.exc_value))
    fds = psutil.Process().num_fds()
    for pause in (0, 0.01, 0.05, 0.1, 0.2):
        processes = tier("os.write(ready, b'ready\\n')", api_respawn=0, api_respawns=1000)
        seen: set[int] = set()
        processes.start()
        time.sleep(pause)
        seen.update(processes.pids())
        processes.close()
        seen.update(processes.pids())
        assert not [p for p in seen if alive(p)], 'every one it started has exited'
        assert not [t for t in threading.enumerate() if t.name == 'api-0'], 'its watcher ended'
    assert raised == [] and psutil.Process().num_fds() == fds, 'no thread failed, and every pipe is closed'


def test_an_api_process_that_cannot_be_started_fails_the_start_and_leaves_no_pipe(
    tier: Callable[..., ApiProcesses], monkeypatch: pytest.MonkeyPatch
) -> None:
    processes = tier('')
    monkeypatch.setattr(tier_module, 'CHILD_ARGV', ('/nonexistent/python',))
    fds = psutil.Process().num_fds()
    with pytest.raises(FileNotFoundError):
        processes.start()
    assert psutil.Process().num_fds() == fds - 1, 'no pipe is left, and the socket that it was given is closed'


def test_a_stop_while_a_new_api_process_starts_counts_no_failure(
    tier: Callable[..., ApiProcesses], tmp_path: Path
) -> None:
    """The first one served and exited; the one in its place has not served yet when the daemon stops."""
    once = tmp_path / 'served-once'
    code = (
        f'from pathlib import Path; once = Path({str(once)!r})\n'
        'if once.exists(): os.read(control, 1); sys.exit(0)\n'
        "once.write_text(str(os.getpid())); os.write(ready, b'ready\\n')"
    )
    why: list[str] = []
    processes = tier(code, failed=why.append, api_respawn=0, api_respawns=1)
    processes.start()
    first = int(once.read_text())
    wait_until(lambda: processes.pids() != [first], 'a new one starts, and waits')
    processes.close()
    assert why == [], 'the stop ended it: no failure'


def test_a_daemon_whose_api_processes_keep_failing_stops(cfg: Settings) -> None:
    with listening(cfg) as daemon:
        daemon._api_failed('API process 0 does not serve: the CAS is gone, and 3 in a row')
        assert daemon.scheduler._stopped(), 'the loop ends, as on SIGTERM'
