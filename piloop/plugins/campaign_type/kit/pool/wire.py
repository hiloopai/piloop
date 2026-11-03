"""What the pool's routes take and answer, and the client that calls them."""

from pydantic import Field

from piloop.api.client import Client
from piloop.api.contract import Params, Reasoned
from piloop.plugins.campaign_type.kit.candidates.ids import CandidateId
from piloop.plugins.campaign_type.kit.candidates.wire import CandidateAnswer
from piloop.plugins.campaign_type.kit.evaluation.wire import REPEATS


class RemeasureBody(Params):
    """How to measure a candidate again."""

    repeats: int = Field(REPEATS, ge=1)
    """How many more evaluations of the candidate."""


class Remeasured(CandidateAnswer):
    """A candidate that the daemon measures again."""

    held: str | None
    """What the daemon waits for before it measures, such as a resume. Null: it measures soon."""


class ExcludeBody(Reasoned):
    """Why a candidate may not be best, such as an evaluation that it gamed."""


class PoolClient(Client):
    """The calls of the pool's routes: a remeasure, and an exclusion."""

    def remeasure(self, candidate: CandidateId, body: RemeasureBody) -> Remeasured:
        return self.post(f'/v1/candidates/{candidate}/remeasure', body, Remeasured)

    def exclude(self, candidate: CandidateId, body: ExcludeBody) -> CandidateAnswer:
        return self.post(f'/v1/candidates/{candidate}/exclude', body, CandidateAnswer)
