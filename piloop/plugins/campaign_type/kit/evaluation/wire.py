"""What the evaluation's routes take and answer, and the client that calls them."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import ConfigDict, Field

from piloop.api.client import Client
from piloop.api.contract import Params, Polled
from piloop.api.views import View
from piloop.plugins.campaign_type.kit.candidates.ids import CandidateId
from piloop.plugins.campaign_type.kit.candidates.wire import CandidateAnswer, CandidateView
from piloop.plugins.campaign_type.kit.evaluation.ids import EvaluatorId, MeasurementId, RungName
from piloop.plugins.campaign_type.kit.runs.ids import RunId

REPEATS = 3
"""The pairs of a remeasure that asks for no number: the 95% bound of 3 pairs is near their worst pair."""


class Report(Params):
    """A measurement made outside the daemon, for example by running the evaluate command by hand."""

    model_config = ConfigDict(extra='forbid')

    metrics: dict[str, Annotated[float, Field(strict=True, allow_inf_nan=False)]]
    """Each metric's value, as the evaluate command's result would give it."""
    seconds: Annotated[float, Field(ge=0, allow_inf_nan=False)] | None = None
    """How long the evaluation took."""
    error: str | None = None
    """Why the evaluation failed. A failed measurement may lack metrics."""


class MeasurementAdded(View):
    """A measurement recorded by hand, read at ``cursor``."""

    cursor: int
    measurement: MeasurementId
    rejudged: bool
    """Whether the daemon judges the candidate again (``RejudgeRequested``)."""
    effect: str
    """What happens to the verdict, in one line: when the daemon judges it again, or why the verdict stays."""


class RunningSince(View):
    """An evaluation that has a slot of the executor."""

    kind: Literal['running'] = 'running'
    since: datetime


class WaitingSince(View):
    """An evaluation that waits for a slot, or a remeasure that someone asked for and that is not queued yet."""

    kind: Literal['waiting'] = 'waiting'
    since: datetime
    """When it was asked for."""


class CancelledBy(View):
    """A running evaluation that someone cancelled: the daemon stops its evaluator."""

    kind: Literal['cancelled'] = 'cancelled'
    by: str
    """Who cancelled it, as the actor signs."""


type EvaluationState = Annotated[RunningSince | WaitingSince | CancelledBy, Field(discriminator='kind')]


class QueuedEvaluation(View):
    """One evaluation of the queue."""

    candidate: CandidateId
    campaign: str
    """The name of the candidate's campaign."""
    summary: str | None
    state: EvaluationState


class EvaluationQueue(View):
    """The running evaluations, oldest start first, then the waiting ones in the order in which they get a slot."""

    evaluations: list[QueuedEvaluation]


class RungResult(View):
    """A rung of the ladder that a candidate measured, over its successful measurements of the rung."""

    rung: RungName
    metrics: dict[str, float]
    """The mean of each metric at the rung."""
    seconds: float | None
    """The mean time of the rung alone, without the rungs before it. Null when no measurement of it has a time."""
    measurements: int
    stopped: str | None
    """Why the latest measurement of the rung stopped the climb there. Null when the climb went on."""


class CandidateDetail(View):
    """A candidate, with what its measurements under its campaign's current evaluator say."""

    candidate: CandidateView
    revision: str
    parent: CandidateId | None
    run: RunId | None
    evaluator: EvaluatorId | None
    metrics: dict[str, float]
    """The mean of each metric over its measurements of the full panel. A ratio is computed from its parts."""
    parent_metrics: dict[str, float]
    """The same of its parent. Empty without a parent."""
    rungs: list[RungResult]
    """Each rung of the ladder that it measured, in the order that it first measured them."""


class SubmitBody(Params):
    """A version of the caller's run to score now (``piloop submit``)."""

    revision: str
    """The version, as the campaign's vcs names one: a commit of the caller's checkout."""
    blob: str | None = None
    """The address in the CAS of the versions that the repo of record lacks (``Repository.send``), from a checkout on a
    worker elsewhere. None: the repo of record has the version. Text, so a client loads no CAS."""


class SubmissionQuery(Polled):
    """How long to wait for a submission's verdict."""


class Submission(View):
    """A submission as its implementer reads it: the verdict and the score, nothing else."""

    candidate: CandidateId
    verdict: str
    """The verdict, ``pending`` while piloop scores it, or ``skipped`` when someone cancelled its evaluation."""
    score: float | None
    """The primary's value, on the evaluate command's score. None without one."""


class EvaluationClient(Client):
    """The calls of the evaluation's routes: a candidate, a measurement by hand, the queue, a cancel and a submission."""

    def submit(self, body: SubmitBody) -> Submission:
        return self.post('/v1/submissions', body, Submission)

    def submission(self, candidate: CandidateId, query: SubmissionQuery) -> Submission:
        return self._get(f'/v1/submissions/{candidate}', query, Submission)

    def add_measurement(self, candidate: CandidateId, body: Report) -> MeasurementAdded:
        return self.post(f'/v1/candidates/{candidate}/measurements', body, MeasurementAdded)

    def candidate(self, candidate: CandidateId) -> CandidateDetail:
        return self._get(f'/v1/candidates/{candidate}', Params(), CandidateDetail)

    def evaluations(self) -> EvaluationQueue:
        return self._get('/v1/evaluations', Params(), EvaluationQueue)

    def cancel_evaluation(self, candidate: CandidateId) -> CandidateAnswer:
        return self.post(f'/v1/candidates/{candidate}/evaluation/cancel', None, CandidateAnswer)
