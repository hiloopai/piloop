"""The ``evaluation`` job kind: the evaluate command on a candidate's tree, each run of it recorded as a measurement.

The evaluation queue (``queue``) decides which evaluation runs next and asks the executor for it. A worker runs it
(``run_evaluation``), and the report applies under its lease's fence (``measured``), so the executor knows nothing of
measuring.
"""

import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import ClassVar, Literal, assert_never

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from piloop.actor import System
from piloop.db.events import emit
from piloop.db.models import Campaign, EventModel
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
    Infra,
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
from piloop.plugins.campaign_type.kit.evaluation.ops import sealed_refused
from piloop.plugins.campaign_type.kit.evaluation.tables import Evaluation, Evaluator
from piloop.plugins.campaign_type.kit.runs.ids import RunId
from piloop.plugins.executor.contract import Applying, Control, Job, JobKind, Report, Settled, Site
from piloop.plugins.executor.kit.jobs import SHOWN, rebuilt
from piloop.plugins.vcs.contract import UnknownRevision
from piloop.proc import kill_group
from piloop.settings import UNITS_DIR

EVALUATION = 'evaluation'
"""The name of the kind (``piloop.job_kinds``)."""
INPUTS_CHANGED = 'inputs.changed'
"""An event: an evaluation changed a linked input that it read, so what it measured is a crash, not a measurement."""


class InfraError(BaseModel):
    """Why an evaluation measured nothing of its candidate (``Failed.infra``), and its error."""

    model_config = ConfigDict(frozen=True)

    why: Infra
    error: str


class InfraFailed(EventModel):
    """An evaluation failed for a cause that is not its candidate's (``Failed.infra``): it recorded no measurement.

    The request that waits for it asks for it again (``RunController``), so it never becomes a score.
    """

    kind: Literal['evaluation.infra_error'] = 'evaluation.infra_error'
    evaluation: int
    """The evaluation, by its row in the queue (``evaluations``)."""
    error: str


class EvaluationJob(Job):
    """The description of an evaluation's job: the evaluator's runner and its arguments, on the candidate's tree.

    It runs the evaluator ``repeats`` times on the candidate.
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
    repeats: int = Field(default=1, ge=1)
    """How many times the evaluator runs on the candidate."""
    sealed: bool = False
    """It scores the sealed split (``[evaluator] sealed``): a result of its command that piloop refuses is the setup's
    fault, not the candidate's (``Failed.infra``)."""


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
    infra_error: InfraError | None = None
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

    A cancelled evaluation records nothing (``queue.cancel``). Nor does one that ends while the daemon stops: its
    evaluator was killed, and the next daemon measures it again. Nor does an infra error: it emits ``InfraFailed``, and
    its request asks again. A sealed command whose result piloop refuses escalates too, once (``sealed_refused``).
    """
    evaluation = s.get(Evaluation, job.evaluation)
    if evaluation is None or evaluation.started_at is None:
        raise Settled(f'evaluation#{job.evaluation} is not running')
    s.delete(evaluation)
    if daemon.stopping or evaluation.cancelled_by is not None:
        return
    if (infra := report.infra_error) is not None:
        failed = InfraFailed(evaluation=evaluation.id, error=infra.error)
        emit(s, failed, TargetRef(CANDIDATE, job.candidate), System.DAEMON)
        if infra.why is Infra.SEALED:
            sealed_refused(s, s.get_one(Campaign, s.get_one(Candidate, job.candidate).campaign_id), infra.error)
        return
    for sample in report.samples:
        record(s, daemon.home / UNITS_DIR, job.run, job.candidate, evaluation.evaluator_id, sample)


def record(
    s: Session,
    units_dir: Path,
    run_id: RunId | None,
    candidate_id: CandidateId,
    evaluator_id: EvaluatorId,
    sample: Sample,
) -> MeasurementId:
    """Store one sample as the daemon's measurement (``ledger.record``), with its per-unit results, and return its id.

    The caller commits.
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
        rung=sample.rung,
        stopped=sample.stopped,
        feedback=sample.feedback,
    )
    save_units(units_dir, measurement.id, sample.units)
    return measurement.id


def run_evaluation(job: EvaluationJob, control: Control, site: Site, span: Span) -> EvaluationReport:
    """Run the evaluator ``repeats`` times on the candidate's tree.

    It stops at the first run that fails: a candidate that crashes once crashes again. Each run climbs the job's ladder
    (``_climb``): one sample for each rung, then one for the full panel. Only the first run runs the rungs that have a
    command of their own, and only it may stop at a rung (``EvaluationJob.gate``), which ends the job too. A run that
    fails for a cause that is not the candidate's (``Failed.infra``) ends the job, and the report says so: none of its
    samples count. Each run has its own worktree and logs, under ``candidate-<n>`` in the job's place, and the report
    names the files of all of them.
    """
    samples: list[Sample] = []
    for i in range(job.repeats):
        one = job.model_copy(update={'place': f'{job.place}/candidate-{i}'})
        first = i == 0
        own = tuple(r for r in job.ladder if r.evaluate is not None) if first else ()
        results = _evaluate(one, control, site, span, own=own, gate=job.gate if first else None)
        climb = [sample(r) for r in results]
        last = climb[-1]
        span.set(error=last.error, stopped=last.stopped)
        samples.extend(climb)
        if isinstance(failed := results[-1], Failed) and (why := failed.infra(sealed=job.sealed)) is not None:
            return EvaluationReport(
                samples=climb, infra_error=InfraError(why=why, error=failed.error), files=site.files(job)
            )
        if last.error is not None or last.stopped is not None or control.asked() or control.stopping():
            break
    return EvaluationReport(samples=samples, files=site.files(job))


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
