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
    """Something worth trying. The scheduler turns ready ideas into runs.

    Anyone can insert an idea: a human, the orchestrator or a research agent. Ideas link to ideas of the same campaign
    through ``idea_links`` (``IdeaLink``), many-to-many in both directions.

    ``after``: the idea is blocked (``ready``, but never taken) until every prerequisite is ``done``. Its run then
    starts from the head like any other run, and the head holds every keep on the trunk that the prerequisites made.
    An idea can pin a ``base_candidate_id`` instead, for example the branch keep of a prerequisite. A prerequisite
    that ends without a keep (``parked``, ``rejected``, or ``done`` after runs that kept nothing) never releases its
    dependents: the daemon parks them with an escalation, and the orchestrator or a human deletes the link or sets
    them ready again. Cycles of prerequisites are not detected: the ideas in a cycle never run and show as blocked.

    ``supersedes``: the idea is a sharper version of the targets. A research sweep uses it to refine rejected, parked
    or done ideas instead of proposing near-duplicates. The daemon rejects a superseded idea that never ran (it has
    no run and is still ``seed``, ``proposed`` or ``ready``). A superseded idea that ran keeps its status and results.
    """

    __tablename__ = 'ideas'
    __table_args__ = (
        UniqueConstraint('campaign_id', 'title'),
        # the queue: the pass and the widget read the ``ready`` ideas of one campaign in priority order
        Index('ix_ideas_campaign_status_priority', 'campaign_id', 'status', 'priority'),
        *text_checks(
            'ideas', 'title', 'hypothesis', 'created_by', 'track', nullable=('plan', 'harness', 'provenance', 'family')
        ),
    )

    id: Mapped[IdeaId] = mapped_column(primary_key=True)
    campaign_id: Mapped[CampaignId] = mapped_column(ForeignKey('campaigns.id', ondelete='CASCADE'))
    title: Mapped[str]
    """Unique in a campaign, so two research sweeps that pick the same title fail here and do not duplicate the idea."""
    hypothesis: Mapped[str]
    """Why this idea should improve the primary metric."""
    track: Mapped[TrackName]
    """The track (objective) the idea works on, by its name in ``piloop.toml``: a primary's ``track``, or else the
    primary's name. Its runs are judged on that track's primary, with the primary of every other track as a guardrail,
    and their brief has the track's addendum. The scheduler parks an idea whose track is not in ``piloop.toml``, with
    a note that says why."""
    plan: Mapped[str | None]
    """Concrete steps for the implementer. They are appended to the brief."""
    status: Mapped[IdeaStatus] = mapped_column(default=IdeaStatus.READY, server_default=IdeaStatus.READY.value)
    priority: Mapped[int] = mapped_column(default=2, server_default=text('2'))
    """0 (urgent) to 4 (someday). The scheduler takes the lowest number first."""
    harness: Mapped[str | None]
    """The harness for this idea. None lets the scheduler use the campaign's harnesses (``[plugins.harness] use``) in turn."""
    base_candidate_id: Mapped[CandidateId | None]
    """A deliberate branch: the ``candidates.id`` to start from, instead of the head at the time the run starts. NULL
    unless the idea gives a reason for that starting point. Never pin the current head by default: when the idea
    runs, that tree is out of date. The ``run.queued`` event of a pinned run records both bases. A plain integer, as
    on runs."""
    max_runs: Mapped[int] = mapped_column(default=2, server_default=text('2'))
    """The number of attempts allowed. A crashed run is tried again until this number is reached."""
    created_by: Mapped[str]
    """Who proposed it: ``human``, ``orchestrator``, or the agent's signature (``research#12``, ``implementer#40``,
    ``watchdog#7``). The part before ``#`` is the idea's source in the count of keeps by source."""
    provenance: Mapped[str | None]
    """The finding that led to the idea, with ids or a URL. For example ``finding comment#81: timeouts lose the last
    rows``, ``loss table workspace/research/losses.md`` or ``arXiv 2305.11860 via exa``."""
    family: Mapped[str | None]
    """The mechanism family: a short lowercase name for ideas that try the same mechanism (``feature engineering``,
    ``ensembling``, ``test-time training``). The research pack shows what each family produced."""
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
        """The prerequisites. Each one must be ``done`` before this idea runs."""
        return self.targets(LinkKind.AFTER)

    @property
    def supersedes(self) -> list[Idea]:
        """The older ideas that this one is a sharper version of."""
        return self.targets(LinkKind.SUPERSEDES)


class IdeaLink(Base):
    """A directed link between two ideas of one campaign: ``idea_id`` comes *after* or *supersedes* ``target_id``.

    Links are many-to-many in both directions. An idea may wait for several prerequisites, several ideas may wait for
    it, and a sharper version may replace several earlier ideas. One pair may have both kinds. Rows are inserted and
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
