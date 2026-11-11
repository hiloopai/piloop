"""What the daemon evaluates by itself, beside what runs submit.

That is the baseline, the best that an evaluator change left, what someone asked for, and new references.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from piloop.db.models import Campaign
from piloop.ids import CampaignStatus
from piloop.plugins.campaign_type.kit.candidates.ids import Origin
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.evaluation import task_holds
from piloop.plugins.campaign_type.kit.evaluation.measure import requested, status
from piloop.plugins.campaign_type.kit.pool.pool import Pool, baseline, carried, confirm_runs


def firsts(s: Session, campaign: Campaign) -> list[Candidate]:
    """What runs wait for: the ``carried`` best, then the baseline. The daemon confirms both with the current evaluator.

    Runs start once the current evaluator answered both, or someone cancelled them (``Status.settled``).
    """
    return [c for c in (carried(s, campaign), baseline(s, campaign)) if c is not None]


def due(s: Session, campaign: Campaign) -> list[Candidate]:
    """Return what the daemon evaluates with the current evaluator, most urgent first, each once.

    1. The ``carried`` best, until it is confirmed (``Status.needs``): it is the best again once the new evaluator scores
       it.
    2. The baseline, until it is confirmed: runs wait for it, and its evaluations show whether the evaluator is
       deterministic.
    3. Each candidate that someone asked to measure again (``requested``), oldest request first.
    4. Each candidate that the sealed split is due to score (``Pool.seal_due``), such as the would-be best after a new
       sealed command: its visible evaluations stand.
    5. Each reference that no evaluator measured and nobody cancelled: a new import.

    A cancel stands (``Cancel``): what someone cancelled is not due again until someone asks.

    A stopped campaign gets only 3: what someone asked for, to review the campaign. Nothing else is measured again
    unasked: after an evaluator change, a candidate keeps the scores of the old evaluator until it is submitted again or
    someone asks (``request_remeasure``).
    """
    held = task_holds.held_keys(s, campaign.id)
    asked = [c for c, _ in requested(s, campaign) if task_holds.key(c) not in held]
    if campaign.status is not CampaignStatus.ACTIVE:
        return list(dict.fromkeys(asked))
    confirm = confirm_runs(s, campaign, campaign.evaluator_id)
    unconfirmed = [c for c in firsts(s, campaign) if status(c, campaign.evaluator_id).needs(confirm)]
    new = select(Candidate).where(
        Candidate.campaign_id == campaign.id,
        Candidate.origin == Origin.REFERENCE,
        ~Candidate.measurements.any(),
        ~Candidate.cancels.any(),
    )
    pool = Pool.of(s, campaign, campaign.config.all_tracks[0])
    sealing = [s.get_one(Candidate, c) for c in pool.ranked_all() if pool.seal_due(c)]
    candidates = dict.fromkeys([*unconfirmed, *asked, *sealing, *s.scalars(new.order_by(Candidate.id))])
    return [c for c in candidates if task_holds.key(c) not in held]
