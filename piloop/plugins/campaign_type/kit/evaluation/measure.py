"""What the measurements mean: the current evaluator, a candidate's means and score under it, and remeasures.

All code that reads measurements to answer "how good is this" uses this module, so the CLI, the scheduler, the run
controller and the history block agree. Where a score stands among the others is the pool's
(``piloop.plugins.campaign_type.kit.pool``).
"""

import hashlib
import json
from collections.abc import Sequence
from enum import StrEnum
from pathlib import Path
from statistics import fmean
from typing import Literal

from pydantic import BaseModel, Field, TypeAdapter, ValidationError
from sqlalchemy import ColumnElement, select
from sqlalchemy.exc import IntegrityError
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

EventModel.retire('evaluator.metric_lines')  # 0.4 noted an evaluator that printed METRIC lines; 0.5 reads none


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


class Split(StrEnum):
    """What an evaluation of a version scores."""

    VISIBLE = 'visible'
    """The evaluate command, with its ladder: what the pool reads."""
    SEALED = 'sealed'
    """The sealed split (``[evaluator] sealed``): only a candidate to confirm, and the baseline."""


SEALED = 'sealed'
"""The key of a sealed split's evaluator's ``definition``: its command. A visible evaluator's has none."""


def sealed_evaluator(s: Session, campaign: Campaign, cfg: CampaignConfig) -> Evaluator | None:
    """Return the evaluator row of the campaign's sealed split, created if needed, or None without one.

    It is an evaluator of its own, keyed by its command, so its measurements never count as the visible ones. It is
    never the campaign's current evaluator.
    """
    if (found := sealed_of(s, campaign, cfg)) is not None or (command := cfg.evaluator.sealed) is None:
        return found
    try:
        with s.begin_nested():  # two candidates of one campaign confirmed at once both find none
            found = Evaluator(campaign_id=campaign.id, key=_sealed_key(command), definition={SEALED: list(command)})
            s.add(found)
    except IntegrityError:
        found = sealed_of(s, campaign, cfg)
    return found


def sealed_of(s: Session, campaign: Campaign, cfg: CampaignConfig) -> Evaluator | None:
    """Return the evaluator row of the campaign's sealed split, or None without one or before its first evaluation."""
    if (command := cfg.evaluator.sealed) is None:
        return None
    row = select(Evaluator).where(Evaluator.campaign_id == campaign.id, Evaluator.key == _sealed_key(command))
    return s.scalar(row)


def retired(s: Session, evaluator_id: EvaluatorId) -> bool:
    """Whether its campaign no longer measures with ``evaluator_id``: as its current evaluator or its sealed one.

    A sealed split's evaluator stays while the stored config does not validate: what replaced it is not known.
    """
    evaluator = s.get_one(Evaluator, evaluator_id)
    campaign = s.get_one(Campaign, evaluator.campaign_id)
    if evaluator.id == campaign.evaluator_id:
        return False
    if SEALED not in evaluator.definition:
        return True
    try:
        command = campaign.config.evaluator.sealed
    except ValidationError:  # the campaigns' pass records this error for the campaign
        return False
    return command is None or _sealed_key(command) != evaluator.key


def _sealed_key(command: Sequence[str]) -> str:
    definition: dict[str, object] = {SEALED: list(command)}
    return hashlib.sha256(json.dumps(definition, sort_keys=True).encode()).hexdigest()[:16]


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
    """How many evaluations of the candidate."""


def request_remeasure(s: Session, candidate: Candidate, actor: Actor, repeats: int = REPEATS) -> None:
    """Ask the daemon to evaluate ``candidate`` ``repeats`` more times with the current evaluator. The caller commits.

    Use it for a measurement that a fault of the host spoiled (a full disk, a lost GPU), or that load made noisy. The
    request (``RemeasureRequested``) is pending until a measurement of the candidate under the campaign's current
    evaluator is recorded (``record``). A commit that the daemon never measured (``skipped`` or ``recovered``) gets
    its first measurements, to isolate a regression to it, say. The candidate's mean then includes them.
    """
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
    rung: RungName | None = None,
    stopped: str | None = None,
    feedback: str | None = None,
) -> Measurement:
    """Store a measurement of ``candidate``, with ``MeasurementRecorded``. The caller commits.

    The one writer of measurements. ``run`` is the run that the daemon measured it for, ``rung`` the rung of the ladder that it measured, and
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
