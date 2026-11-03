"""``optimization``: the built-in fact source of an optimization campaign's research, from piloop's tables."""

from collections import Counter
from collections.abc import Callable
from datetime import datetime
from enum import IntEnum

from sqlalchemy import ColumnElement, Select, case, exists, func, select, true
from sqlalchemy.orm import Session

from piloop.db.models import Campaign, count
from piloop.plugins.campaign_type.kit.backlog.tables import Idea
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.evaluation.tables import Measurement
from piloop.plugins.campaign_type.kit.pool.pool import Pool
from piloop.plugins.campaign_type.kit.pool.scored import new_bests
from piloop.plugins.campaign_type.kit.runs.stop import attempt, kept, runs, runs_without_best, scored, unsettled
from piloop.plugins.campaign_type.kit.runs.tables import Run, campaign_runs
from piloop.plugins.fact_source.contract import FactSource
from piloop.plugins.fact_source.facts import (
    ATTEMPTS,
    BEST,
    EVALUATIONS,
    FUNNEL,
    METRIC_SUMS,
    NEW_BESTS,
    OPTIMIZATION,
    RUNS,
    UNSETTLED,
    WITHOUT_BEST,
    Fact,
    FactValue,
    Funnel,
)

type Gatherer = Callable[[Session, Campaign, datetime], FactValue]


def _best(s: Session, campaign: Campaign, now: datetime) -> float | None:
    pool = Pool.of(s, campaign, campaign.config.all_tracks[0])
    return pool.mean(pool.best())


def _runs(s: Session, campaign: Campaign, now: datetime) -> int:
    return runs(s, campaign)


def _unsettled(s: Session, campaign: Campaign, now: datetime) -> int:
    return count(s, campaign_runs(campaign.id).where(attempt(), unsettled()))


def _without_best(s: Session, campaign: Campaign, now: datetime) -> int:
    return runs_without_best(s, campaign)


def _new_bests(s: Session, campaign: Campaign, now: datetime) -> int:
    return kept(s, campaign)


def _attempts(s: Session, campaign: Campaign, now: datetime) -> int:
    return count(s, campaign_runs(campaign.id).where(attempt()))


def _measurements(campaign: Campaign) -> Select[Measurement]:
    """Select the campaign's measurements, under every evaluator."""
    mine = select(Measurement).join(Candidate, Measurement.candidate_id == Candidate.id)
    return mine.where(Candidate.campaign_id == campaign.id)


def _evaluations(s: Session, campaign: Campaign, now: datetime) -> int:
    return count(s, _measurements(campaign))


def _metric_sums(s: Session, campaign: Campaign, now: datetime) -> dict[str, float]:
    each = func.json_each(Measurement.metrics).table_valued('key', 'value')
    sums = _measurements(campaign).join(each, true()).with_only_columns(each.c.key, func.total(each.c.value))
    return {str(name): total for name, total in s.execute(sums.group_by(each.c.key)).all()}


class Stage(IntEnum):
    """How far an attempt got (``Funnel``). Each stage includes the ones before it."""

    ATTEMPTED = 0
    REACHED = 1
    """It left a candidate that is measured or waits for its score (``scored``)."""
    MEASURED = 2
    """One of its candidates has a measurement without an error."""
    BEST = 3
    """One of its candidates became the best when it was scored."""


def _stage(bests: set[int]) -> ColumnElement[int]:
    """The ``Stage`` of a run that was an attempt, with ``bests`` the candidates that became the best."""
    mine = Candidate.run_id == Run.id
    return case(
        (exists().where(mine, Candidate.id.in_(bests)), Stage.BEST.value),
        (
            exists().where(mine, Measurement.candidate_id == Candidate.id, Measurement.error.is_(None)),
            Stage.MEASURED.value,
        ),
        (scored(), Stage.REACHED.value),
        else_=Stage.ATTEMPTED.value,
    )


def _funnel(s: Session, campaign: Campaign, now: datetime) -> Funnel:
    # the attempts that ended and whose commits are scored
    bests = {e.target_id for e, _ in new_bests(s, campaign)}
    rows = s.execute(
        campaign_runs(campaign.id)
        .where(attempt(), ~unsettled())
        .with_only_columns(_stage(bests).label('stage'), Run.end, func.count(Run.id))
        .group_by('stage', Run.end)
    ).all()
    at_least = {at: sum(n for got, _, n in rows if got >= at) for at in Stage}
    unmeasured: Counter[str] = Counter()
    for got, end, n in rows:
        if got < Stage.MEASURED:
            unmeasured[str(end)] += n
    return Funnel(
        ideas=count(s, select(Idea).where(Idea.campaign_id == campaign.id)),
        attempts=at_least[Stage.ATTEMPTED],
        reached=at_least[Stage.REACHED],
        measured=at_least[Stage.MEASURED],
        bests=at_least[Stage.BEST],
        unmeasured=dict(unmeasured.most_common()),
    )


GATHERERS: dict[Fact[FactValue], Gatherer] = {
    BEST: _best,
    RUNS: _runs,
    UNSETTLED: _unsettled,
    WITHOUT_BEST: _without_best,
    NEW_BESTS: _new_bests,
    EVALUATIONS: _evaluations,
    METRIC_SUMS: _metric_sums,
    ATTEMPTS: _attempts,
    FUNNEL: _funnel,
}
"""How the source gathers each of its facts (``OPTIMIZATION``)."""


class OptimizationFacts(FactSource):
    """``optimization``: the research of the campaign: its best, runs, new bests, evaluations and funnel.

    It is on by default (``DEFAULT``). A campaign that sets ``[plugins.fact_source] use`` lists it too, to keep these facts.
    """

    DEFAULT = True

    def supplies(self) -> tuple[Fact[FactValue], ...]:
        """``OPTIMIZATION``."""
        return OPTIMIZATION

    def gather(
        self, s: Session, campaign: Campaign, wanted: frozenset[Fact[FactValue]], now: datetime
    ) -> dict[Fact[FactValue], FactValue]:
        """Each fact in ``wanted``, from piloop's tables (``GATHERERS``)."""
        return {fact: GATHERERS[fact](s, campaign, now) for fact in wanted}
