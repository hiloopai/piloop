from collections.abc import Callable
from datetime import datetime, timedelta

import pytest
from inline_snapshot import snapshot
from sqlalchemy import select

from piloop.campaign.inbox import items
from piloop.db.models import Campaign, Comment, Event, Pause, utcnow
from piloop.db.session import Database
from piloop.ids import CommentKind, TargetKind
from piloop.plugins.campaign_type.kit.backlog.ids import IdeaStatus
from piloop.plugins.campaign_type.kit.backlog.tables import Idea
from piloop.plugins.campaign_type.optimization.idle import SlotsIdle, watch
from tests.campaign.test_stop import with_spend
from tests.daemon.test_scheduler import add_ideas, set_config

ALERT = timedelta(minutes=30)


def watched(db: Database, campaign: Campaign, now: datetime) -> datetime | None:
    with db.session() as s:
        stored = s.get_one(Campaign, campaign.id)
        due = watch(s, stored, stored.config, now)
        s.commit()
        return due


def escalations(db: Database) -> list[Comment]:
    with db.session() as s:
        return list(s.scalars(select(Comment).where(Comment.kind == CommentKind.ESCALATION).order_by(Comment.id)))


def spells(db: Database) -> list[str]:
    with db.session() as s:
        return list(s.scalars(select(Event.kind).where(Event.kind.like('slots_idle.%')).order_by(Event.id)))


def set_status(db: Database, ids: list[int], status: IdeaStatus) -> None:
    with db.session() as s:
        for idea_id in ids:
            s.get_one(Idea, idea_id).status = status
        s.commit()


def test_idle_slots_behind_proposed_ideas_raise_one_item_after_the_alert_until_the_slots_fill(
    db: Database, campaign: Campaign
) -> None:
    """Three slots, one runnable idea, three proposed ones: two slots sit idle, and after 30 minutes one P1 escalation
    says so. It is raised once, and the daemon acknowledges it when the approved ideas fill the slots."""
    set_config(db, campaign, plugins={'role': {'implementer': {'max_concurrent': 3}}})
    ready = add_ideas(db, campaign, 'runnable')
    proposed = add_ideas(db, campaign, 'a', 'b', 'c', status=IdeaStatus.PROPOSED)
    start = utcnow()
    due = watched(db, campaign, start)
    assert due == start + ALERT and escalations(db) == [], 'the spell starts: the item waits for the alert'
    assert watched(db, campaign, start + ALERT / 2) is not None and escalations(db) == []

    assert watched(db, campaign, start + ALERT + timedelta(seconds=1)) is None
    [item] = escalations(db)
    assert (item.target_kind, item.target_id, item.author, item.priority) == (
        TargetKind.CAMPAIGN,
        campaign.id,
        'daemon',
        1,
    )
    assert item.body == snapshot(
        "3 proposed ideas await review while 2 implementer slots are idle (idea#2, idea#3, idea#4). Set each one ready, parked or rejected: piloop idea status <id> ready --reason '…'."
    )
    with db.session() as s:
        assert [c.id for c, _ in items(s)] == [item.id], 'an inbox item, which the notifiers send'
    watched(db, campaign, start + 2 * ALERT)
    assert len(escalations(db)) == 1, 'one item for the spell'

    set_status(db, proposed, IdeaStatus.READY)
    assert watched(db, campaign, start + 2 * ALERT) is None
    [cleared] = escalations(db)
    assert (cleared.acked_at, cleared.acked_by) == (start + 2 * ALERT, 'daemon'), 'the slots fill: it clears'
    assert spells(db) == ['slots_idle.seen', 'slots_idle.raised', 'slots_idle.ended']

    set_status(db, [*ready, *proposed], IdeaStatus.DONE)
    add_ideas(db, campaign, 'd', status=IdeaStatus.PROPOSED)
    watched(db, campaign, start + 3 * ALERT)
    assert spells(db)[-1] == 'slots_idle.seen' and len(escalations(db)) == 1, 'a new spell waits for its own alert'


