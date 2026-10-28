"""What the verdicts' routes take and answer, and the client that calls them."""

from pydantic import Field

from piloop.api.client import Client
from piloop.api.contract import Params, Reasoned
from piloop.api.views import View
from piloop.plugins.campaign_type.kit.backlog.ids import IdeaId
from piloop.plugins.campaign_type.kit.candidates.ids import CandidateId, Outcome
from piloop.plugins.campaign_type.kit.candidates.wire import CandidateAnswer
from piloop.plugins.campaign_type.kit.evaluation.wire import REPEATS
from piloop.plugins.campaign_type.kit.runs.ids import RunId


class RemeasureBody(Params):
    """How to measure a candidate again."""

    repeats: int = Field(REPEATS, ge=1)
    """How many pairs of the candidate and its bar."""


class VerdictBody(Reasoned):
    """A candidate's verdict, set by hand."""

    verdict: Outcome


class Remeasured(CandidateAnswer):
    """A candidate that the daemon measures again."""

    bar: CandidateId | None
    """The bar that it is measured next to. Null: alone."""
    held: str | None
    """What the daemon waits for before it measures, such as a resume. Null: it measures soon."""
    rejudged: bool
    """Whether the daemon judges the candidate again after the measurements. Else its verdict stays."""


class Merge(View):
    """A keep off the head, merged onto it: a candidate that the daemon measures and judges against that head."""

    candidate: CandidateId
    head: CandidateId


class CancelledRun(View):
    """An open run from the head that a verdict set by hand replaced: cancelled, and its idea ready again once it ends."""

    run: RunId
    idea: IdeaId
    base: CandidateId
    """The head that it started from."""


class VerdictChanged(CandidateAnswer):
    """A candidate whose verdict was set by hand."""

    was: str
    """The verdict that it had, as ``label`` reads it."""
    merge: Merge | None
    cancelled: list[CancelledRun] = []
    """The open runs from the head that it replaced, oldest first."""


class VerdictsClient(Client):
    """The calls of the verdicts' routes: a remeasure next to the bar, and a verdict set by hand."""

    def remeasure(self, candidate: CandidateId, body: RemeasureBody) -> Remeasured:
        return self.post(f'/v1/candidates/{candidate}/remeasure', body, Remeasured)

    def set_verdict(self, candidate: CandidateId, body: VerdictBody) -> VerdictChanged:
        return self.post(f'/v1/candidates/{candidate}/verdict', body, VerdictChanged)
