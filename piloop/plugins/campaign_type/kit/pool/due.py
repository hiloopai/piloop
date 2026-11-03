"""What the daemon evaluates by itself, beside what runs submit.

That is the baseline, the best that an evaluator change left, what someone asked for, and new references.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from piloop.db.models import Campaign
from piloop.ids import CampaignStatus
from piloop.plugins.campaign_type.kit.candidates.ids import Origin
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.evaluation.measure import measured, requested
from piloop.plugins.campaign_type.kit.pool.pool import Pool, baseline, carried, confirm_runs


def needs(s: Session, campaign: Campaign, candidate: Candidate) -> int:
    """How many more evaluations ``candidate`` needs under the current evaluator to be confirmed.

    0 once it is confirmed, and once that many of its evaluations failed: a fault of the evaluator is not fixed by
    running it again (``hold_on_failed_baseline``).
    """
    ev = campaign.evaluator_id
    mine = [m for m in candidate.measurements if m.evaluator_id == ev and m.rung is None]
    want = confirm_runs(s, campaign, ev)
    if sum(m.error is not None for m in mine) >= want:
        return 0
    return max(want - sum(m.error is None for m in mine), 0)


def unready(s: Session, campaign: Campaign) -> list[Candidate]:
    """What runs wait for: the baseline and the ``carried`` best, while the current evaluator has measured neither."""
    found = [c for c in (baseline(s, campaign), carried(s, campaign)) if c is not None]
    return [c for c in found if not measured(c, campaign.evaluator_id)]


def due(s: Session, campaign: Campaign) -> list[Candidate]:
    """Return what the daemon evaluates with the current evaluator, most urgent first, each once.

    1. The ``carried`` best, until it is confirmed (``needs``): it is the best again once the new evaluator scores it.
    2. The baseline, until it is confirmed: runs wait for it, and its evaluations show whether the evaluator is
       deterministic.
    3. Each candidate that someone asked to measure again (``requested``), oldest request first.
    4. Each candidate that the sealed split is due to score (``Pool.seal_due``), such as the would-be best after a new
       sealed command: its visible evaluations stand.
    5. Each reference that no evaluator measured: a new import.

    A stopped campaign gets only 3: what someone asked for, to review the campaign. Nothing else is measured again
    unasked: after an evaluator change, a candidate keeps the scores of the old evaluator until it is submitted again or
    someone asks (``request_remeasure``).
    """
    asked = [c for c, _ in requested(s, campaign)]
    if campaign.status is not CampaignStatus.ACTIVE:
        return list(dict.fromkeys(asked))
    firsts = [c for c in (carried(s, campaign), baseline(s, campaign)) if c is not None and needs(s, campaign, c)]
    new = select(Candidate).where(
        Candidate.campaign_id == campaign.id, Candidate.origin == Origin.REFERENCE, ~Candidate.measurements.any()
    )
    pool = Pool.of(s, campaign, campaign.config.all_tracks[0])
    sealing = [s.get_one(Candidate, c) for c in pool.ranked_all() if pool.seal_due(c)]
    return list(dict.fromkeys([*firsts, *asked, *sealing, *s.scalars(new.order_by(Candidate.id))]))
