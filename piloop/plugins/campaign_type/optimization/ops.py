"""What optimization does across its components: a note on a candidate, with follow-up ideas about it."""

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from piloop.actor import Actor
from piloop.campaign.ops import existing, required_reason
from piloop.db.models import Campaign, Comment
from piloop.ids import CommentKind, TargetRef
from piloop.plugins.campaign_type.kit.backlog.ids import IdeaStatus
from piloop.plugins.campaign_type.kit.backlog.ops import files, track_for
from piloop.plugins.campaign_type.kit.backlog.tables import Idea
from piloop.plugins.campaign_type.kit.candidates.ids import CANDIDATE, CandidateId
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.evaluation.ids import TrackName
from piloop.plugins.campaign_type.kit.pool.pool import track_of


def note_candidate(
    s: Session,
    candidate_id: CandidateId,
    reason: str,
    *,
    actor: Actor,
    follow_ups: Sequence[str] = (),
    track: TrackName | None = None,
) -> tuple[Candidate, list[Idea]]:
    """Note why a candidate is worth starting from, as a note on it, with an open idea about it per follow-up.

    The follow-ups are on ``track``, else on the track of the candidate's run. Returns the candidate and the new ideas.

    Raises:
        Refused: the candidate is missing, the reason is blank, the actor files nothing and gave follow-ups, or the
            track is not the campaign's.
    """
    candidate = existing(s, Candidate, candidate_id, 'candidate')
    why = required_reason(reason)
    campaign = s.get_one(Campaign, candidate.campaign_id)
    if follow_ups:
        files(campaign, actor)
    s.add(Comment(target=TargetRef(CANDIDATE, candidate.id), author=actor.signature, kind=CommentKind.NOTE, body=why))
    on = track_for(campaign, track) if track is not None else track_of(campaign.config, candidate).name
    return candidate, follow_up(s, candidate, follow_ups, why, actor, track=on)


def follow_up(
    s: Session, candidate: Candidate, titles: Sequence[str], why: str, actor: Actor, *, track: TrackName
) -> list[Idea]:
    """Add one open idea per title on ``track``, about ``candidate``. Return the new ideas.

    A title that the campaign already has gets `` (from candidate#<id>)``. A follow-up already filed from this candidate
    with that title, under either name and in any status, is not filed again: a dropped compose idea stays dropped
    when the candidate is noted again. Its run can start from the candidate (``--base``).
    """
    ideas: list[Idea] = []
    hypothesis = f'Follow-up of candidate#{candidate.id} ({candidate.summary or "no summary"})'
    provenance = f'candidate#{candidate.id}'
    for title in titles:
        names = (title, f'{title} (from {provenance})')
        taken = s.scalars(select(Idea).where(Idea.campaign_id == candidate.campaign_id, Idea.title.in_(names))).all()
        name = next((n for n in names if n not in {i.title for i in taken}), None)
        if name is None or any(i.provenance == provenance for i in taken):
            continue
        idea = Idea(
            campaign_id=candidate.campaign_id,
            title=name,
            hypothesis=f'{hypothesis}: {why}',
            status=IdeaStatus.OPEN,
            track=track,
            provenance=provenance,
            created_by=actor.signature,
        )
        s.add(idea)
        s.flush()
        ideas.append(idea)
    return ideas
