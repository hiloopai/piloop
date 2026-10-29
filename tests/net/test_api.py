"""The API: who may call it on each listener, what it answers, how its long-poll waits, and its OpenAPI document."""

import errno
import os
import subprocess
import threading
import time
from contextlib import closing
from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path
from types import UnionType
from typing import Annotated, TypeAliasType, get_args, get_origin, get_type_hints

import httpx2 as httpx
import pytest
from fastapi import APIRouter
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute
from pydantic import BaseModel
from pytest_mock import MockerFixture
from rich.console import Console

from piloop.actor import System
from piloop.agents.results import PromptOutcome
from piloop.api import client as client_module
from piloop.api.client import Client, NoDaemon, Refused
from piloop.api.contract import (
    MAX_POLL_SECONDS,
    Access,
    AckBody,
    EventsQuery,
    InboxQuery,
    MessageBody,
    NewsQuery,
    Params,
    PauseBody,
    Problem,
    PromptBody,
    Reasoned,
    ReplyBody,
    Selection,
    StopBody,
)
from piloop.api.views import CampaignState, Pending, Status, thread_lines
from piloop.cli import common, main as cli
from piloop.daemon.api import ROUTERS, Handlers, daemon_state, openapi, with_api
from piloop.db import session, sql
from piloop.db.events import emit
from piloop.db.models import (
    EVENT,
    AgentSession,
    AgentSessionState,
    Campaign,
    Comment,
    EventModel,
    UnknownEvent,
)
from piloop.db.session import Database
from piloop.ids import AgentSessionId, CommentId, CommentKind, TargetKind, TargetRef
from piloop.net import tokens
from piloop.net.server import OPENAPI, Server, app, local_listener
from piloop.net.tokens import Tokens
from piloop.plugins.campaign_type.kit.backlog import api as backlog_api
from piloop.plugins.campaign_type.kit.backlog.ids import IdeaId, IdeaStatus, LinkKind
from piloop.plugins.campaign_type.kit.backlog.wire import BacklogClient, IdeaBody, StatusBody
from piloop.plugins.campaign_type.kit.candidates import api as candidates_api
from piloop.plugins.campaign_type.kit.candidates.ids import CandidateId
from piloop.plugins.campaign_type.kit.candidates.wire import CandidatesClient
from piloop.plugins.campaign_type.kit.evaluation import api as evaluation_api
from piloop.plugins.campaign_type.kit.evaluation.wire import EvaluationClient
from piloop.plugins.campaign_type.kit.runs import api as runs_api
from piloop.plugins.campaign_type.kit.runs.wire import RunsClient
from piloop.plugins.campaign_type.kit.verdicts import api as verdicts_api
from piloop.plugins.campaign_type.kit.verdicts.wire import RemeasureBody, VerdictsClient
from piloop.plugins.campaign_type.optimization import api as optimization_api
from piloop.plugins.campaign_type.optimization.wire import OptimizationClient
from piloop.plugins.contribution import collect, contributions_of, installed
from piloop.plugins.lineage import api as lineage_api
from piloop.plugins.lineage.wire import LineageClient
from piloop.plugins.network.contract import NetworkHost
from piloop.plugins.network.https import TIMEOUT, TOKEN_ENV, Https
from piloop.plugins.secrets.contract import HomeSecrets
from piloop.plugins.secrets.local import LocalSecrets
from piloop.settings import Settings
from tests.conftest import EveryClient, disk, serving_api


def token(db: Database, name: str, access: Access) -> str:
    with db.session() as s:
        secret = tokens.create(s, name, access)
        s.commit()
    return secret


def emitted(db: Database, kind: str) -> None:
    with db.session() as s:
        emit(s, UnknownEvent(kind=kind, payload={}), TargetRef(TargetKind.CAMPAIGN, 1), System.DAEMON)
        s.commit()


def test_on_the_socket_a_call_without_a_token_is_the_owner_and_a_token_is_its_own_caller(
    db: Database, campaign: Campaign, cfg: Settings
) -> None:
    worker = token(db, 'gpu1', Access.WORKER)
    with serving_api(cfg), EveryClient.on(cfg.socket_file, System.HUMAN) as owner:
        [state] = owner.status(Selection(campaign='demo')).campaigns
        assert state.name == 'demo'
        transport = httpx.HTTPTransport(uds=str(cfg.socket_file))
        with httpx.Client(transport=transport, headers={'Authorization': f'Bearer {worker}'}) as client:
            refused = client.get('http://piloop/v1/status')
    assert (refused.status_code, refused.json()) == (
        403,
        {'detail': 'gpu1 has worker access, and this route needs read'},
    ), 'the token says who calls, wherever it comes from'


