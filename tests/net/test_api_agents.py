"""The API requests, prompts, cancels and extends agents and runs."""

import threading
import time
from datetime import timedelta

import pytest
from sqlalchemy import select

from piloop.actor import System
from piloop.agents.results import AgentSessionExtended
from piloop.api.client import Refused
from piloop.api.contract import (
    CancelBody,
    LaunchBody,
    Problem,
    PromptBody,
)
from piloop.api.views import Pending, Queued
from piloop.db.events import has
from piloop.db.models import (
    AgentSession,
    AgentSessionState,
    Campaign,
    Comment,
    Event,
)
from piloop.db.session import Database
from piloop.ids import AgentSessionId, CommentId, Ending, RoleName, TargetRef
from piloop.plugins.campaign_type.kit.runs.ids import RUN, RunId, RunStatus
from piloop.plugins.campaign_type.kit.runs.wire import ExtendBody
from piloop.settings import Settings
from tests.campaign.test_stop import add_run
from tests.conftest import EveryClient, serving_api
from tests.net.test_api import outcome


def test_an_agent_is_requested_of_a_sweeping_role_only(db: Database, campaign: Campaign, cfg: Settings) -> None:
    with serving_api(cfg), EveryClient.on(cfg.socket_file, System.ORCHESTRATOR) as client:
        asked = client.launch('demo', LaunchBody(role=RoleName('research'), model='opus', reason='look again'))
        with pytest.raises(
            Refused, match="^demo has no sweeping role 'janitor'; it has orchestrator, research, "
        ) as refused:
            client.launch('demo', LaunchBody(role=RoleName('janitor'), reason='sweep'))
    assert refused.value.error == Problem.REFUSED
    with db.session() as s:
        agent = s.get_one(AgentSession, asked.agent)
        assert (asked.role, agent.kind, agent.model, agent.reason) == ('research', 'research', 'opus', 'look again')


def test_a_prompt_waits_for_what_came_of_its_note_and_refuses_an_agent_that_does_not_run(
    db: Database, campaign: Campaign, cfg: Settings
) -> None:
    with db.session() as s:
        s.add_all(
            [
                AgentSession(kind='research', campaign_id=1, state=AgentSessionState.RUNNING),
                AgentSession(kind='research', campaign_id=1, state=AgentSessionState.EXITED),
            ]
        )
        s.commit()
    with serving_api(cfg), EveryClient.on(cfg.socket_file, System.ORCHESTRATOR) as client:
        unanswered = client.prompt(AgentSessionId(1), PromptBody(text='look at #3'))
        assert (unanswered.comment, unanswered.delivery) == (1, Pending()), 'no wait: it answers at once'
        threading.Timer(0.2, outcome, [db, CommentId(2)]).start()
        started = time.monotonic()
        queued = client.prompt(AgentSessionId(1), PromptBody(text='and #4', wait=20))
        assert (queued.comment, queued.delivery) == (2, Queued()) and time.monotonic() - started < 10
        with pytest.raises(Refused, match='^agent_session#2 is not running$') as ended:
            client.prompt(AgentSessionId(2), PromptBody(text='x'))
        assert ended.value.error == Problem.REFUSED
        with pytest.raises(Refused, match='^no agent_session#9$') as unknown:
            client.prompt(AgentSessionId(9), PromptBody(text='x'))
        assert unknown.value.error == Problem.UNKNOWN_AGENT
    with db.session() as s:
        assert s.get_one(Comment, 1).author == 'orchestrator'


