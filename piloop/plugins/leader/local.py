"""``local``: the default leader provider, for a home that one host serves.

The pid file's lock (``piloop.daemon.serve.acquire``) makes a daemon the home's only one before it takes a term, so a
take never waits. Its epoch is one above the newest of the home's terms (``piloop.db.terms``). A term never ends.
"""

import socket
import threading
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path

from sqlalchemy.orm import Session

from piloop.db import terms
from piloop.plugins.leader.contract import LeaderConfig, LeaderHost, Renewal, Term


class LocalLeader(LeaderConfig):
    """``[plugins.leader] provider = "local"``: one host, whose lock is the term."""

    def open(self, host: LeaderHost) -> LocalTerms:
        return LocalTerms(host.database)


class LocalTerms:
    """The home's terms, from its database."""

    renewal: Renewal | None = None

    def __init__(self, database: Path) -> None:
        self._database = database

    def take(self, holder: str, url: str | None, stop: threading.Event) -> Term:
        return Term(epoch=terms.newest_in(self._database) + 1, holder=holder, url=url)

    def renew(self, term: Term) -> Term:
        return term

    def release(self, term: Term) -> None:
        pass

    def restore(self, term: Term) -> bool:
        return False  # the home is on this host's disk, as the term before left it

    def replicate(self, term: Term, unreplicated: Callable[[str], None]) -> AbstractContextManager[None]:
        return nullcontext()

    def begin(self, s: Session, term: Term) -> None:
        pass  # one host, whose database is never restored under it: its ids stay small

    def here(self) -> bool:
        return True  # the home has one host: its database is the home's, whether its daemon runs or not

    def current(self) -> Term | None:
        newest = terms.running_in(self._database)
        if newest is None:
            return None
        epoch, pid, listen = newest
        return Term(epoch=epoch, holder=f'{socket.gethostname()}/{pid}', url=listen)
