"""What one process serves the API with: core's routes, and those of each provider that serves (``Serves``).

The daemon builds it to serve the API itself, and each process that serves the API beside it builds its own
(``piloop.daemon.tier``). Nothing in it reads the daemon's memory: every route reads the database, and the term row says
which daemon answers (``piloop.db.terms``).
"""

import socket
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from functools import partial
from pathlib import Path

from fastapi import FastAPI

from piloop.actor import System
from piloop.agents.results import CampaignResults
from piloop.api.views import DaemonState
from piloop.campaign.campaigns import checkout_of, record_of
from piloop.daemon.api import ROUTERS, Handlers, daemon_state, with_api
from piloop.db import terms
from piloop.db.session import Database, WriteSpans
from piloop.net.server import App, Server, app
from piloop.net.tokens import Tokens
from piloop.otel import Tracer, tracer
from piloop.plugins.cas.contract import CAS, Cas
from piloop.plugins.contribution import RouteHost, routes_of
from piloop.plugins.network.contract import NETWORKS, Listener, Network, NetworkHost
from piloop.plugins.secrets.contract import SECRETS, HomeSecrets
from piloop.settings import Settings, settings


class Served:
    """The API of a home, as one process serves it: one ``app`` for each of its servers, until ``close``."""

    def __init__(
        self,
        db: Database,
        cfg: Settings,
        tokens: Tokens,
        network: Network,
        tracer: Tracer,
        cas: Cas,
        daemon: DaemonState,
        process: int = 0,
    ) -> None:
        """``daemon``: the daemon that answers, of the current term.

        ``process``: which of the processes that serve the API this one is (``[daemon] api_processes``), 0 for the
        daemon itself.
        """
        secrets = SECRETS.selected(cfg.providers)
        results = CampaignResults(
            db,
            tracer,
            cfg.home,
            cas,
            secrets=secrets,
            exit_output_chars=cfg.agents.exit_output_chars,
            startup_seconds=cfg.agents.startup_seconds,
            startup_turns=cfg.agents.startup_turns,
        )
        host = RouteHost(
            db,
            results,
            tracer,
            cfg.home,
            cas,
            partial(checkout_of, db),
            partial(record_of, db),
            secrets,
            max(1, cfg.daemon.api_processes),
            process,
        )
        self._routes = routes_of(host, cfg.providers)
        self._tokens, self._network = tokens, network
        self._handlers = Handlers(db, cfg, daemon, cas)

    def app(self) -> App:
        """An app of the API: each server has its own, since a server's shutdown ends the long-polls of its app."""
        return with_api(app(self._tokens, self._network, [r.router for r in self._routes]), self._handlers)

    def close(self) -> None:
        """Release what the providers' routes hold, once no server serves them."""
        for routes in self._routes:
            routes.close()


def openapi() -> dict[str, object]:
    """The OpenAPI document of the API (``ROUTERS``) and of each provider that serves by default (``routes_of``).

    It is what ``GET /v1/openapi.json`` answers, and ``docs/reference/openapi.json``. The routes are built over a home
    that is never opened: they read nothing to be described.
    """
    with tempfile.TemporaryDirectory(prefix='piloop-openapi-') as scratch:
        home = Path(scratch)
        db = Database('sqlite://', actor=System.DAEMON)
        tracing, cas, secrets = Tracer(None), CAS.selected({}).open(home), SECRETS.selected({})
        results = CampaignResults(db, tracing, home, cas, secrets=secrets)
        routes = routes_of(
            RouteHost(db, results, tracing, home, cas, partial(checkout_of, db), partial(record_of, db), secrets, 1, 0),
            {},
        )
        described = FastAPI(title='piloop', version='1')
        try:
            for router in (*ROUTERS, *(r.router for r in routes)):
                described.include_router(router)
            return described.openapi()
        finally:
            for each in routes:
                each.close()
            db.close()


@contextmanager
def serving(
    local: socket.socket, remote: socket.socket | None, process: int, cfg: Settings = settings
) -> Generator[None]:
    """Serve the API of ``cfg``'s home for the block, as an API process (``piloop.daemon.tier``).

    ``local`` is the home's Unix socket, and ``remote`` the network's or None: the daemon bound them. ``process`` is
    which API process this is. It opens the home as a client does, and the current term says which daemon answers.
    The block runs once every server serves.

    Raises:
        SystemExit: No daemon began a term on the home: nothing here would answer for it.
    """
    tracing = tracer()
    measures = tracing.measures
    db = Database(
        cfg.database_url,
        actor=System.DAEMON,
        writes=None if measures is None else WriteSpans(measures, cfg.tracing.slow_write),
        strict=cfg.debug or None,
    )
    try:
        with db.session() as s:
            term = terms.current(s)
        if term is None:
            raise SystemExit(f'no daemon began a term on {cfg.home}: an API process serves only beside one')
        tokens = Tokens(db, cfg.daemon.token_trust.total_seconds())
        secrets = SECRETS.selected(cfg.providers)
        network = NETWORKS.selected(cfg.providers).open(NetworkHost(tokens, HomeSecrets(db, secrets)))
        cas = CAS.selected(cfg.providers).open(cfg.home)
        api = Served(db, cfg, tokens, network, tracing, cas, daemon_state(term), process)
        listeners = [Listener(local, None), *([] if remote is None else [network.listener(remote)])]
        servers = [Server(api.app(), each) for each in listeners]
        for server in servers:
            server.start()
        try:
            for server in servers:
                server.serving(cfg.daemon.api_start.total_seconds())
            yield
        finally:
            for server in servers:
                server.stop()
            for server in servers:
                server.close()
            api.close()
    finally:
        db.close()
        tracing.shutdown()
