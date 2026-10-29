"""The reads of the API that a UI needs."""

from pathlib import Path

import pytest

from piloop.actor import System
from piloop.api.client import Refused
from piloop.api.contract import (
    CommentsQuery,
    EventsQuery,
    OutputQuery,
    Problem,
    Selection,
)
from piloop.api.views import CampaignState
from piloop.db.models import (
    AgentSession,
    AgentSessionState,
    Campaign,
    Comment,
    utcnow,
)
from piloop.db.session import Database
from piloop.ids import AgentSessionId, TargetRef
from piloop.plugins.campaign_type.kit.backlog.ids import IDEA, IdeaId, IdeaStatus
from piloop.plugins.campaign_type.kit.backlog.wire import IdeasQuery, PriorityBody
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.evaluation.tables import Measurement
from piloop.plugins.campaign_type.kit.runs.tables import Run
from piloop.settings import Settings
from tests.conftest import EveryClient, serving_api
from tests.reports.test_history import idea


def test_the_reads_of_a_ui_answer_agents_their_output_ideas_and_comments(
    db: Database, campaign: Campaign, cfg: Settings, tmp_path: Path
) -> None:
    log = tmp_path / 'research-log'
    log.mkdir()
    (log / 'transcript.txt').write_text('reading the history\n\nproposing idea B\n')
    with db.session() as s:
        p = s.get_one(Campaign, campaign.id)
        old = idea(s, p, 'more lines', IdeaStatus.DONE)
        run = Run(idea=old, attempt=1, base_candidate_id=1, harness='pi', model='m', error='no commit')
        s.add(run)
        s.flush()
        s.add(Candidate(id=2, campaign_id=p.id, run_id=run.id, parent_id=1, tree_sha='t2', revision='r2'))
        s.add(Measurement(candidate_id=2, evaluator_id=1, metrics={'score': 3, 'size': 4}))
        newer = idea(s, p, 'even more lines', IdeaStatus.READY, priority=1, after=(old.id,), supersedes=(old.id,))
        idea(s, p, 'a hunch', IdeaStatus.SEED)
        s.add(
            AgentSession(
                kind='research',
                campaign_id=p.id,
                harness='claude',
                model='opus',
                runtime='subprocess',
                state=AgentSessionState.RUNNING,
                reason='low_water: 1 ready < 10',
                log_dir=str(log),
                started_at=utcnow(),
            )
        )
        s.add(Comment(target=TargetRef(IDEA, newer.id), author='human', body='worth a try'))
        s.commit()
    with serving_api(cfg), EveryClient.on(cfg.socket_file, System.HUMAN) as client:
        [state] = client.status(Selection(campaign='demo')).campaigns
        assert isinstance(state, CampaignState)
        [agent] = state.agents
        assert (agent.runtime, agent.reason, agent.started is not None) == (
            'subprocess',
            'low_water: 1 ready < 10',
            True,
        )
        output = client.agent_output(agent.id, OutputQuery(lines=1))
        assert output.lines == ['proposing idea B'], 'the last lines, blank ones left out'
        listed = client.ideas('demo', IdeasQuery(status=(IdeaStatus.SEED, IdeaStatus.READY)))
        assert [(i.title, i.after, i.supersedes) for i in listed.ideas] == [
            ('a hunch', [], []),
            ('even more lines', [old.id], [old.id]),
        ], 'in the order of the statuses asked for'
        assert len(client.ideas('demo', IdeasQuery()).ideas) == 3
        shown = client.idea(old.id)
        assert (shown.idea.superseded_by, shown.hypothesis, [(r.status, r.error) for r in shown.runs]) == (
            [newer.id],
            'more lines helps',
            [('queued', 'no commit')],
        )
        moved = client.set_priority(newer.id, PriorityBody(priority=4))
        assert (moved.was, moved.idea.priority) == (1, 4)
        [event] = client.events(EventsQuery(after=listed.cursor, kind=('idea.priority',))).events
        assert (event.subject.id, event.actor) == (newer.id, 'human')
        [note] = client.comments(CommentsQuery(kind=IDEA, id=newer.id)).comments
        assert (note.author, note.body) == ('human', 'worth a try')
        assert [n.id for n in client.lineage('demo').graph.nodes] == ['candidate#1', 'candidate#2']
        for refused, error in [
            (lambda: client.idea(IdeaId(99)), Problem.UNKNOWN_IDEA),
            (lambda: client.set_priority(IdeaId(99), PriorityBody(priority=0)), Problem.UNKNOWN_IDEA),
            (lambda: client.agent_output(AgentSessionId(99), OutputQuery(lines=1)), Problem.UNKNOWN_AGENT),
        ]:
            with pytest.raises(Refused) as no:
                refused()
            assert no.value.error == error
