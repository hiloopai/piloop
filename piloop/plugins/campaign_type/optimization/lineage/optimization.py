"""``optimization``: the default lineage, one node per candidate, with the head's ancestry as the spine."""

from collections.abc import Iterable, Sequence
from typing import ClassVar, Final

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from piloop.actor import Agent
from piloop.db.models import AgentSession, Campaign, Event
from piloop.plugins.campaign_type.kit.backlog.ids import IdeaId
from piloop.plugins.campaign_type.kit.backlog.tables import Idea
from piloop.plugins.campaign_type.kit.candidates.ids import CANDIDATE, JUDGED, CandidateId, Origin
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.evaluation.config import Track
from piloop.plugins.campaign_type.kit.evaluation.measure import derive, full_clause, means, ratios
from piloop.plugins.campaign_type.kit.evaluation.tables import Evaluator, Measurement
from piloop.plugins.campaign_type.kit.runs.ids import RUN, RunId
from piloop.plugins.campaign_type.kit.runs.tables import Run
from piloop.plugins.campaign_type.kit.verdicts.judge import CandidateJudged, head, trunk_keeps
from piloop.plugins.campaign_type.kit.verdicts.verdict_rule.contract import Outcome
from piloop.plugins.lineage.contract import (
    Graph,
    Judged,
    KindName,
    LineageNode,
    LineageProvider,
    NodeId,
    NodeKind,
    NodeLink,
    NodeStatus,
    RowLink,
    ScoreTrack,
    SeriesPoint,
    SessionLink,
    StatusName,
    Tone,
    TrackKey,
)

KINDS: Final[dict[Origin, NodeKind]] = {
    Origin.BASELINE: NodeKind(name=KindName('baseline'), label='Baseline'),
    Origin.RUN: NodeKind(name=KindName('candidate'), label='Candidate'),
    Origin.REFERENCE: NodeKind(name=KindName('reference'), label='Reference'),
    Origin.RECOVERED: NodeKind(name=KindName('recovered'), label='Recovered'),
    Origin.SKIPPED: NodeKind(name=KindName('skipped'), label='Skipped'),
}
"""The kind of a candidate by its origin."""
VERDICTS: Final[dict[Outcome, NodeStatus]] = {
    Outcome.KEEP: NodeStatus(name=StatusName('kept'), label='Kept', tone=Tone.SUCCESS),
    Outcome.INTERESTING: NodeStatus(name=StatusName('interesting'), label='Interesting', tone=Tone.ATTENTION),
    Outcome.DISCARD: NodeStatus(name=StatusName('discarded'), label='Discarded', tone=Tone.NEUTRAL),
    Outcome.CRASH: NodeStatus(name=StatusName('crashed'), label='Crashed', tone=Tone.DANGER),
}
"""The status of a judged candidate by its verdict."""
PENDING: Final = NodeStatus(name=StatusName('pending'), label='Pending', tone=Tone.ACCENT)
"""A candidate of a judged origin without its verdict yet."""
UNJUDGED: Final = NodeStatus(name=StatusName('unjudged'), label='Not judged', tone=Tone.NEUTRAL)
"""A candidate of an origin that is never judged: a reference, a recovered or a skipped tree."""


class OptimizationLineage(LineageProvider):
    """``[plugins.lineage] use = "optimization"``, the default: the candidates of the campaign and their parents.

    Its spine is the head's ancestry, and its series the trunk's keeps, each when it was last judged. The scores are
    the primary metric of each track, under the current evaluator.
    """

    DEFAULT: ClassVar[bool] = True

    def graph(self, s: Session, campaign: Campaign) -> Graph:
        tracks = campaign.config.all_tracks
        scored = _scores(s, campaign, tracks)
        sessions = _sessions(s, campaign)
        made = select(
            Candidate.id,
            Candidate.parent_id,
            Candidate.origin,
            Candidate.verdict,
            Candidate.verdict_by,
            Candidate.verdict_reason,
            Candidate.summary,
            Candidate.created_at,
            Candidate.run_id,
            Run.idea_id,
            Idea.title,
        )
        rows = s.execute(
            made.outerjoin(Run, Candidate.run_id == Run.id)
            .outerjoin(Idea, Run.idea_id == Idea.id)
            .where(Candidate.campaign_id == campaign.id)
            .order_by(Candidate.id)
        )
        nodes: list[LineageNode] = []
        parents: dict[CandidateId, CandidateId | None] = {}
        for c, parent, origin, verdict, by, reason, summary, created, run, idea, title in rows:
            parents[c] = parent
            nodes.append(
                LineageNode(
                    id=_id(c),
                    kind=KINDS[origin].name,
                    status=_status(origin, verdict).name,
                    label=title or summary or KINDS[origin].label,
                    parents=[] if parent is None else [_id(parent)],
                    created=created,
                    judged=None if by is None else Judged(by=by, reason=reason),
                    scores=scored.get(c, {}),
                    links=_links(c, run, idea, title, sessions),
                    summary=summary,
                )
            )
        found = head(s, campaign)
        spine = _ancestry(found.id, parents) if found is not None else []
        return Graph(
            kinds=list(KINDS.values()),
            statuses=[*VERDICTS.values(), PENDING, UNJUDGED],
            tracks=[_track(t) for t in tracks],
            evaluator=None if campaign.evaluator_id is None else f'evaluator#{campaign.evaluator_id}',
            nodes=nodes,
            spine=[_id(c) for c in spine],
            series=_series(s, trunk_keeps(s, campaign), set(spine)),
        )


