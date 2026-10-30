"""What the measurements mean: the current evaluator, a candidate's means and score under it, and remeasures.

All code that reads measurements to answer "how good is this" uses this module, so the CLI, the scheduler, the run
controller and the history block agree. What a score means against a bar is the verdicts component's
(``piloop.plugins.campaign_type.kit.verdicts.judge``).
"""

import json
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from statistics import fmean
from typing import Literal

from pydantic import BaseModel, Field, TypeAdapter
from sqlalchemy import ColumnElement, select
from sqlalchemy.orm import Session

from piloop.actor import Actor
from piloop.config import CampaignConfig
from piloop.db import requests
from piloop.db.events import emit
from piloop.db.models import (
    PROCESS,
    Campaign,
    Event,
    EventModel,
)
from piloop.ids import (
    TargetRef,
)
from piloop.plugins.campaign_type.kit.candidates.ids import CANDIDATE, CandidateId
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.evaluation import config
from piloop.plugins.campaign_type.kit.evaluation.config import Metric
from piloop.plugins.campaign_type.kit.evaluation.evaluate import UNITS, Units
from piloop.plugins.campaign_type.kit.evaluation.ids import MEASUREMENT, EvaluatorId, MeasurementId, RungName
from piloop.plugins.campaign_type.kit.evaluation.ladder import Gate
from piloop.plugins.campaign_type.kit.evaluation.tables import Evaluator, Measurement
from piloop.plugins.campaign_type.kit.evaluation.wire import REPEATS, RungResult
from piloop.plugins.campaign_type.kit.runs.ids import RunId
from piloop.plugins.campaign_type.kit.verdicts.verdict import (
    pending,
)

EventModel.retire('evaluator.metric_lines')  # 0.4 noted an evaluator that printed METRIC lines; 0.5 reads none

type Samples = dict[str, list[float]]
"""The samples of a paired evaluation: metric name → the value of each sample, in the order of the pairs."""


def current_evaluator(s: Session, campaign: Campaign, cfg: CampaignConfig) -> Evaluator:
    """Return the evaluator row that ``cfg`` describes, and make it the campaign's current one. Create it if needed.

    Raises:
        ValueError: the evaluator has a runner that is not installed (``evaluator_runners``).
    """
    key = cfg.evaluator_key
    evaluator = s.scalar(select(Evaluator).where(Evaluator.campaign_id == campaign.id, Evaluator.key == key))
    if evaluator is None:
        evaluator = Evaluator(campaign_id=campaign.id, key=key, definition=cfg.evaluator_definition)
        s.add(evaluator)
        s.flush()
    if evaluator.runner not in (installed := evaluator_runners()):
        raise ValueError(
            f'evaluator#{evaluator.id} of {campaign.name} has the evaluator runner {evaluator.runner!r}, which is not'
            f' installed: {", ".join(installed)}'
        )
    if campaign.evaluator_id != evaluator.id:
        campaign.evaluator_id = evaluator.id
        s.flush()
    return evaluator


def evaluator_runners() -> tuple[str, ...]:
    """Return the names of the installed evaluator runners: the built-in ``process`` for now.

    TODO: add the evaluator-runner plugins from the plugin registry when it lands; nothing else changes here.
    """
    return (PROCESS,)


def full(m: Measurement) -> bool:
    """Whether ``m`` scores: it measured the full panel, and succeeded. A rung of the ladder never scores."""
    return m.error is None and m.rung is None


def full_clause() -> ColumnElement[bool]:
    """Select the measurements that ``full`` is true of."""
    return Measurement.error.is_(None) & Measurement.rung.is_(None)


class _Judged(BaseModel):
    """A judging metric as an evaluator's ``definition`` stores it: its name, and its parts if piloop computes it."""

    name: str
    ratio: config.Ratio | None = None


JUDGED = TypeAdapter(list[_Judged])


