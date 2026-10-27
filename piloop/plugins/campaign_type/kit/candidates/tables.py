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
from piloop.plugins.campaign_type.kit.candidates.ids import JUDGED, CandidateId, Cause, Origin, Outcome
from piloop.plugins.campaign_type.kit.runs.ids import RunId

if TYPE_CHECKING:  # evaluation and runs need candidates: only the relationships name them
    from piloop.plugins.campaign_type.kit.evaluation.tables import Measurement
    from piloop.plugins.campaign_type.kit.runs.tables import Run


# the types of its columns, beside core's in Base.type_annotation_map
Base.registry.update_type_annotation_map(
    {
        CandidateId: Integer,
        Cause: by_value(Cause),
        Origin: by_value(Origin),
        Outcome: by_value(Outcome),
    }
)


class Candidate(Tracked, Base):
    """A distinct state of the campaign's code. The git tree identifies it, so equal contents are one candidate.

    The baseline has no run and no parent. A reference has neither (``piloop candidate import`` makes one from the
    tree of any commit, pinned at ``refs/piloop/<campaign>/reference/<id>``). An implementer committed every other candidate,
    and it descends from the candidate that its run started from. When the last commit of a run reverts to its base,
    the run produces nothing new: the commit resolves to the base candidate. Measurements score candidates, and the
    verdict judges them against a bar.
    """

    __tablename__ = 'candidates'
    __table_args__ = (
        UniqueConstraint('campaign_id', 'tree_sha'),
        Index('ix_candidates_campaign_verdict', 'campaign_id', 'verdict'),
        # the candidates of a run (every history row), and the FK action when a run is deleted
        Index('ix_candidates_run', 'run_id'),
        # the FK check on every delete of a candidate (deleting a campaign deletes thousands)
        Index('ix_candidates_parent', 'parent_id'),
        *text_checks('candidates', nullable=('summary', 'verdict_by', 'verdict_reason')),
        CheckConstraint(
            f'verdict IS NULL OR origin IN ({", ".join(repr(o.value) for o in sorted(JUDGED))})',
            name='candidates.verdict needs a judged origin',
        ),
        CheckConstraint(
            f"cause IS NULL OR verdict = '{Outcome.INTERESTING}'", name='candidates.cause needs interesting'
        ),
    )

    id: Mapped[CandidateId] = mapped_column(primary_key=True)
    campaign_id: Mapped[CampaignId] = mapped_column(ForeignKey('campaigns.id', ondelete='CASCADE'))
    run_id: Mapped[RunId | None] = mapped_column(ForeignKey('runs.id', ondelete='SET NULL'))
    parent_id: Mapped[CandidateId | None] = mapped_column(ForeignKey('candidates.id'))
    """The candidate that this one comes from."""
    tree_sha: Mapped[str]
    """The hash of the contents. Two commits with the same tree are the same candidate."""
    revision: Mapped[Revision]
    """The vcs provider's name for one version that has this tree, which it keeps (``Repository.keep``)."""
    summary: Mapped[str | None]
    """The first line of what the version says about itself."""
    origin: Mapped[Origin] = mapped_column(default=Origin.RUN, server_default=Origin.RUN.value)
    verdict: Mapped[Outcome | None]
    """The judgement against the candidate's bar. NULL while it is pending, and on a candidate whose origin is not
    judged."""
    cause: Mapped[Cause | None]
    verdict_by: Mapped[str | None]
    verdict_reason: Mapped[str | None]
    """The reason of the verdict, for a human."""
    created_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=NOW)

    run: Mapped[Run | None] = relationship(back_populates='candidates', foreign_keys=[run_id])
    parent: Mapped[Candidate | None] = relationship(remote_side=[id], foreign_keys=[parent_id])
    measurements: Mapped[list[Measurement]] = relationship(back_populates='candidate', order_by='Measurement.id')
