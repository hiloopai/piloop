"""A process that serves the API needs the database alone: no scheduler, run controller or opened executor."""

import threading
from collections.abc import Generator
from pathlib import Path
from typing import Self

import pytest
from starlette.routing import NoMatchFound

from piloop.api.contract import Access
from piloop.daemon.api import daemon_state
from piloop.daemon.served import Served
from piloop.db import terms
from piloop.db.models import Campaign
from piloop.db.session import Database
from piloop.net.server import Server, listening
from piloop.net.tokens import Tokens
from piloop.otel import Tracer
from piloop.plugins.executor import workers as workers_module
from piloop.plugins.executor.contract import EXECUTORS, Executor, ExecutorConfig, Host
from piloop.plugins.executor.kit.client import WorkerInfo
from piloop.plugins.executor.kit.forks import Forks
from piloop.plugins.executor.kit.leases import SERVE
from piloop.plugins.executor.kit.remote import Remote
from piloop.plugins.executor.kit.tables import NoLease
from piloop.plugins.network.contract import NetworkHost
from piloop.plugins.network.https import Https
from piloop.plugins.secrets.contract import HomeSecrets
from piloop.plugins.secrets.local import LocalSecrets
from piloop.settings import DaemonSettings, Settings
from tests.conftest import Dial, disk
from tests.test_lease import HOST, PROBE
from tests.test_lease_client import secret


@pytest.fixture
def served(db: Database, cfg: Settings) -> Generator[Server]:
    """The API of ``cfg``'s home in one ``Served``, on a network listener: the daemon of term 1 is pid 4242."""
    with db.session() as s:
        term = terms.begin(s, 1, 4242, 'https://elsewhere:7000')
        s.commit()
    tokens = Tokens(db)
    network = Https.model_validate({'listen': '127.0.0.1:0'}).open(NetworkHost(tokens, HomeSecrets(db, LocalSecrets())))
    cas = disk(cfg.home)
    assert cas.put(b'piloop worker cas probe\n') == PROBE, 'the workers share it'
    api = Served(db, cfg, tokens, network, Tracer(None), cas, daemon_state(term))
    listener = listening(network)
    assert listener is not None
    server = Server(api.app(), listener)
    server.start()
    yield server
    server.close()
    api.close()


@pytest.mark.usefixtures('campaign')
def test_a_process_serves_the_api_and_the_workers_routes_from_the_rows_alone(
    served: Server, db: Database, campaign: Campaign, dial: Dial
) -> None:
    with dial(served.url, secret(db, 'laptop', Access.READ)) as http:
        status = http.get('/v1/status').json()
    assert status['daemon'] == {
        'pid': 4242,
        'epoch': status['daemon']['epoch'],
        'started': status['daemon']['started'],
        'listen': 'https://elsewhere:7000',
    }
    assert [c['name'] for c in status['campaigns']] == [campaign.name]
    with dial(served.url, secret(db, 'w1')) as http:
        worker = Remote(http, threading.Event(), Tracer(None))
        info = WorkerInfo(version='0.2.0', pool=SERVE, runners=['process'], sample=HOST, cas_probe=PROBE)
        assert worker.register(info).worker == 'w1'
        assert worker.acquire('r1', HOST) is NoLease.NOTHING


class NoWorkers(ExecutorConfig):
    """An executor whose jobs need no workers elsewhere."""

    def open(self, host: Host) -> Executor[Self]:
        raise AssertionError('a process that serves the API opens no executor')


def test_an_executor_without_workers_elsewhere_serves_no_routes(
    db: Database, cfg: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(EXECUTORS, 'selected', lambda _plugins: NoWorkers())
    with db.session() as s:
        term = terms.begin(s, 1, 4242, None)
        s.commit()
    tokens = Tokens(db)
    network = Https().open(NetworkHost(tokens, HomeSecrets(db, LocalSecrets())))
    api = Served(db, cfg, tokens, network, Tracer(None), disk(cfg.home), daemon_state(term))
    served = api.app()
    api.close()
    assert served.url_path_for('status') == '/v1/status'
    with pytest.raises(NoMatchFound):
        served.url_path_for('executor register')


def test_each_process_that_serves_runs_its_share_of_the_relayed_calls(
    db: Database, cfg: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``relayed_calls`` bounds the home: 3 API processes fork at most 8 at once between them."""
    sizes: list[int] = []

    class Counted(Forks):
        def __init__(self, home: Path, size: int) -> None:
            sizes.append(size)
            super().__init__(home, size)

    monkeypatch.setattr(workers_module, 'Forks', Counted)
    with db.session() as s:
        term = terms.begin(s, 1, 4242, None)
        s.commit()
    tokens = Tokens(db)
    network = Https().open(NetworkHost(tokens, HomeSecrets(db, LocalSecrets())))
    Served(db, cfg, tokens, network, Tracer(None), disk(cfg.home), daemon_state(term)).close()
    three = cfg.model_copy(update={'daemon': DaemonSettings(api_processes=3)})
    for process in range(3):
        Served(db, three, tokens, network, Tracer(None), disk(cfg.home), daemon_state(term), process).close()
    assert sizes == ([8, 3, 3, 2] if workers_module.FORKED else []), 'the daemon alone runs 8; three run 8 between them'
