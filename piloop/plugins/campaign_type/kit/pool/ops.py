"""The operations on the pool: an exclusion, which the API and the CLI run, and the escalation of a rule that fails."""

from sqlalchemy.orm import Session

from piloop.actor import Actor
from piloop.campaign import escalations
from piloop.campaign.ops import Refused, existing, required_reason
from piloop.db.models import Campaign
from piloop.ids import TargetKind, TargetRef
from piloop.plugins.campaign_type.kit.candidates.ids import CandidateId, Origin
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.escalation.contract import Choice, EscalationView


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


def unanswered(s: Session, campaign: Campaign, candidate_id: CandidateId, error: str) -> None:
    """Escalate, once, that the best rule did not answer the case of a candidate. The caller commits.

    The best stays where it is, and the candidates after this one wait: piloop does not ask the rule this case again.
    """
    asked = EscalationView(
        headline=f'The best rule of {campaign.name} did not answer. The best stays where it is.',
        why=(
            f'piloop asked the best rule whether candidate#{candidate_id} may replace the best, and it could not'
            ' answer. piloop does not ask it the same case again.'
        ),
        choices=(
            Choice(
                id='A',
                label='Fix the rule, change its [plugins.best_rule] table, then `piloop campaign sync`.',
                result='piloop asks the fixed rule again, from the baseline.',
            ),
            Choice(
                id='B',
                label='Remove [plugins.best_rule], then `piloop campaign sync`.',
                result='The built-in rule `baseline` decides the best.',
            ),
        ),
        recommended='A',
        details=f'candidate#{candidate_id}: {error}',
    )
    escalations.escalate_once(s, TargetRef(TargetKind.CAMPAIGN, campaign.id), asked, priority=1)
