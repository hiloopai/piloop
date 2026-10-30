"""The yield of the latest runs: how far each got, and the alarm that pages a human while it stays low.

A **spell** of low yield starts when the campaign's ``low_yield`` trigger finds its window low
(``LowYield.check``). The daemon then posts one escalation of priority 1, and acknowledges it when the yield
recovers or the campaign stops.
"""

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, ClassVar, Literal

from sqlalchemy import ColumnElement, case, exists
from sqlalchemy.orm import Session

from piloop.actor import System
from piloop.campaign import escalations
from piloop.campaign.stop import _since_reopen
from piloop.db.events import emit, latest
from piloop.db.models import Campaign, Comment, Event, EventModel, ReadBy
from piloop.ids import CampaignStatus, CommentId, TargetKind, TargetRef
from piloop.plugins.campaign_type.kit.candidates.ids import Origin, Outcome
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.runs.controller import NOT_ENDED
from piloop.plugins.campaign_type.kit.runs.ids import FAILED_RUNS, FINISHED_RUNS
from piloop.plugins.campaign_type.kit.runs.stop import attempt, scored, unsettled
from piloop.plugins.campaign_type.kit.runs.tables import Run, campaign_runs
from piloop.plugins.escalation.contract import Choice, EscalationView

if TYPE_CHECKING:  # the trigger reads this module: only the annotation names it
    from piloop.plugins.campaign_type.optimization.trigger.low_yield import LowYield

PRIORITY = 1
"""The escalation's priority: a campaign whose runs reach nothing spends for nothing today."""


class Result(StrEnum):
    """How far a finished attempt got: the first that holds, in this order."""

    KEPT = 'kept'
    SCORED = 'scored'
    """It left a candidate that is measured or waits for its verdict."""
    SKIPPED = 'skipped scoring'
    """Its candidates all asked to be skipped (``Piloop-Eval: skip``)."""
    CRASHED = 'crashed'
    """It failed or timed out without a scored candidate, other than by saving no version."""
    NO_CANDIDATE = 'no candidate'


UNSCORED = (Result.NO_CANDIDATE, Result.SKIPPED, Result.CRASHED)
"""The results of the runs that did not reach the evaluator, in the order that a breakdown shows them."""


@dataclass(frozen=True)
class Yield:
    """The results of the latest finished attempts."""

    counts: dict[Result, int]
    last: datetime | None
    """When the newest of them finished. None without one."""

    @property
    def runs(self) -> int:
        return sum(self.counts.values())

    @property
    def scored(self) -> int:
        """The runs that reached the evaluator, the kept ones among them."""
        return self.counts.get(Result.SCORED, 0) + self.kept

    @property
    def kept(self) -> int:
        return self.counts.get(Result.KEPT, 0)

    def __str__(self) -> str:
        others = ', '.join(f'{n} {result}' for result in UNSCORED if (n := self.counts.get(result, 0)))
        line = f'{self.scored} of the last {self.runs} runs scored, {self.kept} kept'
        return f'{line}; the others: {others}' if others else line


class Low(StrEnum):
    """Why a window's yield is low."""

    UNSCORED = 'few runs reach scoring'
    """Fewer than ``min_scored`` of the runs scored."""
    KEEPLESS = 'no scored run is kept'
    """At least ``keepless`` of the runs scored, and none is kept."""


@dataclass(frozen=True)
class Advice:
    """What the escalation of a reason asks, why, and what a person checks."""

    ask: str
    why: str
    """With ``{scored}`` and ``{runs}``: the window's scored runs, and all its runs."""
    check: str


ADVICE: dict[Low, Advice] = {
    Low.UNSCORED: Advice(
        ask='Do you pause the campaign while you find why few runs reach a score?',
        why='Only {scored} of the last {runs} runs reached a score. The others end with no version to score.',
        check=(
            'Find out why the runs end without a scored version (piloop history, and the errors of the runs), then fix'
            ' the brief or the evaluate command.'
        ),
    ),
    Low.KEEPLESS: Advice(
        ask='Do you pause the campaign while you find why no scored run is kept?',
        why=(
            '{scored} of the last {runs} runs reached a score, and none is kept. The evaluate command or the bar may not'
            ' tell a better version.'
        ),
        check=(
            'Check that the evaluate command and the bar can tell a better version, and that the ideas aim at the'
            ' metric.'
        ),
    ),
}
"""What the escalation of each reason asks a person to do."""


