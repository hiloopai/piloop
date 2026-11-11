"""Infrastructure failure streaks shared by candidates that select the same task."""

from contextlib import closing
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from piloop.db.models import Campaign
from piloop.ids import CampaignId
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.evaluation.tables import EvaluationTaskFailure
from piloop.plugins.vcs.contract import VCS, VcsError


def identify(s: Session, candidate: Candidate) -> None:
    """Fill the task of a pre-upgrade candidate from its immutable revision on first use."""
    if candidate.task_id is None:
        campaign = s.get_one(Campaign, candidate.campaign_id)
        try:
            with closing(VCS.selected(campaign.config.plugins).open(Path(campaign.path))) as repo:
                candidate.task_id = repo.version(candidate.revision).task_id or ''
        except VcsError:
            pass  # an unavailable revision can still fail and be held by candidate id


def key(candidate: Candidate) -> str:
    """Untagged submissions have independent retry budgets, even if a real task is named 'candidate:1'."""
    return f'task:{candidate.task_id}' if candidate.task_id else f'candidate:{candidate.id}'


def held(s: Session, candidate: Candidate) -> bool:
    """Whether the scheduler must leave this candidate's task alone."""
    row = s.get(EvaluationTaskFailure, (candidate.campaign_id, key(candidate)))
    return row is not None and row.held


def held_keys(s: Session, campaign_id: CampaignId) -> set[str]:
    """The tasks to skip in a scheduling pass for this campaign."""
    return set(
        s.scalars(
            select(EvaluationTaskFailure.task_key).where(
                EvaluationTaskFailure.campaign_id == campaign_id, EvaluationTaskFailure.held
            )
        )
    )


def reset(s: Session, candidate: Candidate, *, resume: bool = False) -> None:
    """A completed evaluation breaks a streak. Only an explicit resume lifts an established hold."""
    remove = delete(EvaluationTaskFailure).where(
        EvaluationTaskFailure.campaign_id == candidate.campaign_id, EvaluationTaskFailure.task_key == key(candidate)
    )
    s.execute(remove.where(EvaluationTaskFailure.held.is_(resume)))
