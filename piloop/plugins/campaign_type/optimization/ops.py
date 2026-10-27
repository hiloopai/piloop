"""What optimization does across its components: a note on a candidate keeps it as a lead and files a seed."""

from collections.abc import Sequence

from sqlalchemy.orm import Session

from piloop.actor import Actor
from piloop.campaign.ops import _get
from piloop.db.models import Campaign
from piloop.plugins.campaign_type.kit.backlog.ops import files, track_for
from piloop.plugins.campaign_type.kit.backlog.tables import Idea
from piloop.plugins.campaign_type.kit.candidates.ids import CandidateId, Cause, Outcome
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.evaluation.ids import TrackName
from piloop.plugins.campaign_type.kit.evaluation.ledger import (
    follow_up,
    track_of,
)
from piloop.plugins.campaign_type.kit.verdicts.ops import set_verdict


def note_candidate(
    s: Session,
    candidate_id: CandidateId,
    reason: str,
    *,
    actor: Actor,
    follow_ups: Sequence[str] = (),
    track: TrackName | None = None,
) -> tuple[Candidate, list[Idea]]:
    """Keep a regressed result in view as a lead (``set_verdict``, ``Cause.NOTE``), with a seed pinned to it per follow-up.

    The follow-ups are on ``track``, else on the track of the candidate's run. Returns the candidate and the new seeds.

    Raises:
        Refused: the candidate is missing or not a measured discard or interesting, the reason is blank, the actor
            files nothing and gave follow-ups, or the track is not the campaign's.
    """
    candidate = _get(s, Candidate, candidate_id, 'candidate')
    campaign = s.get_one(Campaign, candidate.campaign_id)
    if follow_ups:
        files(campaign, actor)
    set_verdict(s, candidate.id, Outcome.INTERESTING, cause=Cause.NOTE, reason=reason, actor=actor)
    on = track_for(campaign, track) if track is not None else track_of(campaign.config, candidate).name
    return candidate, follow_up(s, candidate, follow_ups, reason, actor, track=on)