@dataclass(frozen=True)
class Found:
    """A low yield: the window, why it is low, and the threshold that it missed in words."""

    got: Yield
    low: Low
    detail: str
    """``fewer than 50% scored``, ``none kept of 15 scored``."""

    def __str__(self) -> str:
        return f'{self.got}; {self.detail}'


def _result() -> ColumnElement[str]:
    """The ``Result`` of a finished attempt."""
    mine = Candidate.run_id == Run.id
    crashed = Run.end.in_(FAILED_RUNS) & (Run.error.is_(None) | (Run.error != NOT_ENDED))
    return case(
        (exists().where(mine, Candidate.verdict == Outcome.KEEP), Result.KEPT.value),
        (scored(), Result.SCORED.value),
        (exists().where(mine, Candidate.origin == Origin.SKIPPED), Result.SKIPPED.value),
        (crashed, Result.CRASHED.value),
        else_=Result.NO_CANDIDATE.value,
    )


def recent(s: Session, campaign: Campaign, window: int) -> Yield:
    """The results of the campaign's latest ``window`` attempts that finished and are judged, since its latest reopen.

    A run that a stop ended, or whose agent never started, is no attempt.
    """
    rows = s.execute(
        campaign_runs(campaign.id)
        .where(attempt(), Run.status.in_(FINISHED_RUNS), ~unsettled(), *_since_reopen(s, campaign, Run.finished_at))
        .with_only_columns(Run.finished_at, _result().label('result'))
        .order_by(Run.finished_at.desc(), Run.id.desc())
        .limit(window)
    ).all()
    counts = Counter(Result(row.result) for row in rows)
    return Yield(counts=dict(counts), last=rows[0].finished_at if rows else None)


class YieldAlarm(EventModel):
    """On a campaign: a spell of low yield raised its escalation, or ended."""

    READ_BY: ClassVar[ReadBy] = ReadBy.READERS
    kind: Literal['low_yield.raised', 'low_yield.cleared']
    comment: CommentId
    """The escalation."""


def spell(s: Session, campaign: Campaign) -> tuple[Event, YieldAlarm] | None:
    """The ``raised`` event of the campaign's open spell of low yield, or None when none is open."""
    last = latest(s, YieldAlarm, Event.target == TargetRef(TargetKind.CAMPAIGN, campaign.id))
    return last if last is not None and last[1].kind == 'low_yield.raised' else None


def ended_at(s: Session, campaign: Campaign) -> datetime | None:
    """When the campaign's latest spell of low yield ended, or None if none did."""
    on = Event.target == TargetRef(TargetKind.CAMPAIGN, campaign.id)
    last = latest(s, YieldAlarm, on, Event.kind == 'low_yield.cleared')
    return None if last is None else last[0].at


def watch(s: Session, campaign: Campaign, trigger: LowYield, now: datetime) -> None:
    """Raise the escalation of a new spell of low yield, or clear that of a spell that ended. The caller commits.

    A spell ends when the yield recovers or the campaign no longer runs: stopped or retired. A paused campaign keeps
    its spell. One item for each spell: a person's acknowledgement raises no second one.
    """
    low = trigger.check(s, campaign) if campaign.status is CampaignStatus.ACTIVE else None
    raised = spell(s, campaign)
    on = TargetRef(TargetKind.CAMPAIGN, campaign.id)
    match (low, None if raised is None else raised[1]):
        case (Found() as found, None):
            item = escalations.escalate(s, on, asked(found), author=System.DAEMON, priority=PRIORITY)
            emit(s, YieldAlarm(kind='low_yield.raised', comment=item.id), on, System.DAEMON)
        case (None, YieldAlarm(comment=comment)):
            emit(s, YieldAlarm(kind='low_yield.cleared', comment=comment), on, System.DAEMON)
            item = s.get(Comment, comment)
            if item is not None and item.acked_at is None:
                item.acked_at, item.acked_by = now, System.DAEMON.signature
        case _:  # no spell, or a spell that has its item
            pass


def asked(found: Found) -> EscalationView:
    """What the escalation of a spell of low yield asks: whether to pause the campaign while a person finds the cause."""
    advice = ADVICE[found.low]
    return EscalationView(
        headline=advice.ask,
        why=advice.why.format(scored=found.got.scored, runs=found.got.runs),
        choices=(
            Choice(
                id='A', label='Pause the campaign, and fix the cause.', result='No new run starts until you resume.'
            ),
            Choice(id='B', label='Let the campaign run.', result='New runs cost tokens, and most may give no score.'),
        ),
        recommended='A',
        details=f'{found.low.capitalize()}: {found}. {advice.check} Pause: piloop pause. Resume: piloop resume.',
    )
