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

CAMPAIGN = 'campaign'
"""The shared failure count for submissions without a task ID. A hold on this key covers all evaluations."""


def identify(s: Session, candidate: Candidate) -> None:
    """Fill the task of a pre-upgrade candidate from its immutable revision on first use."""
    if candidate.task_id is None:
        campaign = s.get_one(Campaign, candidate.campaign_id)
        try:
            with closing(VCS.selected(campaign.config.plugins).open(Path(campaign.path))) as repo:
                candidate.task_id = repo.version(candidate.revision).task_id or ''
        except VcsError:
            pass  # an unavailable revision uses the same fallback as a missing TASK


def key(candidate: Candidate) -> str:
    """Tagged submissions share a counter per task, untagged ones share one per campaign."""
    return f'task:{candidate.task_id}' if candidate.task_id else CAMPAIGN


def held(s: Session, candidate: Candidate) -> bool:
    """Whether the scheduler must leave this candidate's task alone."""
    matching = select(EvaluationTaskFailure).where(
        EvaluationTaskFailure.campaign_id == candidate.campaign_id,
        EvaluationTaskFailure.task_key.in_((CAMPAIGN, key(candidate))),
        EvaluationTaskFailure.held,
    )
    return bool(s.scalar(select(matching.exists())))


def held_keys(s: Session, campaign_id: CampaignId) -> set[str]:
    """The tasks to skip in a scheduling pass for this campaign."""
    return set(
        s.scalars(
            select(EvaluationTaskFailure.task_key).where(
                EvaluationTaskFailure.campaign_id == campaign_id, EvaluationTaskFailure.held
            )
        )
    )


def all_tasks_held(campaign: Campaign, held_keys: set[str]) -> bool:
    """Whether every route in the operator's TASKS.txt is held, including an explicitly empty roster."""
    roster = Path(campaign.path) / 'TASKS.txt'
    if roster.is_symlink() or not roster.is_file():
        return False
    try:
        task_ids = [line.strip() for line in roster.read_text(encoding='utf-8').splitlines() if line.strip()]
    except (OSError, UnicodeError):
        return False
    return all(f'task:{task_id}' in held_keys for task_id in task_ids)


def reset(s: Session, candidate: Candidate, *, resume: bool = False) -> None:
    """A completed evaluation breaks a streak. Only an explicit resume lifts an established hold."""
    if resume and not candidate.task_id:
        return  # only a campaign resume releases the shared evaluator hold
    remove = delete(EvaluationTaskFailure).where(
        EvaluationTaskFailure.campaign_id == candidate.campaign_id, EvaluationTaskFailure.task_key == key(candidate)
    )
    s.execute(remove.where(EvaluationTaskFailure.held.is_(resume)))
