"""The ``evaluation`` job kind: the evaluate command on a candidate's tree, each run of it recorded as a measurement.

The evaluation queue (``queue``) decides which evaluation runs next and asks the executor for it. A worker runs it
(``run_evaluation``), and the report applies under its lease's fence (``measured``), so the executor knows nothing of
measuring.
"""

import subprocess
from collections.abc import Sequence
from enum import StrEnum
from pathlib import Path
from typing import ClassVar, Literal, assert_never

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from piloop.actor import System
from piloop.db.events import emit
from piloop.db.models import EventModel, ReadBy
from piloop.ids import TargetRef
from piloop.otel import Span
from piloop.plugins.campaign_type.kit.candidates.ids import CANDIDATE, CandidateId
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.evaluation import measure as ledger
from piloop.plugins.campaign_type.kit.evaluation.config import Rung
from piloop.plugins.campaign_type.kit.evaluation.evaluate import (
    EvalResult,
    Failed,
    Gone,
    InputsChanged,
    Killed,
    Measured,
    NotRun,
    Said,
    Score,
    Step,
    StoppedAt,
    printed,
    run_evaluate,
)
from piloop.plugins.campaign_type.kit.evaluation.ids import EvaluatorId, MeasurementId, RungName
from piloop.plugins.campaign_type.kit.evaluation.ladder import Gate
from piloop.plugins.campaign_type.kit.evaluation.measure import evaluator_runners, save_units
from piloop.plugins.campaign_type.kit.evaluation.tables import Evaluation, Evaluator
from piloop.plugins.campaign_type.kit.runs.ids import RunId
from piloop.plugins.executor.contract import Applying, Control, Job, JobKind, Report, Settled, Site, Source
from piloop.plugins.executor.kit.jobs import SHOWN, rebuilt
from piloop.plugins.vcs.contract import UnknownRevision
from piloop.proc import kill_group
from piloop.settings import UNITS_DIR

EVALUATION = 'evaluation'
"""The name of the kind (``piloop.job_kinds``)."""
INPUTS_CHANGED = 'inputs.changed'
"""An event: an evaluation changed a linked input that it read, so what it measured is a crash, not a measurement."""


class InfraFailed(EventModel):
    """An evaluation failed for a cause that is not its candidate's (``Failed.infra``): it recorded no measurement.

    The request that waits for it asks for it again (``RunController``), so it never becomes a verdict.
    """

    READ_BY: ClassVar[ReadBy] = ReadBy.READERS
    kind: Literal['evaluation.infra_error'] = 'evaluation.infra_error'
    evaluation: int
    """The evaluation, by its row in the queue (``evaluations``)."""
    error: str


class Bar(BaseModel):
    """The bar of a paired evaluation: the candidate that the measured one must beat, and its tree."""

    model_config = ConfigDict(frozen=True)

    candidate: CandidateId
    artifact: Source


class EvaluationJob(Job):
    """The description of an evaluation's job: the evaluator's runner and its arguments, on the candidate's tree.

    It runs the evaluator ``repeats`` times on the candidate. With a ``bar``, it is a **paired evaluation**: each run of
    the candidate has a run of the bar next to it, in the order candidate, bar, bar, candidate, candidate, bar. So a
    load that changes slowly hits both sides alike.
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal['evaluation'] = EVALUATION
    evaluation: int
    candidate: CandidateId
    run: RunId | None = None
    """The run whose version it measures, named on the measurement's event. None for a measurement on request."""
    runner: str
    """The evaluator runner (``evaluators.runner``). A worker takes only jobs of the runners that it has."""
    command: list[str]
    timeout: float
    score: Score
    """The primary that a result's ``score`` reports."""
    required: list[str]
    """The metrics that the evaluate command must report: the primaries and the guardrails."""
    known: list[str]
    """Every metric that it may report: those of ``[[metrics]]``. A result with another is refused."""
    max_evidence_bytes: int = Field(ge=0)
    """The evidence files that one run may name, all together (``[evaluation] max_evidence_mb``)."""
    ladder: list[Rung] = []
    """The rungs that each run of the evaluator on the candidate climbs before the full panel (``[[ladder]]``)."""
    gate: Gate | None = None
    """Set: the first run on the candidate stops at the first rung whose ``stop`` holds. None: every run climbs every
    rung."""
    stop_grace: float = Field(default=0, ge=0)
    """The seconds between the SIGTERM and the SIGKILL that end an evaluate command at a stop
    (``[evaluation] stop_grace_seconds``)."""
    bar: Bar | None = None
    """The bar to run next to the candidate. None: the candidate alone."""
    repeats: int = Field(default=1, ge=1)
    """How many times the evaluator runs on the candidate, and on the bar."""


