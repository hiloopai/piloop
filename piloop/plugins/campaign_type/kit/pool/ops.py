"""The operations on the pool that the API and the CLI run: an exclusion."""

from sqlalchemy.orm import Session

from piloop.actor import Actor
from piloop.campaign.ops import Refused, existing, required_reason
from piloop.plugins.campaign_type.kit.candidates.ids import CandidateId, Origin
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate


def exclude(s: Session, candidate_id: CandidateId, reason: str, *, actor: Actor) -> Candidate:
    """Take a candidate out of the race for the best of its track, with the reason, signed by ``actor``.

    Use it for a candidate that gamed the visible evaluation, say. Its scores stay in the pool, and a run may still
    start from it. The ``candidate.excluded_reason`` event records it. To take it back, set ``excluded_reason`` and
    ``excluded_by`` to NULL. The caller commits.

    Raises:
        Refused: the candidate is missing, the reason is blank, or it is the baseline, which may always be best.
    """
    candidate = existing(s, Candidate, candidate_id, 'candidate')
    why = required_reason(reason)
    if candidate.origin is Origin.BASELINE:
        raise Refused(f'candidate#{candidate.id} is the baseline, which may always be best')
    candidate.excluded_reason, candidate.excluded_by = why, actor.signature
    return candidate
