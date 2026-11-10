"""The lineage capability: the contract that a client draws without knowing the campaign's type, and its route.

``Bracket``, a provider of the tests only, is a knockout tournament: its vocabulary, its nodes of two parents and its
spine are nothing like a campaign of candidates. The route serves it as it serves ``optimization``.
"""

from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from importlib.metadata import EntryPoint
from pathlib import Path
from typing import ClassVar

import httpx2 as httpx
import pytest
from fastapi.routing import APIRoute
from pydantic import ValidationError
from sqlalchemy.orm import Session

from piloop.daemon.api import openapi
from piloop.db.models import AgentSession, Campaign
from piloop.db.session import Database
from piloop.ids import AgentSessionId, TargetKind, TargetRef
from piloop.plugins.campaign_type.kit.backlog.ids import IDEA
from piloop.plugins.campaign_type.kit.backlog.tables import Idea
from piloop.plugins.campaign_type.kit.candidates.ids import CANDIDATE, CandidateId
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.evaluation.tables import Measurement
from piloop.plugins.campaign_type.kit.pool.pool import Pool
from piloop.plugins.campaign_type.kit.pool.scored import record_scored
from piloop.plugins.campaign_type.kit.runs.ids import RUN
from piloop.plugins.campaign_type.kit.runs.tables import Run
from piloop.plugins.contribution import provided_routers
from piloop.plugins.lineage.contract import (
    LINEAGE,
    Graph,
    Judged,
    KindName,
    Lineage,
    LineageNode,
    LineageProvider,
    NodeId,
    NodeKind,
    NodeStatus,
    RowLink,
    ScoreTrack,
    SeriesPoint,
    SessionLink,
    StatusName,
    Tone,
    TrackKey,
)
from piloop.plugins.metric import Direction
from piloop.settings import Settings
from tests.conftest import forget_loaded, install_plugins, reconfigured, serving_api, store_config

T0 = datetime(2026, 10, 27, 12, tzinfo=UTC)
PATH = '/v1/campaigns/{campaign}/lineage'


class Bracket(LineageProvider):
    """A knockout tournament of ``entrants``: each match is a node whose parents are the two that played it."""

    DEFAULT: ClassVar[bool] = False
    title: str = 'Cup'
    """What the final is called."""

    def graph(self, s: Session, campaign: Campaign) -> Graph:
        entrants = [_entrant(name) for name in ('ana', 'bo', 'cy', 'di')]
        semis = [_match('semi#1', 'ana', 'bo', 'ana'), _match('semi#2', 'cy', 'di', 'di')]
        final = _match('final', 'semi#1', 'semi#2', 'semi#1').model_copy(
            update={'label': f'{campaign.name} {self.title}'}
        )
        return Graph(
            kinds=[
                NodeKind(name=KindName('entrant'), label='Entrant'),
                NodeKind(name=KindName('match'), label='Match'),
            ],
            statuses=[
                NodeStatus(name=StatusName('won'), label='Won', tone=Tone.SUCCESS),
                NodeStatus(name=StatusName('lost'), label='Lost', tone=Tone.NEUTRAL),
                NodeStatus(name=StatusName('playing'), label='Playing', tone=Tone.ACCENT),
            ],
            tracks=[ScoreTrack(name=TrackKey('elo'), metric='rating', direction=Direction.HIGHER)],
            evaluator=None,
            nodes=[*entrants, *semis, final],
            spine=[NodeId('ana'), NodeId('semi#1'), NodeId('final')],
            series=[SeriesPoint(node=NodeId('semi#1'), at=T0 + timedelta(hours=1))],
        )


def _entrant(name: str) -> LineageNode:
    return LineageNode(
        id=NodeId(name),
        kind=KindName('entrant'),
        status=StatusName('won' if name in ('ana', 'di') else 'lost'),
        label=name,
        parents=[],
        created=T0,
        judged=None,
        scores={TrackKey('elo'): 1500.0},
        links=[],
        summary=None,
    )


def _match(name: str, home: str, away: str, winner: str) -> LineageNode:
    return LineageNode(
        id=NodeId(name),
        kind=KindName('match'),
        status=StatusName('playing' if name == 'final' else 'won'),
        label=f'{home} v {away}',
        parents=[NodeId(home), NodeId(away)],
        created=T0 + timedelta(hours=1),
        judged=Judged(by='referee', reason=f'{winner} scored more'),
        scores={},
        links=[RowLink(kind='comment', id=7, label='the report')],
        summary=None,
    )