class Sample(BaseModel):
    """One run of the evaluator on one tree, which the control plane records as one measurement."""

    metrics: dict[str, float]
    seconds: float
    error: str | None = None
    units: dict[str, dict[str, float]] = Field(default_factory=dict)
    rung: RungName | None = None
    """The rung of the ladder that it measured. None for the full panel."""
    stopped: str | None = None
    """Why the climb stopped at ``rung``. None when it went on."""
    feedback: str | None = None
    """What the result said of the run (``Result.feedback``)."""


class EvaluationReport(Report):
    """What an evaluation measured, in the order of its runs: each run's rungs, then its full panel.

    It stops at the first run that fails or stops at a rung.
    """

    kind: Literal['evaluation'] = EVALUATION
    samples: list[Sample] = Field(min_length=1)
    """The candidate's samples. The first one ran first."""
    bar_samples: list[Sample] = Field(default_factory=list)
    """The bar's samples of a paired evaluation: the i-th ran next to the i-th of ``samples``."""
    infra_error: str | None = None
    """Set when its last run failed for a cause that is not the candidate's (``Failed.infra``): then no sample of it
    counts, and it records no measurement."""


class Evaluations(JobKind):
    """``JobKind``: an evaluation of the evaluation queue (``evaluations``), on a worker with its evaluator's runner."""

    JOB: ClassVar[type[Job]] = EvaluationJob
    REPORT: ClassVar[type[Report]] = EvaluationReport
    SLOTS: ClassVar[int] = 2

    def id(self, job: Job) -> int:
        return _own(job).evaluation

    def runner(self, job: Job) -> str:
        return _own(job).runner

    def runners(self) -> list[str]:
        return list(evaluator_runners())

    def target(self, job: Job) -> TargetRef:
        """The candidate that it measures."""
        return TargetRef(CANDIDATE, _own(job).candidate)

    def run(self, job: Job, control: Control, site: Site, span: Span) -> Report:
        return run_evaluation(_own(job), control, site, span)

    def apply(self, s: Session, job: Job, report: Report, daemon: Applying) -> None:
        if not isinstance(report, EvaluationReport):
            raise TypeError(f'an evaluation reports an evaluation, not {report.kind}')
        measured(s, _own(job), report, daemon)

    def lost(self, s: Session, job_id: int) -> None:
        """The evaluation waits again for a slot of the queue."""
        evaluation = s.get(Evaluation, job_id)
        if evaluation is None:
            raise Settled(f'evaluation#{job_id} left the queue')
        evaluation.started_at = None

    def waiting(self, s: Session, runners: Sequence[str]) -> bool:
        """Whether an evaluation of one of ``runners`` waits in the queue for a slot."""
        query = (
            select(Evaluation.id)
            .join(Evaluator, Evaluator.id == Evaluation.evaluator_id)
            .where(Evaluation.started_at.is_(None), Evaluator.runner.in_(runners))
        )
        return s.scalar(query.limit(1)) is not None


def _own(job: Job) -> EvaluationJob:
    if not isinstance(job, EvaluationJob):
        raise TypeError(f'the evaluation kind runs evaluations, not {job.kind}')
    return job


KIND = Evaluations()
"""The kind, as its entry point names it."""


def measured(s: Session, job: EvaluationJob, report: EvaluationReport, daemon: Applying) -> None:
    """Record each sample of an evaluation as one measurement, and take the evaluation out of the queue.

    A sample of the full panel of the candidate names the bar's that ran next to it (``Measurement.pair``): the i-th
    of one side with the i-th of the other. A rung's sample has no pair.

    A cancelled evaluation records nothing (``queue.cancel``). Nor does one that ends while the daemon stops: its
    evaluator was killed, and the next daemon measures it again. Nor does an infra error: it emits ``InfraFailed``, and
    its request asks again.
    """
    evaluation = s.get(Evaluation, job.evaluation)
    if evaluation is None or evaluation.started_at is None:
        raise Settled(f'evaluation#{job.evaluation} is not running')
    s.delete(evaluation)
    if daemon.stopping or evaluation.cancelled_by is not None:
        return
    if report.infra_error is not None:
        failed = InfraFailed(evaluation=evaluation.id, error=report.infra_error)
        emit(s, failed, TargetRef(CANDIDATE, job.candidate), System.DAEMON)
        return
    evaluator_id = evaluation.evaluator_id
    bar = job.bar
    pairs: list[MeasurementId] = []
    for b in report.bar_samples:
        assert bar is not None  # a bar's samples come only from a paired evaluation
        measurement = record(s, daemon.home / UNITS_DIR, None, bar.candidate, evaluator_id, b)
        if b.rung is None:
            pairs.append(measurement)
    pending = iter(pairs)
    for sample in report.samples:
        pair = next(pending, None) if sample.rung is None else None
        record(s, daemon.home / UNITS_DIR, job.run, job.candidate, evaluator_id, sample, pair=pair)