def test_on_the_network_a_caller_needs_a_token_whose_access_reads(
    db: Database, campaign: Campaign, cfg: Settings
) -> None:
    reader = token(db, 'ana', Access.READ)
    with serving_api(cfg, listen='127.0.0.1:0') as [_, tcp]:
        assert httpx.get(tcp.url + '/v1/status', timeout=TIMEOUT).status_code == 401, 'only the socket knows its owner'
        answered_ = httpx.get(tcp.url + '/v1/status', headers={'Authorization': f'Bearer {reader}'}, timeout=TIMEOUT)
        assert answered_.status_code == 200 and answered_.json()['daemon']['listen'] == tcp.url


def test_a_refusal_says_why_and_what_to_do(db: Database, campaign: Campaign, cfg: Settings) -> None:
    with serving_api(cfg), EveryClient.on(cfg.socket_file, System.HUMAN) as client:
        with pytest.raises(Refused, match="^no campaign 'nope' in .*`piloop init <path>` registers one$") as unknown:
            client.inbox(InboxQuery(campaign='nope'))
        assert unknown.value.error == Problem.UNKNOWN_CAMPAIGN
        with pytest.raises(Refused, match='^cursor 99 is ahead of the feed') as ahead:
            client.events(EventsQuery(after=99))
        assert ahead.value.error == Problem.CURSOR_AHEAD
        with pytest.raises(Refused, match='^limit: Input should be less than or equal to 1000$') as invalid:
            client.events(EventsQuery.model_construct(limit=1001))
        assert invalid.value.error is None
        with pytest.raises(Refused, match=r'^the daemon has no such route \(Not Found\): it runs an older piloop'):
            client._get('/v1/missing', Selection(), Status)


def test_a_relative_path_names_the_campaign_from_where_the_client_runs(
    db: Database, campaign: Campaign, cfg: Settings
) -> None:
    path = Path(campaign.path)
    with serving_api(cfg), EveryClient.on(cfg.socket_file, System.HUMAN) as client:
        named = client.status(Selection(campaign=path.name, cwd=str(path.parent)))
        assert [c.id for c in named.campaigns] == [campaign.id]
        inside = client.status(Selection(cwd=str(path)))
        assert [c.id for c in inside.campaigns] == [campaign.id]


def test_the_events_long_poll_answers_the_first_event_that_matches_or_its_moved_cursor_at_the_end(
    db: Database, campaign: Campaign, cfg: Settings
) -> None:
    with serving_api(cfg), EveryClient.on(cfg.socket_file, System.HUMAN) as client:
        start = client.events(EventsQuery(limit=1000)).next
        emitted(db, 'noise')
        quiet = client.events(EventsQuery(after=start, kind=('wanted',), wait=0.2))
        assert (quiet.events, quiet.next, quiet.more) == ([], start + 1, False), 'the end of the wait moves the cursor'
        threading.Timer(0.2, emitted, [db, 'wanted']).start()
        woke = client.events(EventsQuery(after=quiet.next, kind=('wanted',), wait=20))
        assert [e.kind for e in woke.events] == ['wanted'], 'another session commits: the wait wakes on it'


@pytest.mark.parametrize('poll', ['events', 'news', 'prompt'])
def test_a_long_poll_answers_its_moved_cursor_at_once_when_the_server_closes(
    db: Database, campaign: Campaign, cfg: Settings, poll: str
) -> None:
    with db.session() as s:
        s.add(AgentSession(kind='research', campaign_id=1, state=AgentSessionState.RUNNING))
        s.commit()
    with serving_api(cfg) as [server], Client.on(cfg.socket_file, System.HUMAN) as client:
        threading.Timer(0.3, server.close).start()
        began = time.monotonic()
        match poll:
            case 'events':
                assert client.events(EventsQuery(kind=('wanted',), limit=1000, wait=20)).more is False
            case 'news':
                assert client.news(NewsQuery(wait=20)).items == []
            case _:
                assert client.prompt(AgentSessionId(1), PromptBody(text='x', wait=20)).delivery == Pending()
    assert time.monotonic() - began < 15


