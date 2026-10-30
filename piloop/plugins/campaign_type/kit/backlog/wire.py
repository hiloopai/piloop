"""What the backlog's routes take and answer, and the client that calls them."""

from datetime import datetime

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
from piloop.plugins.campaign_type.kit.runs.ids import RunEnd, RunId, RunStatus


class IdeaBody(Params):
    """An idea for the board. It is ``open``."""

    title: str
    hypothesis: str = ''
    """Why this should help, and by how much. Default: the title."""
    plan: str | None = None
    """The steps for the implementer."""
    provenance: str | None = None
    """What prompted the idea, with ids or a URL."""
    priority: Priority = 2
    harness: str | None = None
    """The harness to use instead of the rotation."""
    base: CandidateId | None = None
    """The candidate to start from instead of the head. This makes a branch."""
    track: TrackName | None = None
    """The track it works on. Required when the campaign has more than one track."""
    after: tuple[IdeaId, ...] = ()
    """The ideas to wait for. A worker can claim the idea once all are done."""


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


class IdeaRow(IdeaView):
    """An idea in a list: its view, when it was made, and its links."""

    created: datetime
    after: list[IdeaId]
    """The prerequisites: a worker can claim it once each one is ``done`` or ``dropped``."""


class Ideas(View):
    """A campaign's ideas of the statuses asked for, by status, then by priority, then oldest first, at ``cursor``."""

    cursor: int
    ideas: list[IdeaRow]


class IdeaRun(View):
    """A run of an idea."""

    id: RunId
    status: RunStatus
    end: RunEnd | None
    harness: str
    model: str
    error: str | None


class IdeaDetail(View):
    """An idea in full: what an implementer reads, where it came from, and its runs, at ``cursor``."""

    cursor: int
    idea: IdeaRow
    hypothesis: str
    plan: str | None
    provenance: str | None
    runs: list[IdeaRun]
    """Oldest first."""


class IdeasQuery(Params):
    """Which ideas of a campaign."""

    status: tuple[IdeaStatus, ...] = ()
    """Only those of these statuses. Default: all."""


class PriorityBody(Params):
    """An idea's new priority."""

    priority: Priority


class IdeaAnswer(View):
    """An idea that a change wrote, read at ``cursor``."""

    cursor: int
    idea: IdeaView


class StatusChanged(IdeaAnswer):
    """An idea whose status changed."""

    was: IdeaStatus


class PriorityChanged(IdeaAnswer):
    """An idea whose priority changed."""

    was: int


class TrackChanged(IdeaAnswer):
    """An idea that moved to another track."""

    was: TrackName


class BacklogClient(Client):
    """The calls of the backlog's routes: ideas, their links, bases, tracks and statuses."""

    def ideas(self, campaign: str, query: IdeasQuery) -> Ideas:
        return self._get(f'/v1/campaigns/{segment(campaign)}/ideas', query, Ideas)

    def idea(self, idea: IdeaId) -> IdeaDetail:
        return self._get(f'/v1/ideas/{idea}', Params(), IdeaDetail)

    def set_priority(self, idea: IdeaId, body: PriorityBody) -> PriorityChanged:
        return self.post(f'/v1/ideas/{idea}/priority', body, PriorityChanged)

    def add_idea(self, campaign: str, body: IdeaBody) -> IdeaAnswer:
        return self.post(f'/v1/campaigns/{segment(campaign)}/ideas', body, IdeaAnswer)

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
