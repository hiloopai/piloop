from piloop.db import terms
from piloop.db.session import Database


def test_each_term_takes_the_next_epoch_and_a_stop_marks_only_the_current_one_once(db: Database) -> None:
    with db.session() as s:
        assert (terms.current(s), terms.stopping(s)) == (None, False), 'before any daemon served'
        terms.stop(s)  # nothing to stop
        first = terms.begin(s, 10, None)
        terms.stop(s)
        s.commit()
        s.expire_all()
        stopped_at = first.stopping_at
        assert stopped_at is not None
        second = terms.begin(s, 11, 'https://h:1')
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
