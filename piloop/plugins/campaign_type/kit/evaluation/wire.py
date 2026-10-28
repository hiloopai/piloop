"""What the evaluation's routes take and answer, and the client that calls them."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import ConfigDict, Field

from piloop.api.client import Client
from piloop.api.contract import Params
from piloop.api.views import View
from piloop.plugins.campaign_type.kit.candidates.ids import CandidateId
from piloop.plugins.campaign_type.kit.candidates.wire import CandidateAnswer
from piloop.plugins.campaign_type.kit.evaluation.ids import MeasurementId

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


class EvaluationClient(Client):
    """The calls of the evaluation's routes: a measurement by hand, the queue, and an evaluation's cancel."""

    def add_measurement(self, candidate: CandidateId, body: Report) -> MeasurementAdded:
        return self.post(f'/v1/candidates/{candidate}/measurements', body, MeasurementAdded)

    def evaluations(self) -> EvaluationQueue:
        return self._get('/v1/evaluations', Params(), EvaluationQueue)

    def cancel_evaluation(self, candidate: CandidateId) -> CandidateAnswer:
        return self.post(f'/v1/candidates/{candidate}/evaluation/cancel', None, CandidateAnswer)
