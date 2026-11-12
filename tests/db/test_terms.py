import sqlite3
from pathlib import Path

from piloop.db import terms
from piloop.db.session import Database


def test_each_term_takes_the_next_epoch_and_a_stop_marks_only_the_current_one_once(db: Database) -> None:
    with db.session() as s:
        assert (terms.current(s), terms.stopping(s)) == (None, False), 'before any daemon served'
        terms.stop(s)  # nothing to stop
        first = terms.begin(s, terms.newest(s) + 1, 10, None)
        terms.stop(s)
        s.commit()
        s.expire_all()
        stopped_at = first.stopping_at
        assert stopped_at is not None
        second = terms.begin(s, terms.newest(s) + 1, 11, 'https://h:1')
        s.commit()
        assert (first.epoch, second.epoch) == (1, 2)
        assert terms.stopping(s) is False, 'the new term serves'
        terms.stop(s)
        terms.stop(s)
        s.commit()
        s.expire_all()
        current = terms.current(s)
        assert current is not None
        assert (current.epoch, current.pid, current.listen, terms.stopping(s)) == (2, 11, 'https://h:1', True)
        assert s.get_one(type(first), 1).stopping_at == stopped_at, 'a stop marks the current term only'


def test_the_file_is_read_before_anything_opens_it_and_one_without_terms_has_none(db: Database, tmp_path: Path) -> None:
    """A leader provider reads it before a restore replaces it: no pooled connection holds the old file."""
    missing, bare = tmp_path / 'none.db', tmp_path / 'bare.db'
    sqlite3.connect(bare).close()
    for path in (missing, bare):
        assert (terms.newest_in(path), terms.running_in(path)) == (0, None)
    path = Path(db.engine.url.database or '')
    assert (terms.newest_in(path), terms.running_in(path)) == (0, None), 'migrated, before any daemon served'
    with db.session() as s:
        terms.begin(s, 5, 42, 'https://h:1')
        s.commit()
    assert (terms.newest_in(path), terms.running_in(path)) == (5, (5, 42, 'https://h:1'))
    with db.session() as s:
        terms.stop(s)
        s.commit()
    assert (terms.newest_in(path), terms.running_in(path)) == (5, None), 'it stops'
