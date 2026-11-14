"""The table of the ``candidates`` component: the versions of a campaign's repo, and their lineage."""

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from piloop.db.models import NOW, Base, Tracked, by_value, text_checks, utcnow
from piloop.ids import CampaignId, Revision
from piloop.plugins.campaign_type.kit.backlog.ids import IdeaId
from piloop.plugins.campaign_type.kit.candidates.ids import CandidateId, Origin
from piloop.plugins.campaign_type.kit.runs.ids import RunId

if TYPE_CHECKING:  # evaluation, runs and the backlog need candidates: only the relationships name them
    from piloop.plugins.campaign_type.kit.backlog.tables import Idea
    from piloop.plugins.campaign_type.kit.evaluation.tables import Cancel, Measurement
    from piloop.plugins.campaign_type.kit.runs.tables import Run
from sqlalchemy import Select, select

from piloop.db.models import Campaign

# the types of its columns, beside core's in Base.type_annotation_map
Base.registry.update_type_annotation_map(
    {
        CandidateId: Integer,
        IdeaId: Integer,
        Origin: by_value(Origin),
    }
)


class Candidate(Tracked, Base):
    """A distinct state of the campaign's code. The git tree identifies it, so equal contents are one candidate.

    The baseline has no run and no parent. A reference has neither (``piloop candidate import`` makes one from the
    tree of any commit, pinned at ``refs/piloop/<campaign>/reference/<id>``). An implementer committed every other candidate,
    and it descends from the candidate that its run started from. When the last commit of a run reverts to its base,
    the run produces nothing new: the commit resolves to the base candidate. Measurements score candidates into the
    campaign's pool (``piloop.plugins.campaign_type.kit.pool``).
    """

    __tablename__ = 'candidates'
    __table_args__ = (
        UniqueConstraint('campaign_id', 'tree_sha'),
        # the candidates of a run (every history row), and the FK action when a run is deleted
        Index('ix_candidates_run', 'run_id'),
        # the FK check on every delete of a candidate (deleting a campaign deletes thousands)
        Index('ix_candidates_parent', 'parent_id'),
        *text_checks('candidates', nullable=('summary', 'excluded_reason', 'excluded_by')),
        CheckConstraint(
            '(excluded_reason IS NULL) = (excluded_by IS NULL)', name='candidates.an exclusion has its author'
        ),
        {'sqlite_autoincrement': True},
    )

    id: Mapped[CandidateId] = mapped_column(primary_key=True)
    campaign_id: Mapped[CampaignId] = mapped_column(ForeignKey('campaigns.id', ondelete='CASCADE'))
    run_id: Mapped[RunId | None] = mapped_column(ForeignKey('runs.id', ondelete='SET NULL'))
    idea_id: Mapped[IdeaId | None] = mapped_column(ForeignKey('ideas.id', ondelete='SET NULL'))
    """The idea of its run's claim that it was made for: the one that its submission names (``piloop submit --idea``),
    and for a version that the run never submitted, the run's newest claim then. NULL for the baseline, a reference, a
    merge, and a version of a run that claimed nothing."""
    parent_id: Mapped[CandidateId | None] = mapped_column(ForeignKey('candidates.id'))
    """The candidate that this one comes from."""
    tree_sha: Mapped[str]
    """The hash of the contents. Two commits with the same tree are the same candidate."""
    revision: Mapped[Revision]
    """The vcs provider's name for one version that has this tree, which it keeps (``Repository.keep``)."""
    task_id: Mapped[str | None]
    """The task selected by this revision. Empty when unlabelled, NULL until an older candidate is first evaluated."""
    summary: Mapped[str | None]
    """The first line of what the version says about itself."""
    origin: Mapped[Origin] = mapped_column(default=Origin.RUN, server_default=Origin.RUN.value)
    excluded_reason: Mapped[str | None]
    """Why someone excluded it from being best (``piloop candidate exclude``), such as a gamed evaluation. NULL
    otherwise."""
    excluded_by: Mapped[str | None]
    """Who excluded it."""
    created_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=NOW)

    run: Mapped[Run | None] = relationship(back_populates='candidates', foreign_keys=[run_id])
    idea: Mapped[Idea | None] = relationship()
    parent: Mapped[Candidate | None] = relationship(remote_side=[id], foreign_keys=[parent_id])
    measurements: Mapped[list[Measurement]] = relationship(back_populates='candidate', order_by='Measurement.id')
    cancels: Mapped[list[Cancel]] = relationship(back_populates='candidate', cascade='all, delete-orphan')


def produced(campaign: Campaign) -> Select[Candidate]:
    """Select the candidates of the campaign: the baseline and the commits of its runs, not imported references."""
    return select(Candidate).where(Candidate.campaign_id == campaign.id, Candidate.origin != Origin.REFERENCE)
