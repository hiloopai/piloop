"""The ``low_yield`` trigger and its alarm: few runs that reach scoring, or none kept, wake the watchdog and page once."""

from pathlib import Path
from typing import Never

from inline_snapshot import snapshot
from sqlalchemy import select

from piloop.campaign.inbox import items
from piloop.db.events import AnomalyReported
from piloop.db.models import Campaign, Comment, Event, utcnow
from piloop.db.session import Database
from piloop.ids import CampaignStatus, CommentKind, TargetKind, TargetRef
from piloop.plugins.campaign_type.consumers import RolePass
from piloop.plugins.campaign_type.kit.candidates.ids import Origin
from piloop.plugins.campaign_type.kit.runs.controller import NO_CHANGES
from piloop.plugins.campaign_type.kit.runs.ids import RunStatus
from piloop.plugins.campaign_type.optimization.trigger.low_yield import LowYield
from piloop.plugins.campaign_type.optimization.yields import Result, recent, watch
from piloop.plugins.section.contract import SECTIONS, Rendering, markdown
from piloop.plugins.trigger.contract import Fire
from tests.campaign.test_stop import add_candidate, add_run
from tests.conftest import WATCHDOG, cas_of
from tests.daemon.test_scheduler import set_config
from tests.reports.test_overview import agent

NONE, SKIPPED, CRASHED, SCORED, KEPT = (
    Result.NO_CANDIDATE,
    Result.SKIPPED,
    Result.CRASHED,
    Result.SCORED,
    Result.KEPT,
)


def finished(db: Database, campaign: Campaign, *results: Result) -> None:
    """Add one finished run for each of ``results``, in the order that they finish."""
    for result in results:
        match result:
            case Result.NO_CANDIDATE:
                add_run(db, campaign, status=RunStatus.FAILED, error=NO_CHANGES, measured=False)
            case Result.CRASHED:
                add_run(db, campaign, status=RunStatus.FAILED, error='agent exit 1', measured=False)
            case Result.SKIPPED:
                add_candidate(db, campaign, add_run(db, campaign, measured=False), Origin.SKIPPED)
            case Result.SCORED:
                add_run(db, campaign)
            case Result.KEPT:
                add_run(db, campaign, keep_score=5.0)


def fired(db: Database, campaign: Campaign, trigger: LowYield) -> Fire | None:
    """What ``trigger`` of the watchdog fires on a pass now."""

    def unread(_: object) -> Never:
        raise AssertionError('the yield reads no backlog and no stall')

    with db.session() as s:
        p = RolePass(
            s=s,
            now=utcnow(),
            campaign=s.get_one(Campaign, campaign.id),
            role=WATCHDOG,
            last=None,
            quiet=None,
            max_concurrent=1,
            interval=0,
            backlog=unread,
            without_keep=unread,
        )
        found = trigger.fire(p)
        if found is not None:  # the scheduler gives the incident to the watchdog's row
            row = agent(s, p.campaign, WATCHDOG, 0, 5)
            for event in found.events:
                s.add(Event(actor='daemon', target=TargetRef(TargetKind.AGENT_SESSION, row.id), payload=event))
            s.commit()
        return found


def watched(db: Database, campaign: Campaign, trigger: LowYield | None = None) -> None:
    with db.session() as s:
        watch(s, s.get_one(Campaign, campaign.id), trigger or LowYield(), utcnow())
        s.commit()


def escalations(db: Database) -> list[Comment]:
    with db.session() as s:
        return list(s.scalars(select(Comment).where(Comment.kind == CommentKind.ESCALATION).order_by(Comment.id)))


def alarms(db: Database) -> list[str]:
    with db.session() as s:
        return list(s.scalars(select(Event.kind).where(Event.kind.like('low_yield.%')).order_by(Event.id)))


def test_the_yield_counts_the_latest_finished_attempts_by_how_far_each_got(db: Database, campaign: Campaign) -> None:
    """A run is kept, scored, skipped scoring, crashed or left no candidate: the first that holds, in that order.

    Runs in flight, and runs that a stop ended, are not in the window.
    """
    finished(db, campaign, KEPT, NONE, SKIPPED, CRASHED, SCORED, NONE)
    add_run(db, campaign, status=RunStatus.RUNNING)
    add_run(db, campaign, status=RunStatus.TIMED_OUT, measured=False)
    with db.session() as s:
        row = s.get_one(Campaign, campaign.id)
        assert str(recent(s, row, 20)) == snapshot(
            '2 of the last 7 runs scored, 1 kept; the others: 2 no candidate, 1 skipped scoring, 2 crashed'
        )
        assert str(recent(s, row, 3)) == snapshot(
            '1 of the last 3 runs scored, 0 kept; the others: 1 no candidate, 1 crashed'
        )


def test_few_scored_runs_wake_the_watchdog_once_until_the_yield_recovers(db: Database, campaign: Campaign) -> None:
    """Fewer than ``min_scored`` of a full window scored: one incident, with the breakdown, until it recovers."""
    trigger = LowYield(window=4, min_scored=0.5)
    finished(db, campaign, NONE, NONE, SCORED)
    assert fired(db, campaign, trigger) is None, 'three runs: not yet a window'
    finished(db, campaign, CRASHED)
    fire = fired(db, campaign, trigger)
    assert fire is not None and fire.alone
    [event] = fire.events
    assert isinstance(event, AnomalyReported) and event.line == snapshot(
        '- campaign#1 yields little, 0s ago: 1 of the last 4 runs scored, 0 kept; the others: 2 no candidate, 1 crashed;'
        ' fewer than 50% scored'
    )
    finished(db, campaign, NONE)
    assert fired(db, campaign, trigger) is None, 'the watchdog got this spell'
    watched(db, campaign, trigger)
    finished(db, campaign, SCORED, SCORED, SCORED)
    watched(db, campaign, trigger)
    assert fired(db, campaign, trigger) is None, 'recovered'
    finished(db, campaign, NONE, NONE, NONE)
    assert fired(db, campaign, trigger) is not None, 'a new spell after the recovery'


