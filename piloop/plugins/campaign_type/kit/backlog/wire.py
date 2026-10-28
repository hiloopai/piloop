"""What the backlog's routes take and answer, and the client that calls them."""

from piloop.api.client import Client, segment
from piloop.api.contract import (
    Params,
    Priority,
    Reasoned,
)
from piloop.api.views import (
    View,
)
from piloop.ids import CampaignId
from piloop.plugins.campaign_type.kit.backlog.ids import IdeaId, IdeaStatus, LinkKind
from piloop.plugins.campaign_type.kit.candidates.ids import CandidateId
from piloop.plugins.campaign_type.kit.evaluation.ids import TrackName


class IdeaBody(Params):
    """A defined idea, which the daemon schedules in priority order once it is ready."""

    title: str
    hypothesis: str = ''
    """Why this should help, and by how much. Default: the title."""
    plan: str | None = None
    """The steps for the implementer."""
    provenance: str | None = None
    """What prompted the idea, with ids or a URL."""
    family: str | None = None
    """The mechanism family. Ideas that try the same mechanism share it."""
    priority: Priority = 2
    harness: str | None = None
    """The harness to use instead of the rotation."""
    base: CandidateId | None = None
    """The candidate to start from instead of the head. This makes a branch."""
    proposed: bool = False
    """Wait for a review before the daemon schedules the idea."""
    track: TrackName | None = None
    """The track it works on. Required when the campaign has more than one track."""
    after: tuple[IdeaId, ...] = ()
    """The ideas to wait for. The idea runs from the head when all are done."""
    supersedes: tuple[IdeaId, ...] = ()
    """The ideas that this idea refines."""


class SeedBody(Params):
    """A seed: a hunch with pointers, for research to develop into a defined idea or to reject."""

    hunch: str
    provenance: str
    """Pointers to what prompted the hunch: ids, paths or numbers."""
    base: CandidateId | None = None
    """The candidate to start from, when the hunch is about that tree."""
    track: TrackName | None = None
    """The track it works on. Required when the campaign has more than one track."""


class DevelopBody(Params):
    """What makes a seed a defined idea: each field that the implementer needs."""

    hypothesis: str
    """The mechanism, and why it should move the metric."""
    evidence: str
    """What supports it, with ids and paths."""
    check: str
    """The cheapest measurement that can prove it wrong, and the result that stops the run."""
    metric: str
    """The metric that decides success."""
    effect: str
    """The expected size of the effect in the metric's units, and why."""
    plan: str | None = None
    title: str | None = None
    """A clearer title. Default: the seed's."""
    family: str | None = None
    base: CandidateId | None = None
    priority: Priority | None = None
    """Default: the seed's priority."""
    proposed: bool = False
    after: tuple[IdeaId, ...] = ()
    supersedes: tuple[IdeaId, ...] = ()


class StatusBody(Reasoned):
    """An idea's new status."""

    status: IdeaStatus


class BaseBody(Reasoned):
    """The candidate that an idea's runs start from. Null: no pin, the head when the run starts."""

    base: CandidateId | None


class TrackBody(Reasoned):
    """The track to move an idea to."""

    track: TrackName


class IdeaView(View):
    """An idea, as the change that wrote it left it."""

    id: IdeaId
    campaign: CampaignId
    title: str
    status: IdeaStatus
    priority: int
    track: TrackName
    created_by: str


class IdeaAnswer(View):
    """An idea that a change wrote, read at ``cursor``."""

    cursor: int
    idea: IdeaView


class Defined(IdeaAnswer):
    """An idea that ``add`` or ``develop`` defined."""

    review: str | None
    """Why the idea waits for a review, as ``proposed`` although nobody asked: its author's role is held. Else null."""


class StatusChanged(IdeaAnswer):
    """An idea whose status changed."""

    was: IdeaStatus


class TrackChanged(IdeaAnswer):
    """An idea that moved to another track."""

    was: TrackName


class BacklogClient(Client):
    """The calls of the backlog's routes: ideas, seeds, links, bases, tracks and statuses."""

    def add_idea(self, campaign: str, body: IdeaBody) -> Defined:
        return self.post(f'/v1/campaigns/{segment(campaign)}/ideas', body, Defined)

    def file_seed(self, campaign: str, body: SeedBody) -> IdeaAnswer:
        return self.post(f'/v1/campaigns/{segment(campaign)}/seeds', body, IdeaAnswer)

    def develop(self, idea: IdeaId, body: DevelopBody) -> Defined:
        return self.post(f'/v1/ideas/{idea}/develop', body, Defined)

    def set_status(self, idea: IdeaId, body: StatusBody) -> StatusChanged:
        return self.post(f'/v1/ideas/{idea}/status', body, StatusChanged)

    def link(self, idea: IdeaId, kind: LinkKind, target: IdeaId, body: Reasoned) -> IdeaAnswer:
        return self._send('PUT', f'/v1/ideas/{idea}/links/{kind}/{target}', body, IdeaAnswer)

    def unlink(self, idea: IdeaId, kind: LinkKind, target: IdeaId, body: Reasoned) -> IdeaAnswer:
        return self._send('DELETE', f'/v1/ideas/{idea}/links/{kind}/{target}', body, IdeaAnswer)

    def set_base(self, idea: IdeaId, body: BaseBody) -> IdeaAnswer:
        return self.post(f'/v1/ideas/{idea}/base', body, IdeaAnswer)

    def set_track(self, idea: IdeaId, body: TrackBody) -> TrackChanged:
        return self.post(f'/v1/ideas/{idea}/track', body, TrackChanged)