def ratios(evaluator: Evaluator | None) -> dict[str, config.Ratio]:
    """The ratio metrics of ``evaluator``, by name: piloop computes them from what it measured (``derive``)."""
    if evaluator is None:
        return {}
    return {j.name: j.ratio for j in JUDGED.validate_python(evaluator.definition.get('judging', [])) if j.ratio}


def derive(values: dict[str, float], found: dict[str, config.Ratio]) -> dict[str, float]:
    """``values`` with each ratio of ``found`` whose parts it has, and whose denominator is not 0."""
    return values | {
        name: values[r.numerator] / values[r.denominator]
        for name, r in found.items()
        if r.numerator in values and values.get(r.denominator)
    }


def metrics(candidate: Candidate, evaluator_id: EvaluatorId | None) -> dict[str, float]:
    """Return the mean of each metric over the candidate's ``full`` measurements under ``evaluator_id``.

    A ratio is the ratio of the means of its parts (``derive``).
    """
    rows = [m for m in candidate.measurements if m.evaluator_id == evaluator_id and full(m)]
    return derive(means([m.metrics for m in rows]), ratios(rows[0].evaluator) if rows else {})


def rungs(candidate: Candidate, evaluator_id: EvaluatorId | None) -> dict[RungName, dict[str, float]]:
    """Return the mean of each metric at each rung of the ladder, over the candidate's measurements of the rung."""
    return {r.rung: r.metrics for r in rung_results(candidate, evaluator_id)}


def rung_results(candidate: Candidate, evaluator_id: EvaluatorId | None) -> list[RungResult]:
    """Return each rung of the ladder that the candidate measured under ``evaluator_id``, in the order of its first.

    Each is over the candidate's successful measurements of the rung.
    """
    found: dict[RungName, list[Measurement]] = {}
    for m in candidate.measurements:
        if m.evaluator_id == evaluator_id and m.error is None and m.rung is not None:
            found.setdefault(m.rung, []).append(m)
    return [
        RungResult(
            rung=rung,
            metrics=means([m.metrics for m in rows]),
            seconds=fmean(times) if (times := [m.seconds for m in rows if m.seconds is not None]) else None,
            measurements=len(rows),
            stopped=rows[-1].stopped,
        )
        for rung, rows in found.items()
    ]


def climb(candidate: Candidate, evaluator_id: EvaluatorId | None) -> str | None:
    """Return where the candidate's latest climb of the ladder under ``evaluator_id`` is, or None after the full panel.

    ``stopped at <rung>: <why>``, or ``reached <rung>`` while it climbs.
    """
    for m in reversed(candidate.measurements):
        if m.evaluator_id != evaluator_id or m.error is not None:
            continue
        match m.rung, m.stopped:
            case None, _:
                return None
            case rung, None:
                return f'reached {rung}'
            case _:
                return f'stopped at {m.rung}: {m.stopped}'
    return None


def means(rows: Sequence[dict[str, float]]) -> dict[str, float]:
    """Return the mean of each metric over ``rows``, the metrics of measurements."""
    names = {name for row in rows for name in row}
    return {name: fmean(values) for name in names if (values := [r[name] for r in rows if name in r])}


def score(candidate: Candidate, evaluator_id: EvaluatorId | None, primary: Metric) -> float | None:
    """Return the primary metric under ``evaluator_id``, or None when no successful measurement has it."""
    return metrics(candidate, evaluator_id).get(primary.name)


def measured(candidate: Candidate, evaluator_id: EvaluatorId | None) -> bool:
    """Return whether the candidate needs no evaluation under ``evaluator_id``: a ``full`` measurement, or a crash.

    A climb that stopped at a rung does not count, so what needs a score, a head or a bar, is measured in full, and a
    run that reaches the tree again climbs again.
    """
    return any(
        m.evaluator_id == evaluator_id and (m.rung is None or m.error is not None) for m in candidate.measurements
    )


class RemeasureRequested(EventModel):
    """Someone asked the daemon to measure a candidate again (``request_remeasure``)."""

    kind: Literal['candidate.remeasure_requested'] = 'candidate.remeasure_requested'
    repeats: int = Field(default=REPEATS, ge=1)
    """How many samples of the candidate, each next to one of its bar."""


