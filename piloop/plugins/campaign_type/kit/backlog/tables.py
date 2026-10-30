"""The tables of the ``backlog`` component: ideas, their links, and the techniques that a past scout found."""

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from piloop.db.models import NOW, Base, Campaign, EventModel, Tracked, by_value, text_checks, utcnow
from piloop.ids import CampaignId
from piloop.plugins.campaign_type.kit.backlog.ids import IdeaId, IdeaStatus, LinkKind
from piloop.plugins.campaign_type.kit.candidates.ids import CandidateId
from piloop.plugins.campaign_type.kit.evaluation.ids import TrackName

EventModel.retire('scout.requested')  # 0.1.0 requested a scout sweep with it; piloop has no scout now

if TYPE_CHECKING:  # runs need the backlog: only the relationship names them
    from piloop.plugins.campaign_type.kit.runs.tables import Run


# the types of its columns, beside core's in Base.type_annotation_map
Base.registry.update_type_annotation_map(
    {
        CandidateId: Integer,
        IdeaId: Integer,
        IdeaStatus: by_value(IdeaStatus),
        LinkKind: by_value(LinkKind),
        TrackName: String,
    }
)


class Idea(Tracked, Base):
    """Something worth trying: a card on the board (``IdeaStatus``). A worker claims an open idea and tries it.

    Anyone can insert an idea: a human, the lead, or a worker that claims it at once (``piloop idea propose``).
    ``after`` links (``IdeaLink``) hold an idea until every prerequisite is ``done`` or ``dropped``: a held idea cannot
    be claimed. A prerequisite done without a keep frees it too, and nothing changes its status. Cycles are
    refused (``ops.link``).
    """

    __tablename__ = 'ideas'
    __table_args__ = (
        UniqueConstraint('campaign_id', 'title'),
        # the queue: the pass and the widget read the ``ready`` ideas of one campaign in priority order
        Index('ix_ideas_campaign_status_priority', 'campaign_id', 'status', 'priority'),
        *text_checks('ideas', 'title', 'hypothesis', 'created_by', 'track', nullable=('plan', 'provenance')),
    )

    id: Mapped[IdeaId] = mapped_column(primary_key=True)
    campaign_id: Mapped[CampaignId] = mapped_column(ForeignKey('campaigns.id', ondelete='CASCADE'))
    title: Mapped[str]
    """Unique in a campaign, so two agents that pick the same title fail here and do not duplicate the idea."""
    hypothesis: Mapped[str]
    """Why this idea should improve the primary metric."""
    track: Mapped[TrackName]
    """The track (objective) the idea works on, by its name in ``piloop.toml``: a primary's ``track``, or else the
    primary's name. Its runs are judged on that track's primary, with the primary of every other track as a guardrail,
    and their brief has the track's addendum. A claim refuses an idea whose track is not in ``piloop.toml``."""
    plan: Mapped[str | None]
    """Concrete steps for the implementer. They are appended to the brief."""
    status: Mapped[IdeaStatus] = mapped_column(default=IdeaStatus.OPEN, server_default=IdeaStatus.OPEN.value)
    priority: Mapped[int] = mapped_column(default=2, server_default=text('2'))
    """0 (urgent) to 4 (someday). The board lists the lowest number first."""
    created_by: Mapped[str]
    """Who proposed it: ``human``, ``orchestrator``, or the agent's signature (``research#12``, ``implementer#40``,
    ``watchdog#7``). The part before ``#`` is the idea's source in the count of keeps by source."""
    provenance: Mapped[str | None]
    """The finding that led to the idea, with ids or a URL. For example ``finding comment#81: timeouts lose the last
    rows``, ``loss table workspace/research/losses.md`` or ``arXiv 2305.11860 via exa``."""
    created_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=NOW)

    campaign: Mapped[Campaign] = relationship()
    runs: Mapped[list[Run]] = relationship(back_populates='idea', order_by='Run.id')
    links: Mapped[list[IdeaLink]] = relationship(
        foreign_keys='IdeaLink.idea_id',
        back_populates='idea',
        order_by='IdeaLink.target_id',
        cascade='all, delete-orphan',
        passive_deletes=True,
    )
    """The links from this idea (it is the ``idea_id``). Query ``IdeaLink.target_id`` for the links to it."""

    def targets(self, kind: LinkKind) -> list[Idea]:
        """The ideas that this one links to with ``kind``, by id."""
        return [link.target for link in self.links if link.kind is kind]

    @property
    def after(self) -> list[Idea]:
        """The prerequisites. Each one must be ``done`` or ``dropped`` before this idea can be claimed."""
        return self.targets(LinkKind.AFTER)


class IdeaLink(Base):
    """A directed link between two ideas of one campaign: ``idea_id`` comes *after* ``target_id``.

    Links are many-to-many in both directions. An idea may wait for several prerequisites, and several ideas may wait
    for it. Rows are inserted and
    deleted, never updated, so they have no ``updated_at`` or ``updated_by``. A CHECK refuses a link from an idea to
    itself, and a trigger refuses a link across campaigns. When either idea is deleted, the link is deleted too. To add
    a kind of link, add a ``LinkKind`` member (which changes the CHECK) and a rule for it in
    ``piloop/plugins/campaign_type/kit/backlog/ideas.py``.
    """

    __tablename__ = 'idea_links'
    __table_args__ = (
        CheckConstraint('idea_id != target_id', name='idea_links_not_self'),
        Index('ix_idea_links_target', 'target_id', 'kind'),
    )

    idea_id: Mapped[IdeaId] = mapped_column(ForeignKey('ideas.id', ondelete='CASCADE'), primary_key=True)
    target_id: Mapped[IdeaId] = mapped_column(ForeignKey('ideas.id', ondelete='CASCADE'), primary_key=True)
    kind: Mapped[LinkKind] = mapped_column(primary_key=True)

    idea: Mapped[Idea] = relationship(foreign_keys=[idea_id], back_populates='links')
    target: Mapped[Idea] = relationship(foreign_keys=[target_id])


class Technique(Base):
    """The technique library: outside techniques that a scout sweep of piloop 0.5 or earlier found. One row per link.

    piloop has no scout now, and nothing writes the table: its rows stay readable with SQL.
    """

    __tablename__ = 'techniques'
    __table_args__ = (
        UniqueConstraint('campaign_id', 'url'),
        *text_checks('techniques', 'title', 'mechanism', 'url', 'created_by'),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    campaign_id: Mapped[CampaignId] = mapped_column(ForeignKey('campaigns.id', ondelete='CASCADE'))
    title: Mapped[str]
    """The technique, in a few words."""
    mechanism: Mapped[str]
    """One line: how it works, and the evidence for it (a number, code, an ablation)."""
    url: Mapped[str]
    """The primary source: a post, repo or paper. Unique in a campaign, so the same link is never filed twice."""
    created_by: Mapped[str]
    """Who filed it: the scout (``scout#<agent session id>``) or someone else."""
    created_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=NOW)