def _id(candidate: CandidateId) -> NodeId:
    return NodeId(f'candidate#{candidate}')


def _status(origin: Origin, verdict: Outcome | None) -> NodeStatus:
    match verdict:
        case None:
            return PENDING if origin in JUDGED else UNJUDGED
        case _:
            return VERDICTS[verdict]


def _track(track: Track) -> ScoreTrack:
    return ScoreTrack(name=TrackKey(track.name), metric=track.primary.name, direction=track.primary.direction)


def _scores(s: Session, campaign: Campaign, tracks: Sequence[Track]) -> dict[CandidateId, dict[TrackKey, float]]:
    """The score of each candidate on each track that has one: its primary's mean under the current evaluator."""
    measured = (
        select(Measurement.candidate_id, Measurement.metrics)
        .join(Candidate, Measurement.candidate_id == Candidate.id)
        .where(Candidate.campaign_id == campaign.id, Measurement.evaluator_id == campaign.evaluator_id, full_clause())
        .order_by(Measurement.id)
    )
    rows: dict[CandidateId, list[dict[str, float]]] = {}
    for candidate, values in s.execute(measured):
        rows.setdefault(candidate, []).append(values)
    evaluator = s.get(Evaluator, campaign.evaluator_id) if campaign.evaluator_id is not None else None
    computed = ratios(evaluator)
    found: dict[CandidateId, dict[TrackKey, float]] = {}
    for candidate, values in rows.items():
        metrics = derive(means(values), computed)
        found[candidate] = {TrackKey(t.name): metrics[t.primary.name] for t in tracks if t.primary.name in metrics}
    return found


def _sessions(s: Session, campaign: Campaign) -> dict[RunId, list[SessionLink]]:
    """The agent sessions of each run of the campaign, oldest first. It reads columns: a campaign has thousands."""
    rows = s.execute(
        select(
            AgentSession.id, AgentSession.kind, AgentSession.subject_id, AgentSession.harness, AgentSession.session_id
        )
        .where(AgentSession.campaign_id == campaign.id, (AgentSession.subject_kind == RUN))
        .order_by(AgentSession.id)
    )
    found: dict[RunId, list[SessionLink]] = {}
    for agent, role, run, harness, session in rows:
        assert run is not None  # the query selects these only
        label = Agent(role=role, session=agent).signature
        found.setdefault(RunId(run), []).append(
            SessionLink(kind='agent_session', id=agent, harness=harness, session_id=session, label=label)
        )
    return found


def _links(
    c: CandidateId, run: RunId | None, idea: IdeaId | None, title: str | None, sessions: dict[RunId, list[SessionLink]]
) -> list[NodeLink]:
    """The candidate itself, then its run's idea, the run and the run's agent sessions."""
    found: list[NodeLink] = [RowLink(kind='candidate', id=c, label=None)]
    if run is not None and idea is not None:
        found += [RowLink(kind='idea', id=idea, label=title), RowLink(kind='run', id=run, label=None)]
        found += sessions.get(run, [])
    return found


def _ancestry(candidate: CandidateId, parents: dict[CandidateId, CandidateId | None]) -> list[CandidateId]:
    """``candidate``'s ancestry, from its root to it."""
    line = [candidate]
    while (parent := parents[line[-1]]) is not None:
        line.append(parent)
    return line[::-1]


def _series(s: Session, keeps: Iterable[Candidate], spine: set[CandidateId]) -> list[SeriesPoint]:
    """The trunk's keeps on the spine, each when it was last judged, else when it was made."""
    on = [c for c in keeps if c.id in spine]
    judged = dict(
        s.execute(
            select(Event.target_id, func.max(Event.at))
            .where(
                Event.target_kind == CANDIDATE,
                Event.target_id.in_([c.id for c in on]),
                Event.kind.in_(CandidateJudged.kinds()),
            )
            .group_by(Event.target_id)
        ).all()
    )
    return [SeriesPoint(node=_id(c.id), at=judged.get(c.id, c.created_at)) for c in on]