class RemeasureRefused(Exception):
    """``request_remeasure`` cannot ask for the candidate. The message says why."""


def request_remeasure(s: Session, candidate: Candidate, actor: Actor, repeats: int = REPEATS) -> None:
    """Ask the daemon to measure ``candidate`` again with the current evaluator, next to its bar. The caller commits.

    Use it for a measurement that a fault of the host spoiled (a full disk, a lost GPU), or that load made noisy. The
    request (``RemeasureRequested``) is pending until a measurement of the candidate under the campaign's current
    evaluator is recorded (``record``). The daemon runs ``repeats`` pairs of the candidate and its bar (``bar_of`` its
    parent), then judges it again when it is ``rejudgeable`` (``RunController.remeasure``). A candidate without a
    parent, the baseline or a reference, gets ``repeats`` samples alone. A commit that the daemon never measured
    (``skipped`` or ``recovered``) gets its first measurement, to isolate a regression to it, say.

    Raises:
        RemeasureRefused: the candidate is pending: its run measures and judges it.
    """
    if pending(candidate):
        raise RemeasureRefused(f'candidate#{candidate.id} is pending: the daemon measures and judges it already')
    emit(s, RemeasureRequested(repeats=repeats), TargetRef(CANDIDATE, candidate.id), actor)


def remeasure_asked(s: Session, candidate: Candidate) -> RemeasureRequested | None:
    """Return the pending request to measure ``candidate`` again, or None."""
    found = requests.pending(s, RemeasureRequested, Event.target == TargetRef(CANDIDATE, candidate.id))
    return found[0][1] if found else None


def candidates_of(campaign: Campaign) -> ColumnElement[bool]:
    """Select the events on the candidates of ``campaign``."""
    of_campaign = select(Candidate.id).where(Candidate.campaign_id == campaign.id)
    return (Event.target_kind == CANDIDATE) & Event.target_id.in_(of_campaign)


def requested(s: Session, campaign: Campaign) -> list[tuple[Candidate, Event]]:
    """Return the candidates of ``campaign`` that someone asked to measure again, with the open requests, oldest first."""
    return [
        (s.get_one(Candidate, asked.target_id), asked)
        for asked, _ in requests.pending(s, RemeasureRequested, candidates_of(campaign))
    ]


def record(
    s: Session,
    candidate: Candidate,
    evaluator_id: EvaluatorId,
    actor: Actor,
    *,
    metrics: dict[str, float],
    error: str | None,
    seconds: float | None,
    run: RunId | None = None,
    pair: MeasurementId | None = None,
    rung: RungName | None = None,
    stopped: str | None = None,
    feedback: str | None = None,
) -> Measurement:
    """Store a measurement of ``candidate``, with ``MeasurementRecorded``. The caller commits.

    The one writer of measurements. ``run`` is the run that the daemon measured it for, ``pair`` the bar's
    measurement that ran next to it in a paired evaluation, ``rung`` the rung of the ladder that it measured, and
    ``stopped`` why the climb stopped there, and ``feedback`` the line that the result said of the run, which only its
    event keeps. A measurement of the full panel under the campaign's current evaluator
    answers the pending request to measure the candidate again (``request_remeasure``). A rung does not: the request
    waits for the full panel.
    """
    measurement = Measurement(
        candidate=candidate,
        evaluator_id=evaluator_id,
        metrics=metrics,
        error=error,
        seconds=seconds,
        pair_id=pair,
        rung=rung,
        stopped=stopped,
    )
    s.add(measurement)
    s.flush()
    recorded = MeasurementRecorded(
        candidate=candidate.id,
        metrics=metrics,
        error=error,
        run=run,
        pair=pair,
        rung=rung,
        stopped=stopped,
        feedback=feedback,
    )
    emit(s, recorded, TargetRef(MEASUREMENT, measurement.id), actor)
    if rung is None and evaluator_id == s.get_one(Campaign, candidate.campaign_id).evaluator_id:
        for asked, _ in requests.pending(s, RemeasureRequested, Event.target == TargetRef(CANDIDATE, candidate.id)):
            requests.answer(s, asked)
    return measurement