def record(
    s: Session,
    units_dir: Path,
    run_id: RunId | None,
    candidate_id: CandidateId,
    evaluator_id: EvaluatorId,
    sample: Sample,
    *,
    pair: MeasurementId | None = None,
) -> MeasurementId:
    """Store one sample as the daemon's measurement (``ledger.record``), with its per-unit results, and return its id.

    The caller commits. ``pair`` is the bar's measurement that ran next to it in a paired evaluation.
    """
    candidate = s.get_one(Candidate, candidate_id)
    measurement = ledger.record(
        s,
        candidate,
        evaluator_id,
        System.DAEMON,
        metrics=sample.metrics,
        error=sample.error,
        seconds=sample.seconds,
        run=run_id,
        pair=pair,
        rung=sample.rung,
        stopped=sample.stopped,
        feedback=sample.feedback,
    )
    save_units(units_dir, measurement.id, sample.units)
    return measurement.id


def run_evaluation(job: EvaluationJob, control: Control, site: Site, span: Span) -> EvaluationReport:
    """Run the evaluator ``repeats`` times on the candidate's tree, each next to a run on the bar's tree, if any.

    The order is candidate, bar, bar, candidate, candidate, bar (``order``). It stops at the first run that fails: a
    candidate that crashes once crashes again, and a pair needs both sides. The candidate runs first, so each job
    measures it. Each run climbs the job's ladder (``_climb``): one sample for each rung, then one for the full panel.
    Only the first run, on the candidate, runs the rungs that have a command of their own, and only it may stop at a
    rung (``EvaluationJob.gate``), which ends the job too. A run that fails for a cause that is not the candidate's
    (``Failed.infra``) ends the job, and the report says so: none of its samples count. Each run has its own worktree
    and logs, under ``<side>-<n>`` in the job's place, and the report names the files of all of them.
    """
    samples: dict[Tree, list[Sample]] = {Tree.CANDIDATE: [], Tree.BAR: []}
    runs: dict[Tree, int] = {Tree.CANDIDATE: 0, Tree.BAR: 0}
    for i, side in enumerate(order(job.repeats, paired=job.bar is not None)):
        artifact = job.artifact if side is Tree.CANDIDATE or job.bar is None else job.bar.artifact
        one = job.model_copy(update={'artifact': artifact, 'place': f'{job.place}/{side}-{runs[side]}'})
        runs[side] += 1
        first = i == 0
        own = tuple(r for r in job.ladder if r.evaluate is not None) if first else ()
        results = _evaluate(one, control, site, span, own=own, gate=job.gate if first else None)
        climb = [sample(r) for r in results]
        last = climb[-1]
        span.set(error=last.error, stopped=last.stopped)
        samples[side].extend(climb)
        if isinstance(failed := results[-1], Failed) and failed.infra is not None:
            return EvaluationReport(samples=climb, infra_error=failed.error, files=site.files(job))
        if last.error is not None or last.stopped is not None or control.asked() or control.stopping():
            break
    return EvaluationReport(samples=samples[Tree.CANDIDATE], bar_samples=samples[Tree.BAR], files=site.files(job))


class Tree(StrEnum):
    """The tree that one run of an evaluation measures."""

    CANDIDATE = 'candidate'
    BAR = 'bar'


def order(repeats: int, *, paired: bool) -> list[Tree]:
    """The runs of an evaluation: ``repeats`` of the candidate, and with ``paired``, as many of the bar in ABBA order.

    Linear drift of the load then costs both sides alike. The candidate opens, so a first run that is slower (a cold
    cache) counts against the candidate, never for it.
    """
    if not paired:
        return [Tree.CANDIDATE] * repeats
    pairs = [(Tree.CANDIDATE, Tree.BAR), (Tree.BAR, Tree.CANDIDATE)]
    return [side for i in range(repeats) for side in pairs[i % 2]]


