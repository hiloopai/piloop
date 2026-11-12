"""Assemble and run the daemon: the database, the run controller and the scheduler."""

import os
import signal
import socket
import sys
import threading
from collections.abc import Callable
from functools import partial
from pathlib import Path
from types import FrameType
from typing import NoReturn

from sqlalchemy import select

from piloop.actor import System
from piloop.daemon.api import daemon_state
from piloop.daemon.holding import Holding
from piloop.daemon.rebuild import RebuildFailed, rebuild
from piloop.daemon.scheduler import Scheduler
from piloop.daemon.serve import (
    RESTART_SIGNAL,
    STOP_SECONDS,
    acquire,
    hand_on,
    open_files,
    running_pid,
    serve_argv,
)
from piloop.daemon.served import Served
from piloop.daemon.tier import CLOSE_SECONDS as API_CLOSE_SECONDS, ApiProcesses, NotServing
from piloop.db import terms
from piloop.db.ids import IdsExhausted
from piloop.db.models import TARGET_KINDS, Campaign
from piloop.db.schema import schemas
from piloop.db.session import (
    BackupFailed,
    Database,
    OlderDaemon,
    SchemaMismatch,
    WriteSpans,
    migrate,
    write_transaction,
)
from piloop.db.triggers.changelog import COMMENT_KINDS
from piloop.ids import CampaignStatus
from piloop.net.forward import Forwarding
from piloop.net.server import GRACE_SECONDS, Server, advertised, listening, local_listener, url
from piloop.net.tokens import Tokens
from piloop.otel import Span, tracer
from piloop.plugins import host as plugins
from piloop.plugins.archive.contract import ARCHIVES, CONSUMER, ArchiveHost
from piloop.plugins.campaign_type.kit.runs.controller import RunController
from piloop.plugins.cas import contract as cas
from piloop.plugins.contribution import CONTRIBUTORS
from piloop.plugins.executor.contract import EXECUTORS, JOB_KINDS
from piloop.plugins.leader.contract import LEADERS, Leader, LeaderHost, LostTerm, Term, Unreachable
from piloop.plugins.network import contract as network
from piloop.plugins.network.contract import Listener
from piloop.plugins.runtime.contract import make_runtime, runtimes
from piloop.plugins.secrets.contract import SECRETS, HomeSecrets
from piloop.proc import Inherited
from piloop.settings import CONFIG_FILE, Settings, settings
from piloop.version import code_digest

DECLARED = (JOB_KINDS, TARGET_KINDS, COMMENT_KINDS, CONTRIBUTORS)
"""The slots of what packages add that the daemon reads, which it checks before it starts."""
ACTOR = System.DAEMON
"""The actor that the daemon's connections record in ``updated_by``. The change-log triggers use this name."""
SHUTDOWN_SECONDS = STOP_SECONDS - max(GRACE_SECONDS, API_CLOSE_SECONDS) - 5
"""What the waits of a stop for the agents, the jobs and the lifecycles share. The close of the servers (their grace,
or the whole close of the API processes), the flush of the traces and the close of the database come after: the daemon
frees its lock within ``STOP_SECONDS``."""


def bound(cfg: Settings) -> list[plugins.Capability[plugins.Parameters]]:
    """The capabilities that the daemon uses: every one, but one of the home only when ``config.toml`` selects it.

    A broken package of a capability that nothing selects does not stop the daemon (``unbound_failures``).
    """

    def used(c: plugins.Capability[plugins.Parameters]) -> bool:
        match c.table:
            case plugins.One(scope=plugins.Scope.HOME, default=default):
                return default is not None or c.name in cfg.plugins
            case _:
                return True

    return [c for c in plugins.declared() if used(c)]


def unbound_failures(cfg: Settings) -> dict[plugins.Capability[plugins.Parameters], str]:
    """The capabilities that the daemon does not use and that do not load, with why. ``piloop doctor`` warns."""
    failed: dict[plugins.Capability[plugins.Parameters], str] = {}
    for capability in plugins.declared():
        if capability not in bound(cfg):
            try:
                plugins.compose(capability)
            except plugins.PluginError as e:
                failed[capability] = str(e)
    return failed