def test_the_server_serves_the_openapi_document_of_its_routes(cfg: Settings, db: Database) -> None:
    with serving_api(cfg), EveryClient.on(cfg.socket_file, System.HUMAN) as client:
        assert client._http.get(OPENAPI).json() == openapi()
    paths = openapi()['paths']
    routers = (*ROUTERS, *collect(contributions_of(installed()), ()).routers)
    assert isinstance(paths, dict) and sorted(paths) == sorted(
        {r.path for routes in routers for r in routes.routes if isinstance(r, APIRoute)}
    )
    assert '/v1/campaigns/{campaign}/journal' in paths, 'the route of local, the default memory provider'


def test_a_route_that_crashes_is_an_answer_that_says_so(db: Database, cfg: Settings) -> None:
    def crash() -> None:
        raise RuntimeError('a bug')

    tokens_ = Tokens(db)
    server = app(tokens_, Https().open(NetworkHost(tokens_, HomeSecrets(db, LocalSecrets()))), ())
    server.add_api_route('/v1/status', crash)
    api = with_api(server, Handlers(db, cfg, daemon_state('now', None), disk(cfg.home)))
    with closing(Server(api, local_listener(cfg.socket_file))) as running:
        running.start()
        with (
            pytest.raises(Refused, match='^the daemon answered 500: Internal Server Error$'),
            Client.on(cfg.socket_file, System.HUMAN) as client,
        ):
            client.status(Selection())


@pytest.mark.parametrize(('code', 'answers'), [(errno.ENOTSOCK, False), (errno.EACCES, None)])
def test_a_file_that_is_no_socket_is_no_daemon_as_macos_says_it(
    tmp_path: Path, mocker: MockerFixture, code: int, answers: bool | None
) -> None:
    """macOS refuses to connect to a file that is no socket with ``ENOTSOCK``, where Linux refuses the connection.
    Another error is raised."""
    mocker.patch.object(client_module.sockets.socket, 'connect', side_effect=OSError(code, os.strerror(code)))
    if answers is None:
        with pytest.raises(PermissionError):
            client_module.answers(tmp_path / 'piloop.sock')
    else:
        assert client_module.answers(tmp_path / 'piloop.sock') is answers


def test_without_a_daemon_the_client_says_how_to_start_one(cfg: Settings) -> None:
    with (
        pytest.raises(NoDaemon, match='start it with `piloop serve --detach`'),
        Client.on(cfg.socket_file, System.HUMAN),
    ):
        pass
    cfg.socket_file.touch()
    with pytest.raises(NoDaemon, match='start it with'), Client.on(cfg.socket_file, System.HUMAN):
        pass


def test_each_method_of_the_client_calls_its_route_with_the_route_s_models() -> None:
    """The client and the routes share their models: a method sends its route's query model to its route's path."""
    sent: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(500, text='only the request counts')

    http = httpx.Client(transport=httpx.MockTransport(record), base_url='http://piloop')
    clients: list[tuple[APIRouter, type[Client]]] = [
        (ROUTERS[0], Client),
        (backlog_api.router, BacklogClient),
        (candidates_api.router, CandidatesClient),
        (verdicts_api.router, VerdictsClient),
        (optimization_api.router, OptimizationClient),
        (evaluation_api.router, EvaluationClient),
        (runs_api.router, RunsClient),
        (lineage_api.router, LineageClient),
    ]
    assert [r for r, _ in clients] == [*ROUTERS, *optimization_api.ROUTERS, lineage_api.router], (
        'each router has its client'
    )
    for route, client in [(r, made(http)) for routes, made in clients for r in routes.routes]:
        assert isinstance(route, APIRoute)
        hints = get_type_hints(getattr(client, route.name))
        answer, taken = hints.pop('return'), list(hints.items())
        path = _path_params(route.dependant)
        assert answer is route.response_model, route.name
        assert {n for n, _ in taken if n in path} == path, f"{route.name}: the route's path parameters"
        assert all({*_models(t)} <= _queries(route) for n, t in taken if n not in path), (
            f"{route.name}: the route's query model"
        )
        with pytest.raises(Refused):
            getattr(client, route.name)(*('1' if n in path else _models(t)[0].model_construct() for n, t in taken))
        sent_path = route.path.format(**dict.fromkeys(path, 1))
        assert ({sent[-1].method}, sent[-1].url.path) == (route.methods, sent_path), route.name


def _path_params(dependant: Dependant) -> set[str]:
    """The path parameters that the route and its dependencies take."""
    return {p.name for p in dependant.path_params}.union(*(_path_params(d) for d in dependant.dependencies))


def _queries(route: APIRoute) -> set[type]:
    """The query models that the route and its dependencies take."""
    return _taken(route.dependant)


