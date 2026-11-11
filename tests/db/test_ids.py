"""Ids that never come back: which tables count with AUTOINCREMENT, and the lift of their sequences."""

import pytest
from sqlalchemy import text

from piloop.db.ids import EXACT, SHIFT, IdsExhausted, floor, lift
from piloop.db.models import Campaign, Comment
from piloop.db.session import Database

LEAVE = {'leases', 'agent_sessions', 'evaluations', 'events', 'comments', 'runs', 'candidates', 'ideas', 'measurements'}
"""The tables whose ids a worker, an agent or a client holds and sends back (decision 17)."""


def autoincrement(db: Database) -> dict[str, int | None]:
    with db.session() as s:
        tables = s.scalars(
            text("SELECT name FROM sqlite_master WHERE type = 'table' AND sql LIKE '%PRIMARY KEY AUTOINCREMENT%'")
        ).all()
        return {t: s.scalar(text('SELECT seq FROM sqlite_sequence WHERE name = :t'), {'t': t}) for t in tables}


def test_every_table_whose_ids_leave_the_daemon_counts_with_autoincrement(db: Database) -> None:
    assert set(autoincrement(db)) >= LEAVE


def test_a_lift_moves_every_sequence_up_to_its_floor_and_never_down(db: Database, campaign: Campaign) -> None:
    floor = 3 << SHIFT
    with db.session() as s:
        lift(s, floor)
        s.commit()
    assert set(autoincrement(db).values()) == {floor}, 'a table with no row yet gets its sequence too'
    # a comment, and the event that the change log writes for it
    with db.session() as s:
        s.add(Comment(target_kind='campaign', target_id=campaign.id, author='human', body='after the lift'))
        s.flush()
        lift(s, 2 << SHIFT)  # an older floor
        s.commit()
        assert s.scalar(text('SELECT max(id) FROM comments')) == floor + 1
    after = autoincrement(db)
    assert after['comments'] == floor + 1 and (after['events'] or 0) > floor, 'never lowered'


def test_a_terms_ids_stay_below_2_53_and_a_term_past_that_is_refused() -> None:
    last = EXACT // (1 << SHIFT) - 1
    assert (floor(1), floor(last)) == (1 << SHIFT, last << SHIFT) and (last + 1) << SHIFT == EXACT
    with pytest.raises(IdsExhausted, match=f'term {last + 1} would hand out ids past 2\\^53'):
        floor(last + 1)