def pause(db: Database, campaign: Campaign, _proposed: list[int]) -> None:
    with db.session() as s:
        stored = s.get_one(Campaign, campaign.id)
        stored.paused, stored.paused_reason = Pause.LAUNCHES, 'a test'
        s.commit()


def alert_off(db: Database, campaign: Campaign, _proposed: list[int]) -> None:
    set_config(db, campaign, plugins={'role': {'implementer': {'idle_alert': 0}}})


def takes_others(db: Database, campaign: Campaign, _proposed: list[int]) -> None:
    set_config(db, campaign, plugins={'role': {'implementer': {'takes': 'priority >= 3'}}})


def hold(db: Database, campaign: Campaign, _proposed: list[int]) -> None:
    """The stop rule holds launches while the spend is not known: no reading yet."""
    set_config(db, campaign, plugins={'stop_rule': {'hold_at': {'spend': 10}}, 'fact_source': with_spend()})


def reject(db: Database, _campaign: Campaign, proposed: list[int]) -> None:
    set_status(db, proposed, IdeaStatus.REJECTED)


@pytest.mark.parametrize('change', [pause, hold, alert_off, takes_others, reject])
def test_a_spell_ends_and_its_item_clears_when_the_slots_no_longer_idle_behind_proposed_ideas(
    db: Database, campaign: Campaign, change: Callable[[Database, Campaign, list[int]], None]
) -> None:
    """A paused or held campaign idles on purpose; with the alert off, with no proposed idea that the role takes, or with none
    at all, nothing waits for a review."""
    proposed = add_ideas(db, campaign, 'a', status=IdeaStatus.PROPOSED)
    start = utcnow()
    watched(db, campaign, start)
    watched(db, campaign, start + 2 * ALERT)
    change(db, campaign, proposed)
    assert watched(db, campaign, start + 2 * ALERT) is None
    assert spells(db) == ['slots_idle.seen', 'slots_idle.raised', 'slots_idle.ended']
    [item] = escalations(db)
    assert item.acked_by == 'daemon'


def test_an_item_that_a_person_acknowledged_stays_acknowledged_and_is_not_raised_again(
    db: Database, campaign: Campaign
) -> None:
    add_ideas(db, campaign, 'a', status=IdeaStatus.PROPOSED)
    start = utcnow()
    watched(db, campaign, start)
    watched(db, campaign, start + 2 * ALERT)
    with db.session() as s:
        [item] = s.scalars(select(Comment).where(Comment.kind == CommentKind.ESCALATION))
        item.acked_at, item.acked_by = start, 'human'
        s.commit()
    watched(db, campaign, start + 3 * ALERT)
    [item] = escalations(db)
    assert item.acked_by == 'human' and spells(db) == ['slots_idle.seen', 'slots_idle.raised']
    with db.session() as s:
        assert [e.payload for e in s.scalars(select(Event).where(Event.kind == 'slots_idle.raised'))] == [
            SlotsIdle(kind='slots_idle.raised', role='implementer', comment=item.id)
        ]


def test_a_spell_that_ends_before_its_alert_raises_nothing_and_the_next_one_waits_anew(
    db: Database, campaign: Campaign
) -> None:
    proposed = add_ideas(db, campaign, 'a', status=IdeaStatus.PROPOSED)
    start = utcnow()
    watched(db, campaign, start)
    set_status(db, proposed, IdeaStatus.READY)
    watched(db, campaign, start + ALERT / 2)
    set_status(db, proposed, IdeaStatus.PROPOSED)
    assert watched(db, campaign, start + 2 * ALERT) == start + 2 * ALERT + ALERT
    assert spells(db) == ['slots_idle.seen', 'slots_idle.ended', 'slots_idle.seen'] and escalations(db) == []