def _taken(dependant: Dependant) -> set[type]:
    """The query models that ``dependant``'s call and its dependencies, at any depth, take."""
    hints = get_type_hints(dependant.call, include_extras=True).values() if dependant.call is not None else ()
    found = {model for hint in hints for model in _models(hint) if issubclass(model, Params)}
    return found.union(*(_taken(d) for d in dependant.dependencies))


def _models(hint: object) -> list[type[BaseModel]]:
    """The models of a hint: itself, the one that it annotates, or each of a union's, as a type alias names them."""
    match hint:
        case TypeAliasType():
            return _models(hint.__value__)
        case UnionType():
            return [model for arg in get_args(hint) for model in _models(arg)]
        case type() if issubclass(hint, BaseModel):
            return [hint]
        case _ if get_origin(hint) is Annotated:
            return _models(get_args(hint)[0])
        case _:
            return []


def comment(db: Database, author: str, body: str) -> None:
    with db.session() as s:
        s.add(
            Comment(
                target=TargetRef(TargetKind.CAMPAIGN, 1),
                author=author,
                kind=CommentKind.DECISION,
                priority=2,
                body=body,
            )
        )
        s.commit()


def test_the_news_long_poll_wakes_on_what_someone_else_writes_and_skips_what_the_caller_wrote(
    db: Database, campaign: Campaign, cfg: Settings
) -> None:
    with serving_api(cfg), EveryClient.on(cfg.socket_file, System.ORCHESTRATOR) as orchestrator:
        start = orchestrator.news(NewsQuery()).next
        comment(db, 'orchestrator', 'my own decision')
        mine = orchestrator.news(NewsQuery(after=start, wait=0.2))
        assert mine.items == [] and mine.next > start, (
            'its own writes are not news to it, and the cursor moves past them'
        )
        threading.Timer(0.2, comment, [db, 'watchdog#2', 'look at this']).start()
        [item] = orchestrator.news(NewsQuery(after=mine.next, wait=20)).items
        assert item.text() == 'comment#2 decision p2 on campaign#1 (demo) by watchdog#2: look at this'
        title, lines = thread_lines(orchestrator.thread(CommentId(2)), datetime.now(UTC))
        assert title.startswith('comment#2 decision on campaign#1 by watchdog#2, ') and lines == ['look at this']
        with pytest.raises(Refused, match='^no comment#99$') as unknown:
            orchestrator.thread(CommentId(99))
        assert unknown.value.error == Problem.UNKNOWN_COMMENT


def test_a_write_answers_its_cursor_and_refuses_what_its_operation_refuses(
    db: Database, campaign: Campaign, cfg: Settings
) -> None:
    with serving_api(cfg), EveryClient.on(cfg.socket_file, System.HUMAN) as client:
        added = client.add_idea('demo', IdeaBody(title='try a cache', priority=1))
        assert (added.idea.title, added.idea.status, added.idea.created_by, added.review) == (
            'try a cache',
            'ready',
            'human',
            None,
        )
        assert client.events(EventsQuery(after=added.cursor)).events == [], 'the cursor passes the write itself'
        paused = client.pause('demo', PauseBody(reason='a review'))
        with pytest.raises(Refused, match=r'^campaign demo is paused already') as twice:
            client.pause('demo', PauseBody(reason='again'))
        assert twice.value.error == Problem.REFUSED
        resumed = client.resume('demo').campaign
        assert isinstance(resumed, CampaignState) and isinstance(paused.campaign, CampaignState)
        assert resumed.paused is None and paused.campaign.paused == 'launches'
        stopped = client.stop('demo', StopBody(reason='done'))
        assert stopped.cursor > paused.cursor
        with pytest.raises(Refused, match='^no campaign .nope.') as unknown:
            client.resume('nope')
        assert unknown.value.error == Problem.UNKNOWN_CAMPAIGN
        with pytest.raises(Refused, match='^no comment#9$') as missing:
            client.reply(CommentId(9), ReplyBody(body='hm'))
        assert missing.value.error == Problem.UNKNOWN_COMMENT
        with pytest.raises(Refused, match='^no idea#9$') as no_idea:
            client.set_status(IdeaId(9), StatusBody(status=IdeaStatus.PARKED, reason='hm'))
        with pytest.raises(Refused, match='^no idea#9$') as no_target:
            client.link(added.idea.id, LinkKind.AFTER, IdeaId(9), Reasoned(reason='hm'))
        assert no_idea.value.error == no_target.value.error == Problem.UNKNOWN_IDEA
        with pytest.raises(Refused, match='^no candidate#9$') as no_candidate:
            client.remeasure(CandidateId(9), RemeasureBody())
        assert no_candidate.value.error == Problem.UNKNOWN_CANDIDATE
        [result] = client.ack(AckBody(ids=(CommentId(9),))).results
        assert result.line() == 'comment#9 is not in the inbox'


