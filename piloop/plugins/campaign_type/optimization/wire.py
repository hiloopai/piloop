"""What optimization's own route takes and answers, and the client that calls it."""

from piloop.api.client import Client
from piloop.api.contract import Params
from piloop.plugins.campaign_type.kit.backlog.wire import IdeaView
from piloop.plugins.campaign_type.kit.candidates.ids import CandidateId
from piloop.plugins.campaign_type.kit.candidates.wire import CandidateAnswer
from piloop.plugins.campaign_type.kit.evaluation.ids import TrackName


class NoteBody(Params):
    """Why a regressed result is worth starting from, and the seeds to pin to it."""

    reason: str
    follow_ups: tuple[str, ...] = ()
    """A seed pinned to the candidate for each, for research to develop."""
    track: TrackName | None = None
    """The track of the follow-ups. Default: the track of the candidate's run, else the first track."""


class Noted(CandidateAnswer):
    """A regressed candidate kept in view as a lead."""

    seeds: list[IdeaView]
    """The follow-ups: seeds pinned to the candidate."""


class OptimizationClient(Client):
    """The calls of optimization's own route: a note on a candidate."""

    def note(self, candidate: CandidateId, body: NoteBody) -> Noted:
        return self.post(f'/v1/candidates/{candidate}/note', body, Noted)
