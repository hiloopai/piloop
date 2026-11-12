"""The daemon's hold on a term that ends (``Term.until``): renew it, stop when it cannot, exit at its end.

The daemon exits at once at the term's end, whatever its stop is doing then.
A graceful stop goes on writing for as long as it takes (``STOP_SECONDS``), so it cannot be what ends the writes at
``until``: another daemon may take the term just after. So the daemon begins to stop at the renewal deadline, and
the watch ends the process at ``until`` (``os._exit``). Its children and its lock go with it, and the next daemon's
reap fails the runs that it left. A renewal that another daemon refused (``LostTerm``) ends it at once too.

The model is ``tests/models/term.tla``: ``Renew``, ``Answer`` and ``SelfStop``.
"""

import os
import sys
import threading
import time
from collections.abc import Callable
from datetime import datetime

from piloop.db.models import utcnow
from piloop.plugins.leader.contract import Leader, LostTerm, Renewal, Term, Unreachable

ENDED_EXIT = 75
"""The exit status of a daemon whose term ended (``EX_TEMPFAIL``): a service manager starts it again, as a standby."""


def deadline(term: Term, mono: float, wall: datetime) -> float:
    """When ``term`` ends on the monotonic clock ``mono``, read with the wall clock ``wall`` just as it was answered.

    The provider set ``until`` from the wall clock when it sent the write, so this counts from the send: a slow answer
    shortens the time the holder writes, and never lengthens it past the ``until`` that the others read.
    """
    assert term.until is not None, 'a term that never ends has no deadline'
    return mono + (term.until - wall).total_seconds()


def _exit(why: str) -> None:
    print(f'piloop: {why}; exiting at once', file=sys.stderr, flush=True)
    os._exit(ENDED_EXIT)


class Holding:
    """Renew a term, say when it is lost, and exit at its end.

    It renews ``term`` every ``renewal.every``, calls ``lost`` past ``renewal.deadline`` without a renewal, and calls
    ``exit`` at the term's end, or when another daemon took a newer one.
    """

    def __init__(
        self,
        leader: Leader,
        term: Term,
        renewal: Renewal,
        lost: Callable[[str], None],
        *,
        exit: Callable[[str], None] | None = None,
        mono: Callable[[], float] = time.monotonic,
        wall: Callable[[], datetime] = utcnow,
    ) -> None:
        self.term = term
        self._leader, self._renewal, self._lost = leader, renewal, lost
        self._exit = exit if exit is not None else _exit
        """Ends the process (``_exit``), read when the hold begins: a test of a daemon in its own process swaps it."""
        self._mono, self._wall = mono, wall
        now = mono()
        self.ends = deadline(term, now, wall())
        """When the term ends, on the monotonic clock: the watch exits then."""
        self._give_up = now + renewal.deadline.total_seconds()
        self._stop = threading.Event()
        self._threads = [
            threading.Thread(target=self._renewing, name='term-renewal', daemon=True),
            threading.Thread(target=self._watching, name='term-watch', daemon=True),
        ]

    def start(self) -> None:
        for thread in self._threads:
            thread.start()

    def stop(self) -> None:
        """Renew no more, and watch no more: the daemon wrote its last, and releases the term next."""
        self._stop.set()
        for thread in self._threads:
            thread.join()

    def _watching(self) -> None:
        while not self._stop.wait(max(0.0, self.ends - self._mono())):
            if self._mono() >= self.ends:
                self._exit(f'term {self.term.epoch} ended')
                return

    def _renewing(self) -> None:
        every = self._renewal.every.total_seconds()
        given_up = False
        while not self._stop.wait(every):
            sent = self._mono()
            try:
                self.renew_once()
            except LostTerm as e:
                self._exit(str(e))
                return
            except Unreachable as e:
                if not given_up and self._mono() >= self._give_up:
                    given_up = True
                    self._lost(f'term {self.term.epoch} was not renewed for {self._renewal.deadline}: {e}')
                continue
            self._give_up = sent + self._renewal.deadline.total_seconds()

    def renew_once(self) -> None:
        """Renew the term now, and move its end.

        Raises:
            LostTerm: Another daemon took a newer term.
            Unreachable: The record could not be written now.
        """
        term = self._leader.renew(self.term)
        self.term, self.ends = term, deadline(term, self._mono(), self._wall())