class MeasurementRecorded(EventModel):
    """A measurement of a candidate was recorded: by the daemon, or by hand (``piloop measurement add``)."""

    kind: Literal['measurement.recorded'] = 'measurement.recorded'
    candidate: CandidateId
    metrics: dict[str, float]
    error: str | None
    run: RunId | None
    """The run that the daemon measured it for. None for a measurement by hand or a cancelled evaluation."""
    pair: MeasurementId | None = None
    """The bar's measurement that ran next to it in a paired evaluation."""
    rung: RungName | None = None
    """The rung of the ladder that it measured. None for the full panel."""
    stopped: str | None = None
    """Why the climb stopped at ``rung``. None when it went on."""
    feedback: str | None = None
    """The line that the evaluate command's result said of the run (``Result.feedback``)."""


def units_file(units_dir: Path, measurement_id: MeasurementId) -> Path:
    """Return the path of a measurement's per-unit results, under ``PILOOP_HOME``.

    The file is outside every worktree and the database. The results are not a column, because every agent can read
    every table through ``piloop sql``, and an implementer must not tune against per-unit results. A file is not a sandbox,
    because an agent's shell can read ``PILOOP_HOME``. But the file is not in the schema, the briefs or any renderer.
    """
    return units_dir / f'{measurement_id}.json'


def save_units(units_dir: Path, measurement_id: MeasurementId, units: Units) -> None:
    """Store a measurement's per-unit results. With none, remove any old file that a reused id could find."""
    path = units_file(units_dir, measurement_id)
    if not units:
        path.unlink(missing_ok=True)
        return
    units_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(units))


def pairs(candidate: Candidate, bar: Candidate, evaluator_id: EvaluatorId) -> tuple[Samples, Samples]:
    """Return the samples that ``candidate`` and ``bar`` measured next to each other under ``evaluator_id``.

    The i-th sample of a metric of the candidate and the i-th of the bar are one pair (``Measurement.pair``). A pair
    counts when both of its measurements are ``full``. A ratio has no samples: a rule resamples its parts.
    """
    mine: Samples = {}
    theirs: Samples = {}
    for m in candidate.measurements:
        other = m.pair
        if m.evaluator_id != evaluator_id or not full(m) or other is None:
            continue
        if other.candidate_id != bar.id or not full(other):
            continue
        for name in m.metrics.keys() & other.metrics.keys():
            mine.setdefault(name, []).append(m.metrics[name])
            theirs.setdefault(name, []).append(other.metrics[name])
    return mine, theirs


def units(candidate: Candidate, evaluator_id: EvaluatorId, units_dir: Path) -> Units:
    """Return the candidate's per-unit results under ``evaluator_id``: each unit's mean over its measurements."""
    rows = [
        UNITS.validate_json(path.read_bytes())
        for m in candidate.measurements
        if m.evaluator_id == evaluator_id and full(m) and (path := units_file(units_dir, m.id)).is_file()
    ]
    per_metric: dict[str, dict[str, list[float]]] = {}
    for row in rows:
        for name, values in row.items():
            for unit, value in values.items():
                per_metric.setdefault(name, {}).setdefault(unit, []).append(value)
    return {name: {unit: fmean(v) for unit, v in values.items()} for name, values in per_metric.items()}


def gate_of(bar: Candidate, evaluator_id: EvaluatorId, cfg: CampaignConfig) -> Gate:
    """What the ladder's stops compare with: ``bar``'s means at each rung, and the metrics' directions."""
    return Gate(bar=rungs(bar, evaluator_id), directions={m.name: m.direction for m in cfg.metrics})


def known(c: Candidate) -> datetime:
    """Return the time of the first ``full`` measurement of the candidate, else its creation time."""
    return min((m.created_at for m in c.measurements if full(m)), default=c.created_at)