def test_scored_runs_without_a_keep_wake_the_watchdog(db: Database, campaign: Campaign) -> None:
    """``keepless`` scored runs in the window and none kept: the runs reach the evaluator, and nothing is better."""
    finished(db, campaign, SCORED, SCORED, SCORED, NONE)
    assert fired(db, campaign, LowYield(window=4, min_scored=0.5, keepless=None)) is None
    fire = fired(db, campaign, LowYield(window=4, min_scored=0.5, keepless=3))
    assert fire is not None
    [event] = fire.events
    assert isinstance(event, AnomalyReported) and event.line.endswith('; none kept of 3 scored')
    finished(db, campaign, KEPT)
    assert fired(db, campaign, LowYield(window=4, min_scored=0.5, keepless=3)) is None, 'a keep in the window'


def test_a_low_yield_raises_one_p1_escalation_that_clears_when_the_yield_recovers(
    db: Database, campaign: Campaign
) -> None:
    """The daemon posts one escalation for a spell of low yield, and acknowledges it once the yield recovers."""
    trigger = LowYield(window=4, min_scored=0.5)
    finished(db, campaign, NONE, SKIPPED, CRASHED, SCORED)
    watched(db, campaign, trigger)
    [item] = escalations(db)
    assert (item.target_kind, item.target_id, item.author, item.priority, item.acked_at) == (
        TargetKind.CAMPAIGN,
        campaign.id,
        'daemon',
        1,
        None,
    )
    assert item.body == snapshot(
        'Few runs reach scoring: 1 of the last 4 runs scored, 0 kept; the others: 1 no candidate, 1 skipped scoring,'
        ' 1 crashed; fewer than 50% scored. Find out why the runs end without a scored version (piloop history, and the'
        ' errors of the runs), then fix the brief or the evaluate command, or pause the campaign: piloop pause.'
    )
    with db.session() as s:
        assert [c.id for c, _ in items(s)] == [item.id], 'an inbox item, which the notifiers send'
    finished(db, campaign, NONE)
    watched(db, campaign, trigger)
    assert len(escalations(db)) == 1, 'one item for the spell'

    finished(db, campaign, SCORED, SCORED, SCORED)
    watched(db, campaign, trigger)
    [cleared] = escalations(db)
    assert cleared.acked_by == 'daemon', 'the yield recovered: it clears'
    assert alarms(db) == ['low_yield.raised', 'low_yield.cleared']

    finished(db, campaign, NONE, NONE, NONE)
    watched(db, campaign, trigger)
    assert len(escalations(db)) == 2, 'a new spell, a new item'


def test_a_stopped_campaign_clears_its_alarm_and_a_human_ack_raises_nothing_new(
    db: Database, campaign: Campaign
) -> None:
    trigger = LowYield(window=2, min_scored=0.5)
    finished(db, campaign, NONE, NONE)
    watched(db, campaign, trigger)
    with db.session() as s:
        item = s.scalars(select(Comment)).one()
        item.acked_at, item.acked_by = utcnow(), 'human'
        s.commit()
    watched(db, campaign, trigger)
    assert len(escalations(db)) == 1, 'acknowledged, the spell still has its item'
    with db.session() as s:
        s.get_one(Campaign, campaign.id).status = CampaignStatus.STOPPED
        s.commit()
    watched(db, campaign, trigger)
    [item] = escalations(db)
    assert item.acked_by == 'human', "a person's ack is kept"
    assert alarms(db) == ['low_yield.raised', 'low_yield.cleared']


def test_the_alarm_takes_its_thresholds_from_the_low_yield_table(db: Database, campaign: Campaign) -> None:
    """``[plugins.trigger.low_yield]`` sets the alarm too, whether a role names the trigger or not."""
    set_config(db, campaign, plugins={'trigger': {'low_yield': {'window': 2, 'min_scored': 0.5}}})
    with db.session() as s:
        assert LowYield.of(s.get_one(Campaign, campaign.id).config) == LowYield(window=2, min_scored=0.5)
    set_config(db, campaign, plugins={})
    with db.session() as s:
        assert LowYield.of(s.get_one(Campaign, campaign.id).config) == LowYield()


def test_status_shows_the_yield_and_the_open_alarm(db: Database, campaign: Campaign) -> None:
    set_config(db, campaign, plugins={'trigger': {'low_yield': {'window': 4, 'min_scored': 0.5}}})
    section = [SECTIONS.validated({'provider': 'yields'})]

    def shown() -> str:
        with db.session() as s:
            row = s.get_one(Campaign, campaign.id)
            return markdown(section, Rendering(s, utcnow(), row, Path('/no/workspace'), cas_of(s)))

    assert shown() == '', 'no finished run yet'
    finished(db, campaign, NONE, NONE, SCORED, CRASHED)
    assert shown() == snapshot("""\
## Yield

- 1 of the last 4 runs scored, 0 kept; the others: 2 no candidate, 1 crashed.
- Low: fewer than 50% scored.\
""")
    watched(db, campaign, LowYield(window=4, min_scored=0.5))
    assert shown() == snapshot("""\
## Yield

- 1 of the last 4 runs scored, 0 kept; the others: 2 no candidate, 1 crashed.
- Low: fewer than 50% scored. Escalated in comment#1, 0s ago.\
""")
