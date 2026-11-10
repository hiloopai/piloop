"""What one process serves the API with: core's routes, those of each installed provider, and the executor's.

The daemon builds it to serve the API itself, and each process that serves the API beside it builds its own
(``piloop.daemon.tier``). Nothing in it reads the daemon's memory: every route reads the database, and the term row says
which daemon answers (``piloop.db.terms``).
"""

from functools import partial

from piloop.agents.results import CampaignResults
from piloop.api.views import DaemonState
from piloop.campaign.campaigns import checkout_of, record_of
from piloop.daemon.api import Handlers, with_api
from piloop.db.session import Database
from piloop.net.server import App, app
from piloop.net.tokens import Tokens
from piloop.otel import Tracer
from piloop.plugins.cas.contract import Cas
from piloop.plugins.contribution import provided_routers
from piloop.plugins.executor.contract import EXECUTORS, RouteHost
from piloop.plugins.network.contract import Network
from piloop.plugins.secrets.contract import SECRETS
from piloop.settings import Settings


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
    ) -> None:
        """``daemon``: the daemon that answers, of the current term."""
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
        host = RouteHost(db, results, tracer, cfg.home, cas, partial(checkout_of, db), partial(record_of, db), secrets)
        self._executor = EXECUTORS.selected(cfg.providers).routes(host)
        self._tokens, self._network = tokens, network
        self._handlers = Handlers(db, cfg, daemon, cas)
        executor = () if self._executor is None else (self._executor.router,)
        self._routers = (*executor, *provided_routers())

    def app(self) -> App:
        """An app of the API: each server has its own, since a server's shutdown ends the long-polls of its app."""
        return with_api(app(self._tokens, self._network, self._routers), self._handlers)

    def close(self) -> None:
        """Release what the executor's routes hold, once no server serves them."""
        if self._executor is not None:
            self._executor.close()
