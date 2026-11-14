"""``optimization``: the default lineage, one node per candidate, with the head's ancestry as the spine."""

from collections.abc import Sequence
from typing import ClassVar, Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from piloop.actor import Agent
from piloop.db.models import AgentSession, Campaign
from piloop.plugins.campaign_type.kit.backlog.ids import IdeaId
from piloop.plugins.campaign_type.kit.backlog.tables import Idea
from piloop.plugins.campaign_type.kit.candidates.ids import CandidateId, Origin
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.evaluation.config import Track
from piloop.plugins.campaign_type.kit.evaluation.ids import Measured, TrackName
from piloop.plugins.campaign_type.kit.pool.pool import UNSCORED, Pool, Standing, fallback_track
from piloop.plugins.campaign_type.kit.pool.scored import new_bests
from piloop.plugins.campaign_type.kit.runs.ids import RUN, RunId
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
    Origin.MERGE: NodeKind(name=KindName('merge'), label='Merge'),
}
"""The kind of a candidate by its origin."""
BEST: Final = NodeStatus(name=StatusName('best'), label='Best', tone=Tone.SUCCESS)
"""The best of its track."""
RANKED: Final = NodeStatus(name=StatusName('ranked'), label='May be best', tone=Tone.ACCENT)
"""Confirmed, and the best rule allows it."""
SCORED: Final = NodeStatus(name=StatusName('scored'), label='Scored', tone=Tone.NEUTRAL)
"""Scored, not confirmed, or refused by the best rule."""
EXCLUDED: Final = NodeStatus(name=StatusName('excluded'), label='Excluded', tone=Tone.ATTENTION)
"""Someone took it out of the race for the best."""
CRASHED: Final = NodeStatus(name=StatusName('crashed'), label='Crashed', tone=Tone.DANGER)
"""Every evaluation of it failed."""
PENDING: Final = NodeStatus(name=StatusName('pending'), label='Pending', tone=Tone.ACCENT)
"""Not scored yet."""
UNSCORED_STATUS: Final = NodeStatus(name=StatusName('unscored'), label='Not scored', tone=Tone.NEUTRAL)
"""A candidate of an origin that is never scored: a recovered or a skipped tree."""
STATUSES: Final = (BEST, RANKED, SCORED, EXCLUDED, CRASHED, PENDING, UNSCORED_STATUS)


class OptimizationLineage(LineageProvider):
    """``[plugins.lineage] use = "optimization"``, the default: the candidates of the campaign and their parents.

    Its spine is the ancestry of the best of the first track, and its series the new bests on it, each when it was
    scored. The scores are the primary metric of each track, under the current evaluator. A node's status is where
    it stands in the pool of its track.
    """

    DEFAULT: ClassVar[bool] = True

    def graph(self, s: Session, campaign: Campaign) -> Graph:
        tracks = campaign.config.all_tracks
        sessions = _sessions(s, campaign)
        made = select(
            Candidate.id,
            Candidate.parent_id,
            Candidate.origin,
            Candidate.excluded_by,
            Candidate.excluded_reason,
            Candidate.summary,
            Candidate.created_at,
            Candidate.run_id,
            Candidate.idea_id,
            Idea.title,
            Idea.track,
        )
        rows = s.execute(
            made.outerjoin(Idea, Candidate.idea_id == Idea.id)
            .where(Candidate.campaign_id == campaign.id)
            .order_by(Candidate.id)
        )
        nodes: list[LineageNode] = []
        parents: dict[CandidateId, CandidateId | None] = {}
        pools: dict[TrackName, Pool] = {}
        for c, parent, origin, by, reason, summary, created, run, idea, title, on in rows:
            parents[c] = parent
            track = fallback_track(campaign.config, on)
            pool = pools.get(track.name) or pools.setdefault(track.name, Pool.of(s, campaign, track))
            nodes.append(
                LineageNode(
                    id=_id(c),
                    kind=KINDS[origin].name,
                    status=(UNSCORED_STATUS if origin in UNSCORED else _status(pool.standing(c))).name,
                    label=title or summary or KINDS[origin].label,
                    parents=[] if parent is None else [_id(parent)],
                    created=created,
                    judged=None if by is None else Judged(by=by, reason=reason),
                    scores=_scores(pool, c, tracks),
                    links=_links(c, run, idea, title, sessions),
                    summary=summary,
                )
            )
        first = pools.get(tracks[0].name) or Pool.of(s, campaign, tracks[0])
        found = first.best()
        spine = _ancestry(found, parents) if found is not None else []
        return Graph(
            kinds=list(KINDS.values()),
            statuses=list(STATUSES),
            tracks=[_track(t) for t in tracks],
            evaluator=None if campaign.evaluator_id is None else f'evaluator#{campaign.evaluator_id}',
            nodes=nodes,
            spine=[_id(c) for c in spine],
            series=_series(s, campaign, tracks[0], set(spine)),
        )


def _id(candidate: CandidateId) -> NodeId:
    return NodeId(f'candidate#{candidate}')


def _status(standing: Standing) -> NodeStatus:
    """The status of a scored candidate: where it stands in the pool of its track."""
    if standing.best == standing.candidate:
        return BEST
    if standing.excluded is not None:
        return EXCLUDED
    if standing.mean is None:
        return CRASHED if standing.status.state is Measured.CRASHED else PENDING
    return RANKED if standing.rank is not None else SCORED


def _track(track: Track) -> ScoreTrack:
    return ScoreTrack(name=TrackKey(track.name), metric=track.primary.name, direction=track.primary.direction)


def _scores(pool: Pool, c: CandidateId, tracks: Sequence[Track]) -> dict[TrackKey, float]:
    """The score of ``c`` on each track that has one: its primary's mean in ``pool``, under its evaluator."""
    found = pool.entries[c].means if c in pool.entries else {}
    return {TrackKey(t.name): found[t.primary.name] for t in tracks if t.primary.name in found}


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


def _series(s: Session, campaign: Campaign, track: Track, spine: set[CandidateId]) -> list[SeriesPoint]:
    """The baseline, then the new bests of ``track`` on the spine, each when it was scored best."""
    first = s.scalar(select(Candidate).where(Candidate.campaign_id == campaign.id).order_by(Candidate.id).limit(1))
    start = {first.id: first.created_at} if first is not None and first.id in spine else {}
    found = start | {e.target_id: e.at for e, _ in new_bests(s, campaign, track.name)}
    return [SeriesPoint(node=_id(c), at=found[c]) for c in sorted(spine) if c in found]
