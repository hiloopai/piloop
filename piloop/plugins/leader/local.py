"""``local``: the default leader provider, for a home that one host serves.

The pid file's lock (``piloop.daemon.serve.acquire``) makes a daemon the home's only one before it takes a term, so a
take never waits. Its epoch is one above the newest of the home's terms (``piloop.db.terms``). A term never ends.
"""

import socket
import threading

from piloop.db import terms
from piloop.db.session import Database
from piloop.plugins.leader.contract import LeaderConfig, LeaderHost, Renewal, Term


class LocalLeader(LeaderConfig):
    """``[plugins.leader] provider = "local"``: one host, whose lock is the term."""

    def open(self, host: LeaderHost) -> LocalTerms:
        return LocalTerms(host.db)


class LocalTerms:
    """The home's terms, from its database."""

    renewal: Renewal | None = None

    def __init__(self, db: Database) -> None:
        self._db = db

    def take(self, holder: str, url: str | None, stop: threading.Event) -> Term:
        with self._db.session() as s:
            return Term(epoch=terms.newest(s) + 1, holder=holder, url=url)

    def renew(self, term: Term) -> Term:
        return term

    def release(self, term: Term) -> None:
        pass

    def current(self) -> Term | None:
        with self._db.session() as s:
            newest = terms.current(s)
        if newest is None or newest.stopping_at is not None:
            return None
        return Term(epoch=newest.epoch, holder=f'{socket.gethostname()}/{newest.pid}', url=newest.listen)