def first(client: Client, after: int, *kinds: str) -> EventModel:
    """The payload of the first event of ``kinds`` after the cursor ``after``, once it comes."""
    while not (batch := client.events(EventsQuery(after=after, kind=kinds, wait=MAX_POLL_SECONDS))).events:
        after = batch.next
    return EVENT.validate_python(batch.events[0].payload)


def test_a_validation_refusal_says_where_and_why_and_never_echoes_what_was_sent(db: Database, cfg: Settings) -> None:
    sent = 'sent-value'
    with (
        serving_api(cfg),
        httpx.Client(transport=httpx.HTTPTransport(uds=str(cfg.socket_file)), base_url='http://piloop') as raw,
    ):
        answer = raw.post('/v1/campaigns', json={'clone': {'url': [sent]}})
    assert answer.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    [error] = answer.json()['detail']
    assert (error['loc'][-1], error['msg']) == ('url', 'Input should be a valid string')
    assert 'input' not in error and sent not in answer.text


def outcome(db: Database, comment: CommentId) -> None:
    with db.session() as s:
        queued = PromptOutcome(kind='comment.queued', comment=comment)
        emit(s, queued, TargetRef(TargetKind.AGENT_SESSION, 1), System.DAEMON)
        s.commit()


def test_a_write_needs_act_access_and_a_caller_whose_name_signs(
    db: Database, campaign: Campaign, cfg: Settings
) -> None:
    reader = {'Authorization': f'Bearer {token(db, "ana", Access.READ)}'}
    spaced = {'Authorization': f'Bearer {token(db, "two words", Access.ACT)}'}
    with serving_api(cfg, listen='127.0.0.1:0') as [_, tcp]:
        refused = httpx.post(tcp.url + '/v1/campaigns/demo/resume', headers=reader, timeout=TIMEOUT)
        unsigned = httpx.post(tcp.url + '/v1/campaigns/demo/resume', headers=spaced, timeout=TIMEOUT)
    assert refused.json() == {'detail': 'ana has read access, and this route needs act'}
    assert unsigned.json() == {
        'error': 'forbidden',
        'detail': "'two words' is no name that signs a write: name the caller another way",
    }


def test_a_write_that_waits_out_the_busy_timeout_answers_503_and_when_to_retry(
    db: Database, campaign: Campaign, cfg: Settings, monkeypatch: pytest.MonkeyPatch, writer: subprocess.Popen[str]
) -> None:
    pragmas = tuple(p for p in sql.CONNECTION_PRAGMAS if not p.startswith('busy_timeout='))
    monkeypatch.setattr(session, 'CONNECTION_PRAGMAS', ('busy_timeout=0', *pragmas))
    with serving_api(cfg), EveryClient.on(cfg.socket_file, System.HUMAN) as client:
        with pytest.raises(Refused) as refused:
            client.message('demo', MessageBody(body='hello'))
        transport = httpx.HTTPTransport(uds=str(cfg.socket_file))
        with httpx.Client(transport=transport, base_url='http://piloop') as raw:
            answer = raw.post('/v1/campaigns/demo/messages', json={'body': 'hello'})
    assert (answer.status_code, answer.headers['retry-after']) == (HTTPStatus.SERVICE_UNAVAILABLE, '5')
    assert (refused.value.error, str(refused.value)) == (
        Problem.BUSY,
        'the database is busy: another connection held its write lock for the whole 5 s wait; retry in 5 s',
    )


def test_with_a_server_set_the_commands_call_it_through_the_network_with_its_credential(
    db: Database, campaign: Campaign, cfg: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(TOKEN_ENV, token(db, 'laptop', Access.READ))
    with serving_api(cfg, listen='127.0.0.1:0') as [_, tcp]:
        monkeypatch.setattr(common, 'settings', Settings(home=cfg.home / 'elsewhere', server=tcp.url))
        monkeypatch.setattr(common, 'console', Console(width=200, force_terminal=False))
        cli.app(['status', 'demo', '--json'], result_action='return_value', exit_on_error=False)
    assert '"name": "demo"' in capsys.readouterr().out, 'a name resolves on the server; no socket of this home'
