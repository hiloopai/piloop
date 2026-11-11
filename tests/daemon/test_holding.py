"""The daemon's hold on a term that ends: renewals, the deadline, and the exit at the term's end."""

import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

from piloop.daemon import holding as module
from piloop.daemon.holding import ENDED_EXIT, Holding, deadline
from piloop.db.models import utcnow
from piloop.plugins.leader.contract import LostTerm, Renewal, Term, Unreachable

WAIT = 10
"""How long a test waits for a thread: only a bug makes it wait that long."""


@dataclass
class Fake:
    """A leader whose renewals do what the test says: a term ``ttl`` from now, an error, or a refusal."""

    ttl: timedelta
    fails: type[Exception] | None = None
    renewals: int = 0
    hang: threading.Event | None = None
    """A renewal waits for it, as a call that hangs."""

    @property
    def renewal(self) -> Renewal | None:
        return None

    def take(self, holder: str, url: str | None, stop: threading.Event) -> Term | None:
        return None

    def renew(self, term: Term) -> Term:
        self.renewals += 1
        if self.hang is not None:
            self.hang.wait(WAIT)
        if self.fails is not None:
            raise self.fails('as the test says')
        return term.model_copy(update={'until': utcnow() + self.ttl})

    def release(self, term: Term) -> None:
        pass

    def current(self) -> Term | None:
        return None


@dataclass
class Calls:
    lost: list[str] = field(default_factory=list)
    exits: list[str] = field(default_factory=list)
    exited: threading.Event = field(default_factory=threading.Event)

    def exit(self, why: str) -> None:
        self.exits.append(why)
        self.exited.set()


def holding(leader: Fake, ttl: timedelta, calls: Calls, every: float = 0.02, give_up: float = 0.1) -> Holding:
    term = Term(epoch=3, holder='a/1', until=utcnow() + ttl)
    renewal = Renewal(timedelta(seconds=every), timedelta(seconds=give_up))
    return Holding(leader, term, renewal, calls.lost.append, exit=calls.exit)


def test_the_end_counts_from_the_writes_send_so_a_slow_answer_shortens_it() -> None:
    sent = datetime(2026, 11, 11, tzinfo=UTC)
    term = Term(epoch=1, holder='a/1', until=sent + timedelta(seconds=15))
    assert deadline(term, mono=100.0, wall=sent) == 115.0
    assert deadline(term, mono=100.0, wall=sent + timedelta(seconds=4)) == 111.0, 'answered 4 s after its send'


def test_renewals_keep_the_term_and_a_stop_ends_them() -> None:
    leader, calls = Fake(timedelta(seconds=0.3)), Calls()
    hold = holding(leader, timedelta(seconds=0.3), calls)
    first = hold.ends
    hold.start()
    time.sleep(0.5)  # past the first term's end: only renewals keep it
    hold.stop()
    assert leader.renewals >= 3 and hold.ends > first
    assert (calls.lost, calls.exits) == ([], [])


def test_a_refused_renewal_exits_at_once() -> None:
    leader, calls = Fake(timedelta(seconds=30), LostTerm), Calls()
    hold = holding(leader, timedelta(seconds=30), calls)
    hold.start()
    assert calls.exited.wait(WAIT)
    hold.stop()
    assert calls.exits == ['as the test says'] and leader.renewals == 1 and calls.lost == []


def test_renewals_that_fail_past_the_deadline_begin_the_stop_then_the_end_exits() -> None:
    leader, calls = Fake(timedelta(seconds=30), Unreachable), Calls()
    hold = holding(leader, timedelta(seconds=0.4), calls)
    hold.start()
    assert calls.exited.wait(WAIT)
    hold.stop()
    assert len(calls.lost) == 1 and calls.lost[0].startswith('term 3 was not renewed for'), 'once'
    assert calls.exits == ['term 3 ended'] and leader.renewals >= 3


def test_a_renewal_that_hangs_does_not_outlast_the_term() -> None:
    """The watch is its own thread: a renewal stuck in its call cannot keep the daemon writing past the end."""
    release = threading.Event()
    leader, calls = Fake(timedelta(seconds=30), hang=release), Calls()
    hold = holding(leader, timedelta(seconds=0.2), calls)
    hold.start()
    assert calls.exited.wait(WAIT) and calls.exits == ['term 3 ended']
    release.set()
    hold.stop()


def test_the_exit_at_a_terms_end_says_why_and_ends_the_process_at_once(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``os._exit``: no handler, no ``finally`` and no shutdown runs, so nothing writes after the term's end."""
    codes: list[int] = []
    monkeypatch.setattr(module.os, '_exit', codes.append)
    module._exit('term 3 ended')
    assert (codes, capsys.readouterr().err) == ([ENDED_EXIT], 'piloop: term 3 ended; exiting at once\n')
