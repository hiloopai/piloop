"""A candidate as the API answers it."""

from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.candidates.wire import CandidateView


def candidate_view(c: Candidate) -> CandidateView:
    """``c`` as the API answers it."""
    return CandidateView(id=c.id, summary=c.summary, origin=c.origin, excluded_reason=c.excluded_reason)