@pytest.fixture
def bracket(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Install a package with the ``bracket`` lineage provider."""
    install_plugins(monkeypatch, [EntryPoint('bracket', f'{__name__}:Bracket', LINEAGE.group)])
    yield
    forget_loaded()


def _graph(**changed: object) -> dict[str, object]:
    """The bracket's graph as JSON, with ``changed`` in place of its fields."""
    return Bracket().graph(Session(), Campaign(name='open')).model_dump(mode='json') | changed


def test_a_graph_reads_back_from_its_json() -> None:
    graph = Bracket().graph(Session(), Campaign(name='open'))
    assert Graph.model_validate_json(graph.model_dump_json()) == graph


def _node(at: int, **changed: object) -> dict[str, object]:
    nodes = _graph()['nodes']
    assert isinstance(nodes, list)
    return {**nodes[at], **changed}


BROKEN: list[tuple[dict[str, object], str]] = [
    ({'kinds': [{'name': 'match', 'label': 'M'}] * 2}, "kind 'match' is declared twice"),
    ({'statuses': [{'name': 'won', 'label': 'W', 'tone': 'success'}] * 2}, "status 'won' is declared twice"),
    ({'tracks': [{'name': 'elo', 'metric': 'r', 'direction': 'higher'}] * 2}, "track 'elo' is declared twice"),
    ({'nodes': [_node(0), _node(0)], 'spine': [], 'series': []}, "node 'ana' is declared twice"),
    ({'nodes': [_node(0, kind='player')], 'spine': [], 'series': []}, "ana: kind 'player' is not declared"),
    ({'nodes': [_node(0, status='seeded')], 'spine': [], 'series': []}, "ana: status 'seeded' is not declared"),
    ({'nodes': [_node(0, scores={'goals': 2})], 'spine': [], 'series': []}, "ana: track 'goals' is not declared"),
    ({'nodes': [_node(4)], 'spine': [], 'series': []}, "semi#1: parent 'ana' is not an earlier node"),
    ({'spine': ['nobody']}, "spine: 'nobody' is not a node"),
    ({'spine': ['semi#1'], 'series': []}, "spine: 'semi#1' is not a root"),
    ({'spine': ['ana', 'final']}, "spine: 'final' is not a child of 'ana'"),
    ({'series': [{'node': 'semi#2', 'at': '2026-10-27T13:00:00Z'}]}, "series: 'semi#2' is not on the spine"),
    (
        {'series': [{'node': n, 'at': '2026-10-27T13:00:00Z'} for n in ('final', 'semi#1')]},
        'series: not in the order of the spine',
    ),
]


@pytest.mark.parametrize(('changed', 'error'), BROKEN, ids=[e for _, e in BROKEN])
def test_a_graph_that_breaks_a_rule_is_refused_with_what_it_breaks(changed: dict[str, object], error: str) -> None:
    with pytest.raises(ValidationError, match=f'Value error, {error}'):
        Graph.model_validate(_graph(**changed))


def test_a_link_is_a_row_or_an_agent_session_with_its_harness_session() -> None:
    session = {'kind': 'agent_session', 'id': 3, 'harness': 'claude', 'session_id': 'abc', 'label': None}
    [link] = LineageNode.model_validate(_node(0, links=[session])).links
    assert link == SessionLink(
        kind='agent_session', id=AgentSessionId(3), harness='claude', session_id='abc', label=None
    )
    with pytest.raises(ValidationError, match='union_tag_invalid'):
        LineageNode.model_validate(_node(0, links=[{'kind': 'home', 'id': 1, 'label': None}]))
    with pytest.raises(ValidationError, match='harness'):
        LineageNode.model_validate(_node(0, links=[{'kind': 'agent_session', 'id': 3, 'label': None}]))


@pytest.mark.usefixtures('bracket')
def test_the_route_answers_the_graph_of_the_instance_that_the_campaign_uses(
    db: Database, campaign: Campaign, tmp_path: Path
) -> None:
    with db.session() as s:
        row = s.get_one(Campaign, campaign.id)
        final = {'provider': 'bracket', 'title': 'Final'}
        row.config = reconfigured(row.config, plugins={'lineage': {'use': 'final', 'final': final}})
        s.commit()
    cfg = Settings(home=tmp_path)
    transport = httpx.HTTPTransport(uds=str(cfg.socket_file))
    with serving_api(cfg), httpx.Client(transport=transport, base_url='http://piloop') as http:
        answer = http.get(PATH.format(campaign=campaign.name))
        unknown = http.get(PATH.format(campaign='nope'))
        store_config(db, campaign.id, {'name': campaign.name, 'metrics': 'no list'})
        invalid = http.get(PATH.format(campaign=campaign.id))
    lineage = Lineage.model_validate_json(answer.content)
    assert (lineage.provider, lineage.cursor > 0) == ('final', True)
    assert lineage.graph.nodes[-1].label == f'{campaign.name} Final', 'the instance with its own parameters'
    assert (unknown.status_code, unknown.json()['error']) == (404, 'unknown_campaign')
    assert (invalid.status_code, invalid.json()['error']) == (409, 'refused')
    assert 'the stored config' in invalid.json()['detail']


@pytest.mark.usefixtures('bracket')
def test_every_provider_shares_the_route_which_is_mounted_and_documented_once() -> None:
    routers = provided_routers()
    found = [r.path for router in routers for r in router.routes if isinstance(r, APIRoute) and r.path == PATH]
    assert found == [PATH]
    lineage = openapi()['paths']
    assert isinstance(lineage, dict) and list(lineage[PATH]) == ['get']


def grow(
    db: Database, campaign: Campaign, nodes: Sequence[tuple[tuple[float | None, ...], str | None]]
) -> list[CandidateId]:
    """Add one run and its candidate for each of ``nodes``: its evaluations' scores (None for one that failed), and
    why a human excluded it, or None.

    Each is a child of the latest one with 3 successful evaluations, which confirm it. Each run has an implementer,
    with its harness session. Each measured candidate is scored into the pool, as the daemon scores it.
    """
    found: list[CandidateId] = []
    with db.session() as s:
        row = s.get_one(Campaign, campaign.id)
        row.config = reconfigured(row.config, evaluator={'noise_runs': 3})
        parent = CandidateId(1)
        for n, (scores, excluded) in enumerate(nodes):
            idea = Idea(campaign_id=row.id, track='score', title=f'idea {n}', hypothesis='h', created_by='human')
            s.add(idea)
            s.flush()
            run = Run(
                campaign_id=idea.campaign_id,
                idea_id=idea.id,
                attempt=1,
                base_candidate_id=parent,
                harness='pi',
                model='m',
            )
            s.add(run)
            s.flush()
            s.add(
                AgentSession(
                    kind='implementer',
                    campaign_id=row.id,
                    subject=TargetRef(RUN, run.id),
                    harness='pi',
                    session_id=f's{n}',
                )
            )
            c = Candidate(
                campaign_id=row.id,
                run_id=run.id,
                parent_id=parent,
                tree_sha=f'tree{n}',
                revision=f'commit{n}',
                summary=f'commit {n}',
                excluded_reason=excluded,
                excluded_by=None if excluded is None else 'human',
            )
            s.add(c)
            s.flush()
            s.add_all(
                Measurement(
                    candidate_id=c.id,
                    evaluator_id=row.evaluator_id,
                    metrics={} if v is None else {'score': v},
                    error='boom' if v is None else None,
                )
                for v in scores
            )
            s.flush()
            if scores:
                record_scored(s, Pool.of(s, row, row.config.all_tracks[0]), c.id)
            parent = c.id if sum(v is not None for v in scores) >= 3 else parent
            found.append(c.id)
        s.commit()
    return found


def _lineage(cfg: Settings, campaign: Campaign) -> httpx.Response:
    transport = httpx.HTTPTransport(uds=str(cfg.socket_file))
    with serving_api(cfg), httpx.Client(transport=transport, base_url='http://piloop') as http:
        return http.get(PATH.format(campaign=campaign.name))


def test_optimization_draws_the_candidates_with_the_best_s_ancestry_as_the_spine(
    db: Database, campaign: Campaign, tmp_path: Path
) -> None:
    """3 evaluations confirm a candidate (``noise_runs``): one evaluation is scored, not ranked."""
    best, ranked, scored, crashed, pending, excluded = grow(
        db,
        campaign,
        [((3.0, 3.0, 3.0), None), ((2.0, 2.0, 2.0), None), ((1.5,), None), ((None,), None), ((), None)]
        + [((4.0, 4.0, 4.0), 'gamed the split')],
    )
    lineage = Lineage.model_validate_json(_lineage(Settings(home=tmp_path), campaign).content)
    graph = lineage.graph
    assert lineage.provider == 'optimization', 'the default'
    assert [(n.id, n.kind, n.status, n.parents) for n in graph.nodes] == [
        ('candidate#1', 'baseline', 'ranked', []),
        (f'candidate#{best}', 'candidate', 'best', ['candidate#1']),
        (f'candidate#{ranked}', 'candidate', 'ranked', [f'candidate#{best}']),
        (f'candidate#{scored}', 'candidate', 'scored', [f'candidate#{ranked}']),
        (f'candidate#{crashed}', 'candidate', 'crashed', [f'candidate#{ranked}']),
        (f'candidate#{pending}', 'candidate', 'pending', [f'candidate#{ranked}']),
        (f'candidate#{excluded}', 'candidate', 'excluded', [f'candidate#{ranked}']),
    ]
    assert graph.spine == ['candidate#1', f'candidate#{best}']
    assert [p.node for p in graph.series] == ['candidate#1', f'candidate#{best}'], 'the baseline, then the new bests'
    assert [n.scores for n in graph.nodes] == [
        {'score': 1.0},
        {'score': 3.0},
        {'score': 2.0},
        {'score': 1.5},
        {},
        {},
        {'score': 4.0},
    ]
    assert graph.nodes[-1].judged == Judged(by='human', reason='gamed the split'), 'who excluded it, and why'
    assert graph.evaluator == f'evaluator#{campaign.evaluator_id}'
    [track] = graph.tracks
    assert (track.name, track.metric, track.direction) == ('score', 'score', Direction.HIGHER)
    best_node = graph.nodes[1]
    assert (best_node.label, best_node.summary, best_node.judged) == ('idea 0', 'commit 0', None)
    assert [(link.kind, link.label) for link in best_node.links] == [
        (CANDIDATE, None),
        (IDEA, 'idea 0'),
        (RUN, None),
        (TargetKind.AGENT_SESSION, f'implementer#{best_node.links[3].id}'),
    ]
    assert isinstance(best_node.links[3], SessionLink) and (
        best_node.links[3].harness,
        best_node.links[3].session_id,
    ) == ('pi', 's0')
