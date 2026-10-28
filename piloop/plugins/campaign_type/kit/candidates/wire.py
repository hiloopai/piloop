"""What the candidates' routes take and answer, and the client that calls them."""

from piloop.api.client import Client, segment
from piloop.api.contract import Params
from piloop.api.views import View
from piloop.plugins.campaign_type.kit.candidates.ids import CandidateId, Cause


class ImportBody(Params):
    """A version of the campaign's repo to register as a reference candidate."""

    version: str
    """A version as the campaign's vcs names one: a revision, a branch, a tag."""
    summary: str | None = None
    """A summary of the candidate. Default: the version's own summary."""


class CandidateView(View):
    """A candidate, as the change that wrote it left it."""

    id: CandidateId
    summary: str | None
    label: str
    """The verdict as a person reads it: the verdict, ``pending``, or the origin of a candidate that is never judged."""
    cause: Cause | None
    verdict_reason: str | None


class CandidateAnswer(View):
    """A candidate that a change wrote, read at ``cursor``."""

    cursor: int
    candidate: CandidateView


class CandidatesClient(Client):
    """The calls of the candidates' route: an imported reference."""

    def import_candidate(self, campaign: str, body: ImportBody) -> CandidateAnswer:
        return self.post(f'/v1/campaigns/{segment(campaign)}/candidates', body, CandidateAnswer)