def test_a_cancel_asks_the_daemon_to_end_an_open_run_or_a_live_agent_and_refuses_a_closed_one(
    db: Database, campaign: Campaign, cfg: Settings
) -> None:
    add_run(db, campaign, status=RunStatus.RUNNING, measured=False)
    add_run(db, campaign, measured=False)
    with db.session() as s:
        s.add_all(
            [
                AgentSession(kind='research', campaign_id=1, state=AgentSessionState.REQUESTED),
                AgentSession(kind='research', campaign_id=1, state=AgentSessionState.EXITED),
            ]
        )
        s.commit()
    with serving_api(cfg), EveryClient.on(cfg.socket_file, System.ORCHESTRATOR) as client:
        agent = client.cancel_agent(AgentSessionId(1), CancelBody())
        run = client.cancel_run(RunId(1), CancelBody(ending=Ending.WRAP_UP))
        assert (agent.agent, agent.role, run.run) == (1, 'research', 1)
        with pytest.raises(Refused, match='^agent_session#2 is not requested, starting or running$') as ended:
            client.cancel_agent(AgentSessionId(2), CancelBody())
        with pytest.raises(Refused, match='^run#2 is not queued or running$') as closed:
            client.cancel_run(RunId(2), CancelBody())
        assert ended.value.error == closed.value.error == Problem.REFUSED
        with pytest.raises(Refused, match='^no agent_session#9$') as no_agent:
            client.cancel_agent(AgentSessionId(9), CancelBody())
        assert no_agent.value.error == Problem.UNKNOWN_AGENT
        with pytest.raises(Refused, match='^no run#9$') as no_run:
            client.cancel_run(RunId(9), CancelBody())
        assert no_run.value.error == Problem.UNKNOWN_RUN
    with db.session() as s:
        asked = [
            (e.kind, e.target_id, e.actor) for e in s.scalars(select(Event)) if e.kind.endswith('cancel_requested')
        ]
    assert asked == [('agent_session.cancel_requested', 1, 'orchestrator'), ('run.cancel_requested', 1, 'orchestrator')]


def test_an_extension_gives_a_running_run_s_implementer_more_time_signed_tells_it_so_and_refuses_a_run_without_one(
    db: Database, campaign: Campaign, cfg: Settings
) -> None:
    add_run(db, campaign, status=RunStatus.RUNNING, measured=False)
    add_run(db, campaign, measured=False)
    with db.session() as s:
        s.add(
            AgentSession(kind='implementer', campaign_id=1, subject=TargetRef(RUN, 1), state=AgentSessionState.RUNNING)
        )
        s.add(
            AgentSession(kind='implementer', campaign_id=1, subject=TargetRef(RUN, 2), state=AgentSessionState.EXITED)
        )
        s.commit()
    with serving_api(cfg), EveryClient.on(cfg.socket_file, System.ORCHESTRATOR) as client:
        first = client.extend_run(RunId(1), ExtendBody(by=timedelta(hours=1)))
        second = client.extend_run(RunId(1), ExtendBody(by=timedelta(minutes=30)))
        assert (first.run, first.agent, first.seconds, second.seconds) == (1, 1, 3600, 5400), 'extensions add up'
        with pytest.raises(Refused, match='^run#2 has no running implementer$') as ended:
            client.extend_run(RunId(2), ExtendBody(by=timedelta(hours=1)))
        assert ended.value.error == Problem.REFUSED
        with pytest.raises(Refused, match='^no run#9$'):
            client.extend_run(RunId(9), ExtendBody(by=timedelta(hours=1)))
        with pytest.raises(Refused, match='^by: Input should be greater than 0 seconds'):
            client.extend_run(RunId(1), ExtendBody.model_construct(by=timedelta(0)))
    with db.session() as s:
        extended = [
            (e.target_id, e.actor, e.payload) for e in s.scalars(select(Event).where(has(AgentSessionExtended)))
        ]
        notes = [(c.author, c.body) for c in s.scalars(select(Comment).where(Comment.target_id == 1))]
    assert extended == [
        (1, 'orchestrator', AgentSessionExtended(seconds=3600)),
        (1, 'orchestrator', AgentSessionExtended(seconds=1800)),
    ]
    assert notes[-1] == (
        'orchestrator',
        '[piloop] orchestrator gave you 0:30:00 more time: your deadline is now 5400 s after $PILOOP_DEADLINE, which'
        ' still holds the old one.',
    )