def preflight(cfg: Settings, listen: network.Address | None = None) -> network.NetworkConfig:
    """Compose the capabilities that the daemon uses (``bound``), check what ``config.toml`` selects, return the network.

    The network has ``listen`` (``piloop serve --listen``) in place of its own.

    ``piloop serve --check`` runs this, and ``piloop restart`` and ``piloop upgrade`` run it before they restart the
    daemon. ``piloop.toml`` is checked when it loads (``piloop init``).

    Raises:
        PluginError: A provider, a schema (``schemas``) or another declaration (``DECLARED``) does not load, or
            ``config.toml``
            selects a provider that there is not, or one that refuses its parameters (``[plugins]``),
            or the network refuses ``listen``.
    """
    plugins.compose(*bound(cfg))
    schemas()
    for slot in DECLARED:
        slot.loaded()
    try:
        runtimes(cfg)
        if (why := LEADERS.selected(cfg.providers).refuses_cas(cas.CAS.selected(cfg.providers))) is not None:
            raise ValueError(why)
        return network.NETWORKS.selected(cfg.providers).listening(listen)
    except ValueError as e:
        raise plugins.PluginError(f'{cfg.home / CONFIG_FILE}: {e}') from e


class Daemon:
    """Everything that a ``piloop serve`` process owns."""

    def __init__(
        self, cfg: Settings = settings, inherited: Inherited | None = None, listen: network.Address | None = None
    ) -> None:
        """``listen``: where the server listens (``piloop serve --listen``), in place of ``[plugins.network] listen``."""
        try:
            served = preflight(cfg, listen)
        except plugins.PluginError as e:
            raise SystemExit(f'the daemon does not start: {e}') from e
        self._cfg = cfg
        self._lock = acquire(cfg)
        """The lock of the home's only daemon, taken before the migration: another daemon may run an older schema."""
        self._holding: Holding | None = None
        self._listeners: list[Listener] = []
        """The home's Unix socket, then the network's listener when it has one: bound before the take, and the
        servers' once the daemon serves (``_listen``)."""
        self._forwarding: Forwarding | None = None
        try:
            self._inherited = inherited or Inherited(set())
            """The children an in-place restart handed over (``piloop.proc.Inherited``): reaped as each one exits."""
            self.tracer = tracer()
            leader = LEADERS.selected(cfg.providers)
            self._leader: Leader = leader.open(LeaderHost(cfg.home, cfg.database))
            measures = self.tracer.measures
            self.db = Database(
                cfg.database_url,
                actor=ACTOR,
                writes=None if measures is None else WriteSpans(measures, cfg.tracing.slow_write),
                strict=cfg.debug or None,
            )
            """Nothing connects before the restore, which replaces its file: the network only keeps it, and a standby
            reads nothing of it."""
            self._argv = serve_argv(listen)
            """What an in-place restart execs."""
            self._tokens = Tokens(self.db, cfg.daemon.token_trust.total_seconds())
            self.network = served.open(
                network.NetworkHost(self._tokens, HomeSecrets(self.db, SECRETS.selected(cfg.providers)))
            )
            remote = self._bind(served.listen)
            self._forwarding = Forwarding(self._leader.current, self._listeners, cfg.daemon)
            """What serves the sockets until the daemon serves: a standby forwards each request to the holder."""
            self._forwarding.start()
            self._term = self._take(cfg.daemon.advertise_url or (None if remote is None else advertised(remote)))
            """The term that lets this daemon write the home. A daemon that does not hold one waits in ``_take``."""
            self._forwarding.hold(self._term.epoch)
            self._lost: str | None = None
            """Why the daemon gives its term up, once it does: its renewals failed, or its replica never went whole."""
            self._stop_on_loss: Callable[[], None] = lambda: None
            """What giving the term up stops: nothing while the daemon starts (it checks ``_lost`` before it serves),
            then the scheduler."""
            renewal = self._leader.renewal
            self._holding = (
                None
                if renewal is None or self._term.until is None
                else Holding(self._leader, self._term, renewal, self._lost_term)
            )
            """What renews a term that ends, from its take on, and ends the process at its end: a restore and a
            migration may take longer than the term."""
            if self._holding is not None:
                self._holding.start()
            try:  # before anything opens the database: a restore replaces its file
                self._restored = self._leader.restore(self._term)
                """Whether the home's database came from the term before's replica: its files come from the CAS."""
            except LostTerm as e:  # it never served: it leaves the term to the newer daemon
                raise SystemExit(f'the daemon does not start: {e}') from e
            self._servers: list[Server | ApiProcesses] = []
            self._served: Served | None = None
            """What the daemon's own servers serve the API with, once they serve. None while API processes serve it."""
            self._cas = cas.CAS.selected(cfg.providers).open(cfg.home)
            try:  # after the CAS opens: a revision may move data into it
                self._backup = migrate(
                    self.db.engine, cas=self._cas, backups=cfg.migration_backups, daemon=running_pid(cfg)
                )
            except OlderDaemon, SchemaMismatch, BackupFailed:
                self.db.close()
                raise
            with self.db.session() as s:
                live = s.scalars(select(Campaign).where(Campaign.status != CampaignStatus.RETIRED)).all()
                refused = [why for c in live if (why := leader.refuses(cfg.home, Path(c.path))) is not None]
            if refused:
                self._refuse('; '.join(refused))
            with self.db.session() as s:
                write_transaction(s)
                try:
                    self._leader.begin(s, self._term)
                except IdsExhausted as e:
                    raise SystemExit(f'the daemon does not start: {e}') from e
                self._began = terms.begin(s, self._term.epoch, os.getpid(), None if remote is None else url(remote))
                """This daemon's term: what every process that serves its API reads of it (``piloop.db.terms``)."""
                s.commit()
            if self._restored:
                try:
                    rebuild(self.db, self._cas, cfg.home, SECRETS.selected(cfg.providers), self.tracer)
                except RebuildFailed as e:
                    self._refuse(str(e))
            self._replicating = self._leader.replicate(self._term, self._unreplicated)
            """Ships the database for the term's life: it starts before the daemon serves, and stops after its last
            write."""
            self._replicating.__enter__()
            opened = partial(make_runtime, cfg=cfg)
            self.controller = RunController(
                self.db,
                self.tracer,
                cfg.home,
                executor=EXECUTORS.selected(cfg.providers),
                cas=self._cas,
                reserve=cfg.storage.reserve,
                evaluation=cfg.evaluation,
                agents=cfg.agents,
                runtime_factory=opened,
                network=self.network,
                secrets=SECRETS.selected(cfg.providers),
                wake=self._wake,
            )
            archiver = ARCHIVES.selected(cfg.providers).open(
                ArchiveHost(self.db, self._cas, cfg.home, cfg.storage.reserve, self.tracer, self._wake_archiver)
            )
            self.scheduler = Scheduler(
                self.db,
                self.controller,
                self.tracer,
                cfg.home,
                archiver=archiver,
                runtime_factory=opened,
                daemon=cfg.daemon,
                agents=cfg.agents,
                plugins=cfg.providers,
                defaults=cfg.campaign_defaults,
            )
            self._stop_on_loss = self.scheduler.interrupt
            if self._lost is not None:  # the renewals failed while it started: it stops before it serves
                raise SystemExit(f'the daemon does not start: {self._lost}')
            self._restart = False
        except BaseException:  # no file names a daemon that never served
            if self._holding is not None:  # nor does a daemon that never served keep renewing, or exit at the end
                self._holding.stop()
            self._unbind()
            cfg.socket_file.unlink(missing_ok=True)
            self._release()
            raise

    def serve(self) -> None:
        """Reap, then run the loop until Ctrl-C or SIGTERM. The constructor took the lock.

        On ``RESTART_SIGNAL``, stop the same way, then exec ``piloop serve``. The exec keeps the pid, the environment,
        the lock and the service manager's hold on the process. So ``piloop upgrade`` and ``piloop restart`` start the installed
        code under whatever started the daemon: systemd, launchd or a terminal.
        """
        self._cfg.code_file.write_text(code_digest())
        previous = signal.signal(signal.SIGTERM, self._on_sigterm)
        previous_restart = signal.signal(RESTART_SIGNAL, self._on_restart)
        try:
            with self.tracer.span(
                'piloop.start',
                db=self._cfg.database_url,
                backup=None if self._backup is None else str(self._backup),
            ) as span:
                soft, hard = open_files()
                span.set(open_files=soft, open_files_hard=hard)
                self._listen(span)
                self.scheduler.reap()
                self._inherited.watch()
            self.scheduler.serve()
        except KeyboardInterrupt:
            pass
        finally:
            try:
                self.shutdown()
            finally:
                signal.signal(signal.SIGTERM, previous)
                signal.signal(RESTART_SIGNAL, previous_restart)
                if self._restart:
                    # Exec even after a failed shutdown. Otherwise its run threads keep this process, the old code
                    # and the lock alive. The reap in the new code fails the runs that this one could not stop.
                    # The lock goes on to the new code: no other daemon starts in between.
                    hand_on(self._lock)
                    sys.stdout.flush()  # exec discards Python's buffers.
                    sys.stderr.flush()
                    os.execv(self._argv[0], self._argv)
        if self._lost is not None:  # a failure: a service manager starts it again, with a new term or as a standby
            raise SystemExit(f'the daemon gave its term up: {self._lost}')

    def _bind(self, address: network.Address | None) -> Listener | None:
        """Bind the home's Unix socket, and the network's listener when it has one (``_listeners``), which it returns.

        The daemon binds them before it takes the term, so that a standby serves them (``Forwarding``). An in-place
        restart listens on the port that port 0 took, so a worker that has the URL keeps its server.
        """
        try:
            self._listeners.append(local_listener(self._cfg.socket_file))
        except OSError as e:
            raise SystemExit(f'the daemon does not listen on {self._cfg.socket_file}: {e}') from e
        try:
            remote = listening(self.network)
        except OSError as e:
            raise SystemExit(f'the daemon does not listen: {e}') from e
        if remote is None:
            return None
        self._listeners.append(remote)
        assert address is not None  # a network binds nothing without ``listen``
        if address.port == 0:
            port = remote.socket.getsockname()[1]
            self._argv = serve_argv(address.model_copy(update={'port': port}))
        return remote

    def _unbind(self) -> None:
        """Stop the standby's forwarding, and close the sockets that no server took."""
        if self._forwarding is not None:
            self._forwarding.close()
        for each in self._listeners:
            each.socket.close()
        self._listeners = []

    def _listen(self, span: Span) -> None:
        """Serve the API on the sockets that the daemon bound, in place of the standby's forwarding.

        The daemon serves it itself, or its API processes do (``[daemon] api_processes``). The network's URL goes on
        the start span.
        """
        assert self._forwarding is not None
        self._forwarding.close()  # its servers end; the sockets stay bound, and the next connections wait for these
        listeners, self._listeners = self._listeners, []  # the servers own them from here: they close them
        if self._cfg.daemon.api_processes > 0:
            local, *remote = (each.socket for each in listeners)
            processes = ApiProcesses(self._cfg, [local, remote[0] if remote else None], self.tracer, self._api_failed)
            try:
                processes.start()
            except NotServing as e:
                raise SystemExit(f'the daemon does not start: {e}') from e
            self._servers = [processes]
        else:
            self._served = Served(
                self.db,
                self._cfg,
                self._tokens,
                self.network,
                self.tracer,
                self.controller.cas,
                daemon_state(self._began),
            )
            self._servers = [Server(self._served.app(), each) for each in listeners]
            for server in self._servers:
                server.start()
        if self._began.listen is not None:
            span.set(listen=self._began.listen)

    def _take(self, advertise: str | None) -> Term:
        """Take the home's term, waiting as a standby while another daemon holds it, until SIGTERM or Ctrl-C.

        The term names ``advertise``: where the standbys of other hosts forward to.
        """
        stop = threading.Event()
        handlers: dict[int, Callable[[int, FrameType | None], object] | int | None] = {}
        if threading.current_thread() is threading.main_thread():  # only the main thread can handle signals
            handlers = {sig: signal.signal(sig, lambda *_: stop.set()) for sig in (signal.SIGTERM, signal.SIGINT)}
        try:
            term = self._leader.take(f'{socket.gethostname()}/{os.getpid()}', advertise, stop)
        finally:
            for sig, handler in handlers.items():
                signal.signal(sig, handler)
        if term is None:
            raise SystemExit('the daemon stopped while it waited, as a standby, for its term')
        return term

    def _refuse(self, why: str) -> NoReturn:
        """Give the term back and do not start.

        The renewals stop first: one after the release would find the term no longer this daemon's, and end the process
        at once (``Holding``), without saying why it did not start.
        """
        if self._holding is not None:
            self._holding.stop()
        self._leader.release(self._term)
        raise SystemExit(f'the daemon does not start: {why}')

    def _lost_term(self, why: str) -> None:
        """The term was not renewed in time: the daemon stops, as on SIGTERM, before the term ends."""
        self._give_up('term.lost', why)

    def _unreplicated(self, why: str) -> None:
        """The term's replica did not hold the whole database in time: the daemon stops, as on a lost term."""
        self._give_up('term.unreplicated', why)

    def _give_up(self, event: str, why: str) -> None:
        self.tracer.event(event, error=why)
        self._lost = why
        self._stop_on_loss()

    def _release_term(self) -> None:
        """End the term after the daemon's last write, so that another daemon may take the next.

        The replica ships the rest of the database first, and the renewals stop.
        """
        self._replicating.__exit__(None, None, None)
        if self._holding is not None:
            self._holding.stop()
        try:
            self._leader.release(self._holding.term if self._holding is not None else self._term)
        except (LostTerm, Unreachable) as e:  # the next daemon waits for the term's end instead
            self.tracer.event('term.not_released', error=str(e))

    def _wake(self, consumer: str) -> None:
        """Run ``consumer`` in the next turn of the loop: a thread of a provider that the daemon opened ended."""
        self.scheduler.loop.wake(consumer)

    def _api_failed(self, why: str) -> None:
        """API processes started again keep failing to serve: the daemon stops, as on SIGTERM, and says why."""
        self.tracer.event('api_processes.failed', error=why)
        self.scheduler.interrupt()

    def _wake_archiver(self) -> None:
        """The archiver's job ended: the next turn of the loop starts the next one."""
        self.scheduler.loop.wake(CONSUMER)

    def _on_sigterm(self, signum: int, frame: FrameType | None) -> None:
        """Treat a service manager's stop as Ctrl-C: the loop ends and ``shutdown`` kills the running agents."""
        self.scheduler.interrupt()

    def _on_restart(self, signum: int, frame: FrameType | None) -> None:
        self._restart = True
        self.scheduler.interrupt()

    def shutdown(self, timeout: float = SHUTDOWN_SECONDS) -> None:
        """Stop the scheduler and the servers, and flush traces. A restart hands the agents over. A stop kills them.

        The scheduler's waits share ``timeout``. Then the servers, or the API processes, let their requests in flight
        finish, all together.
        """
        with self.tracer.span('piloop.shutdown'):
            (self.scheduler.hand_over if self._restart else self.scheduler.stop)(timeout)
            for server in self._servers:
                server.stop()
            for server in self._servers:
                server.close()
            self._unbind()  # what a daemon that stopped before it served still holds
            self._cfg.socket_file.unlink(missing_ok=True)
            if self._served is not None:
                self._served.close()
        self.controller.close()
        self.db.close()
        self._release_term()
        self.tracer.shutdown()
        plugins.close()
        # a stop that failed keeps the lock while its run threads may live on; a restart hands it to the next program
        if not self._restart:
            self._release()

    def _release(self) -> None:
        """Stop being the home's daemon: remove its files, then free the lock, so that no file names another's.

        The files are only for tidiness: the lock, not the file, tells which daemon runs.
        """
        self._cfg.pid_file.unlink(missing_ok=True)
        self._cfg.code_file.unlink(missing_ok=True)
        os.close(self._lock)