def _evaluate(
    job: EvaluationJob, control: Control, site: Site, span: Span, *, own: tuple[Rung, ...], gate: Gate | None
) -> list[EvalResult]:
    """Climb the evaluator's ladder once on the job's tree (``_climb``).

    An evaluate that another process killed, an agent's ``pkill -f`` say, measured nothing of the tree, so the climb
    runs once more, in a new worktree. The executor's own kills (a cancel, its stop) are not run again.
    """
    climb = _climb(job, control, site, span, own=own, gate=gate)
    match climb[-1]:
        case Failed(why=Killed() as killed) as failed if killed.sent and not control.asked() and not control.stopping():
            span.event('evaluation.retry', evaluation=job.evaluation, error=failed.error)
            climb = _climb(job, control, site, span, own=own, gate=gate)
        case _:
            pass
    return climb


def sample(result: EvalResult) -> Sample:
    """What the report says of one line of the evaluate command: what it measured, where it stopped, or why it failed."""
    match result:
        case Measured(metrics=metrics, seconds=seconds, units=units, rung=rung, feedback=feedback):
            return Sample(metrics=metrics, seconds=seconds, units=units, rung=rung, feedback=feedback)
        case StoppedAt(rung=rung, reason=reason, metrics=metrics, seconds=seconds, units=units):
            return Sample(metrics=metrics, seconds=seconds, units=units, rung=rung, stopped=reason)
        case Failed(seconds=seconds, rung=rung) as failed:
            feedback = failed.why.feedback if isinstance(failed.why, Said) else None
            return Sample(metrics=failed.printed, seconds=seconds, error=failed.error, rung=rung, feedback=feedback)
        case _:
            assert_never(result)


def _climb(
    job: EvaluationJob, control: Control, site: Site, span: Span, *, own: tuple[Rung, ...], gate: Gate | None
) -> list[EvalResult]:
    """Climb the ladder in a new worktree, which is removed after: what each rung measured, then the full panel.

    The rungs of ``own``, which have a command of their own, run first, each a run of its own. Then the evaluate
    command reports the other rungs and the full panel. With ``gate``, a rung may stop the climb. The climb ends at the first rung
    that stops it or fails. A failure to start is a crash. What it measured counts only if the linked inputs that it
    read still hold their blobs: a crash otherwise.
    """
    log_dir, cwd = site.log_dir(job), site.cwd(job)
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        links = rebuilt(site.checkout(job), span)

        def run(command: list[str], step: Step, prefix: str) -> list[EvalResult]:
            return run_evaluate(
                command,
                cwd=cwd,
                log_dir=log_dir,
                timeout=job.timeout,
                required=set(job.required),
                known=set(job.known),
                max_evidence_bytes=job.max_evidence_bytes,
                score=job.score,
                track=lambda proc: _track(control, proc),
                step=step,
                gate=gate,
                stop_grace=job.stop_grace,
                prefix=prefix,
            )

        climb: list[EvalResult] = []
        for rung in own:
            assert rung.evaluate is not None  # the rungs that have a command of their own
            climb += run(rung.evaluate, Step(last=rung), f'eval-{rung.name}')
            if not isinstance(climb[-1], Measured):
                return climb
        climb += run(job.command, Step(rungs=tuple(r for r in job.ladder if r.evaluate is None)), 'eval')
        if changed := sorted(rel for rel, copy in links.copies.items() if not copy.holds(cwd / rel)):
            span.event(INPUTS_CHANGED, evaluation=job.evaluation, paths=', '.join(changed[:SHOWN]))
            last = climb[-1]
            return [*climb[:-1], Failed(InputsChanged(tuple(changed[:SHOWN])), last.seconds, printed(last))]
        return climb
    except UnknownRevision as e:  # it never runs: the candidate's own crash
        span.error(e)
        return [Failed(Gone(str(e)), 0.0)]
    except Exception as e:  # anything else that makes no worktree is the machine's: ``Failed.infra`` retries it
        span.error(e)
        return [Failed(NotRun(f'{type(e).__name__}: {e}'), 0.0)]
    finally:
        site.remove(job)


def _track(control: Control, proc: subprocess.Popen[bytes] | None) -> None:
    """Tell the executor how to kill the evaluator while it runs. One that it refuses is killed at once."""
    if proc is None:
        control.running(None)
        return
    if not control.process(proc):
        kill_group(proc)
