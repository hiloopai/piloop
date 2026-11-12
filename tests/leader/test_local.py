"""The ``local`` leader: a term at once, one above the home's newest, which never ends."""

import os
import socket
import threading

from piloop.db import terms
from piloop.db.session import Database
from piloop.plugins.leader.contract import Term
from piloop.plugins.leader.local import LocalLeader
from tests.conftest import leader_host


def test_a_take_is_one_above_the_newest_term_and_never_ends(db: Database) -> None:
    leader = LocalLeader().open(leader_host(db))
    assert leader.renewal is None and leader.current() is None
    first = leader.take('a/1', None, threading.Event())
    assert first == Term(epoch=1, holder='a/1')
    assert leader.renew(first) == first
    with db.session() as s:
        terms.begin(s, first.epoch, os.getpid(), 'https://a:7000')
        s.commit()
    assert leader.current() == Term(epoch=1, holder=f'{socket.gethostname()}/{os.getpid()}', url='https://a:7000')
    leader.release(first)
    with db.session() as s:
        terms.stop(s)
        s.commit()
    assert leader.current() is None, 'its daemon stops'
    assert leader.take('a/2', None, threading.Event()) == Term(epoch=2, holder='a/2')
