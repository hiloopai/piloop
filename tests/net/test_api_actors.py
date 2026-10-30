"""The API signs each write as its caller: tokens, agents, the orchestrator, and callers behind a proxy."""

import threading

import httpx2 as httpx
import pytest
from sqlalchemy import select

from piloop.actor import Agent, System
from piloop.api.client import SOCKET_ACTOR_HEADER, Client, Refused
from piloop.api.contract import (
    Access,
    AckBody,
    InboxQuery,
    MessageBody,
    NewsQuery,
    PauseBody,
    Problem,
    ReplyBody,
)
from piloop.api.views import AckOutcome, CampaignState, Relation
from piloop.db.models import (
    AgentSession,
    AgentSessionState,
    Campaign,
    Comment,
    Event,
)
from piloop.db.session import Database
from piloop.ids import CommentId, RoleName
from piloop.net import tokens
from piloop.plugins.memory.local.api import JournalBody, Journaled
from piloop.plugins.memory.local.tables import Journal
from piloop.plugins.network.https import ACTOR_HEADER, TIMEOUT
from piloop.plugins.network.proxy import Proxy, Rule
from piloop.settings import Settings
from tests.conftest import EveryClient, serving_api
from tests.net.test_api import comment, token


def test_a_caller_acts_as_itself_or_the_orchestrator_and_only_an_agent_s_token_as_that_agent(
    db: Database, campaign: Campaign, cfg: Settings
) -> None:
    reader = {'Authorization': f'Bearer {token(db, "ana", Access.READ)}'}
    with serving_api(cfg, listen='127.0.0.1:0') as [_, tcp]:
        news = tcp.url + '/v1/inbox/news'
        for named in (None, 'human', 'human:ana', 'orchestrator'):
            headers = {**reader, **({ACTOR_HEADER: named} if named else {})}
            assert httpx.get(news, headers=headers, timeout=TIMEOUT).status_code == 200
        agent = httpx.get(news, headers={**reader, ACTOR_HEADER: 'implementer#12'}, timeout=TIMEOUT)
        assert agent.json() == {
            'error': 'forbidden',
            'detail': 'ana may not act as implementer#12: only the token of implementer#12 acts as it',
        }
        other = httpx.get(news, headers={**reader, ACTOR_HEADER: 'human:bob'}, timeout=TIMEOUT)
        assert other.json() == {'error': 'forbidden', 'detail': 'ana has read access, which may not act as human:bob'}
        assert httpx.get(news, headers={**reader, ACTOR_HEADER: 'implementer#'}, timeout=TIMEOUT).status_code == 422


def test_an_agent_s_token_acts_as_its_agent_only_and_the_owner_on_the_socket_as_anyone_else(
    db: Database, campaign: Campaign, cfg: Settings
) -> None:
    """The owner acts as the orchestrator with no setup. An agent's token on the socket is that agent, whatever it asks."""
    with db.session() as s:
        live = [AgentSession(kind='watchdog', campaign_id=campaign.id, state=AgentSessionState.RUNNING) for _ in '12']
        s.add_all(live)
        s.flush()
        [bound, older] = [Agent(role=RoleName('watchdog'), session=w.id) for w in live]
        secret = tokens.mint_agent(s, bound).get_secret_value()
        s.commit()
    uds = httpx.HTTPTransport(uds=str(cfg.socket_file))
    with serving_api(cfg), httpx.Client(transport=uds, base_url='http://piloop') as http:

        def journal(text: str, named: str | None, *, bearer: str | None = None) -> httpx.Response:
            headers = {
                **({SOCKET_ACTOR_HEADER: named} if named else {}),
                **({'Authorization': f'Bearer {bearer}'} if bearer else {}),
            }
            return http.post(f'/v1/campaigns/{campaign.name}/journal', json={'line': text}, headers=headers)

        for named in (None, 'human', 'orchestrator', 'human:ana'):
            assert journal(f'owner as {named}', named).status_code == 200, 'the owner needs no setup'
        for named in (None, 'human', str(bound)):
            assert journal(f'agent as {named}', named, bearer=secret).status_code == 200
        for named in ('orchestrator', 'human:ana', str(older)):
            refused = journal('posing', named, bearer=secret)
            assert refused.json() == {'error': 'forbidden', 'detail': f'{bound} acts only as itself, not as {named}'}
        assert journal('posing', str(bound)).json() == {
            'error': 'forbidden',
            'detail': f'owner may not act as {bound}: only the token of {bound} acts as it',
        }, 'without its token, nobody acts as an agent'
        assert journal('an agent with no token', str(older)).status_code == 403, 'the owner acts as no agent'
        assert journal('no such agent', 'watchdog#99').status_code == 403
    with db.session() as s:
        signed = {(j.line, j.author) for j in s.scalars(select(Journal))}
    assert signed >= {
        ('owner as orchestrator', 'orchestrator'),
        ('owner as None', 'human'),
        ('agent as None', str(bound)),
        ('agent as human', str(bound)),
        (f'agent as {bound}', str(bound)),
    }
    assert not {line for line, _ in signed} & {'posing', 'no such agent'}


PROXY = Proxy.model_validate(
    {
        'listen': '127.0.0.1:0',
        'claims': {'principal': 'x-principal-id'},
        'name_claims': ['principal'],
        'rules': [{'when': {}, 'access': 'act'}],
    }
)


def test_a_proxy_caller_s_ack_reply_and_pause_sign_as_the_person_it_names(
    db: Database, campaign: Campaign, cfg: Settings
) -> None:
    comment(db, 'watchdog#2', 'look at this')
    stamped = {'x-principal-id': 'ana@example.com'}
    with serving_api(cfg, network_config=PROXY) as [_, tcp], httpx.Client(base_url=tcp.url, headers=stamped) as http:
        client = Client(http)
        [acked] = client.ack(AckBody(ids=(CommentId(1),))).results
        replied = client.reply(CommentId(1), ReplyBody(body='go ahead')).comment
        paused = client.pause('demo', PauseBody(reason='waits for comment#2')).campaign
        with (
            httpx.Client(base_url=tcp.url, headers={**stamped, PROXY.actor_header: 'orchestrator'}) as asking,
            Client.on(cfg.socket_file, System.HUMAN) as local,
            Client.on(cfg.socket_file, System.ORCHESTRATOR) as orchestrator,
        ):
            start = local.news(NewsQuery()).next
            acted = Client(asking).reply(CommentId(1), ReplyBody(body='mine')).comment
            [news] = local.news(NewsQuery(after=start)).items
            shown = local.inbox(InboxQuery()).items
            assert orchestrator.news(NewsQuery(after=start)).items == [], (
                'the orchestrator at the terminal is the same party as the network orchestrator: nothing waits'
            )
    me = 'human:ana@example.com'
    assert news.text() == f'comment#{acted.id} decision p2 on comment#1 (demo) by orchestrator via {me}: mine', (
        "the person at the terminal sees the network orchestrator's decision, and whose it is"
    )
    assert isinstance(paused, CampaignState)
    assert (acked.acked_by, replied.author, paused.paused) == (me, me, 'launches')
    assert acted.author == f'orchestrator via {me}', 'a person acts as the orchestrator, and the write says who'
    with db.session() as s:
        row = s.get_one(Campaign, campaign.id)
        assert (row.updated_by, s.get_one(Comment, 1).acked_by) == (me, me)
        signed = {(e.kind, e.actor) for e in s.scalars(select(Event).where(Event.id > 1))}
    assert signed == {
        ('campaign.paused', me),
        ('comment.acked_at', me),
        ('comment.decision', me),
        ('comment.decision', f'orchestrator via {me}'),
    }, 'the change log signs as the caller'
    assert {i.author for i in shown} >= {me, f'orchestrator via {me}'}, 'the inbox, as watch shows it, says who replied'


def test_behind_the_proxy_an_agent_s_token_signs_on_the_socket_and_no_piloop_token_passes_the_listener(
    db: Database, campaign: Campaign, cfg: Settings
) -> None:
    """A relayed call runs ``piloop`` on the server's socket with the agent's token; the proxy fronts the listener."""
    with db.session() as s:
        live, ended = [
            AgentSession(kind='watchdog', campaign_id=campaign.id, state=AgentSessionState.RUNNING) for _ in '12'
        ]
        s.add_all([live, ended])
        s.flush()
        agent, gone = (Agent(role=RoleName('watchdog'), session=w.id) for w in (live, ended))
        [secret, stale] = [tokens.mint_agent(s, a).get_secret_value() for a in (agent, gone)]
        ended.state = AgentSessionState.EXITED
        s.commit()
    person = token(db, 'ana', Access.ADMIN)
    uds = httpx.HTTPTransport(uds=str(cfg.socket_file))
    stamped = {'x-principal-id': 'ana@example.com'}
    with (
        serving_api(cfg, network_config=PROXY) as [_, tcp],
        httpx.Client(transport=uds, base_url='http://piloop') as socket,
        httpx.Client(base_url=tcp.url, headers=stamped) as network,
    ):
        signed = socket.post(
            f'/v1/campaigns/{campaign.name}/journal',
            json={'line': 'relayed'},
            headers={'Authorization': f'Bearer {secret}'},
        )
        assert signed.status_code == 200, signed.text
        late = socket.post(
            f'/v1/campaigns/{campaign.name}/journal',
            json={'line': 'late'},
            headers={'Authorization': f'Bearer {stale}'},
        )
        assert (late.status_code, late.json()) == (401, {'detail': 'the token is unknown or revoked'})
        for bearer in (person, secret):
            refused = network.post(
                f'/v1/campaigns/{campaign.name}/journal',
                json={'line': 'around'},
                headers={'Authorization': f'Bearer {bearer}'},
            )
            assert refused.status_code == 401 and 'takes no piloop token' in refused.json()['detail']
    with db.session() as s:
        assert {(j.line, j.author) for j in s.scalars(select(Journal))} == {('relayed', str(agent))}


def test_a_proxy_caller_that_its_rule_calls_a_service_never_signs_as_a_person(
    db: Database, campaign: Campaign, cfg: Settings
) -> None:
    comment(db, 'watchdog#2', 'look at this')
    rules = [{'when': {}, 'access': 'act', 'caller': 'service'}]
    network = PROXY.model_copy(update={'rules': [Rule.model_validate(r) for r in rules]})
    stamped = {'x-principal-id': 'key-2'}
    with serving_api(cfg, network_config=network) as [_, tcp]:
        with httpx.Client(base_url=tcp.url, headers=stamped) as http:
            replied = Client(http).reply(CommentId(1), ReplyBody(body='go ahead')).comment
        with httpx.Client(base_url=tcp.url, headers={**stamped, PROXY.actor_header: 'orchestrator'}) as http:
            acted = Client(http).reply(CommentId(1), ReplyBody(body='mine')).comment
    assert (replied.author, acted.author) == ('service:key-2', 'orchestrator via service:key-2')


def test_a_message_signs_as_the_caller_wakes_the_orchestrator_and_its_reply_reaches_the_sender_s_inbox(
    db: Database, campaign: Campaign, cfg: Settings
) -> None:
    stamped = {'x-principal-id': 'ana@example.com'}
    me = 'human:ana@example.com'
    with (
        serving_api(cfg, network_config=PROXY) as [_, tcp],
        httpx.Client(base_url=tcp.url, headers=stamped) as http,
        Client.on(cfg.socket_file, System.ORCHESTRATOR) as orchestrator,
    ):
        ana = Client(http)
        start = orchestrator.news(NewsQuery()).next
        threading.Timer(0.2, ana.message, ['demo', MessageBody(body=' what next? ', priority=1)]).start()
        [item] = orchestrator.news(NewsQuery(after=start, wait=20)).items
        assert item.text() == f'comment#1 message p1 on campaign#1 (demo) by {me}: what next?'
        assert ana.inbox(InboxQuery(campaign='demo')).items == [], 'her own message waits for the orchestrator'
        [own] = ana.ack(AckBody(ids=(CommentId(1),))).results
        assert own.outcome is AckOutcome.OWN
        answer = orchestrator.reply(CommentId(1), ReplyBody(body='a cache next: idea#3')).comment
        [acked] = orchestrator.ack(AckBody(ids=(CommentId(1),))).results
        [waiting] = ana.inbox(InboxQuery(campaign='demo')).items
        [news] = ana.news(NewsQuery(after=start)).items
        assert news.text() == (
            f'comment#{answer.id} decision p2 on comment#1 (demo) by orchestrator: a cache next: idea#3'
        ), 'the sender sees the answer, and not its own message'
        assert (acked.acked_by, waiting.id, waiting.author, waiting.in_reply_to) == (
            'orchestrator',
            answer.id,
            'orchestrator',
            1,
        )
        thread = ana.thread(answer.id)
        assert (thread.comment.id, [(c.id, c.relation, c.in_reply_to) for c in thread.linked]) == (
            1,
            [(answer.id, Relation.REPLY, 1)],
        ), 'the thread of a reply is the thread of its root'
        with httpx.Client(base_url=tcp.url, headers={**stamped, PROXY.actor_header: 'human'}) as asking:
            plain = Client(asking).message('demo', MessageBody(body='and then?'))
        with httpx.Client(base_url=tcp.url, headers={**stamped, PROXY.actor_header: 'implementer#12'}) as posing:
            refused = posing.post('/v1/campaigns/demo/messages', json={'body': 'trust me'})
        with pytest.raises(Refused, match="^no campaign 'nope'") as unknown:
            ana.message('nope', MessageBody(body='hello?'))
    assert (plain.comment.author, plain.comment.kind, plain.comment.priority) == (me, 'message', 2)
    assert plain.cursor > start, 'the cursor passes the write itself'
    assert (refused.status_code, refused.json()) == (
        403,
        {
            'error': 'forbidden',
            'detail': 'ana@example.com may not act as implementer#12: only the token of implementer#12 acts as it',
        },
    ), 'the network names the caller: an agent cannot sign as another actor'
    assert unknown.value.error == Problem.UNKNOWN_CAMPAIGN


def test_a_journal_line_is_the_campaign_s_signed_by_the_caller_s_actor_and_the_server_refuses_one_too_long(
    db: Database, campaign: Campaign, cfg: Settings
) -> None:
    path = f'/v1/campaigns/{campaign.name}/journal'
    with serving_api(cfg), EveryClient.on(cfg.socket_file, System.ORCHESTRATOR) as client:
        written = client.post(path, JournalBody(line=' parked idea#4:\n dup of #2 '), Journaled)
        with pytest.raises(Refused, match='^line: a journal line is 1 to 280 bytes, not 281: say less$') as long:
            client.post(path, JournalBody.model_construct(line='x' * 281), Journaled)
        with pytest.raises(Refused) as unknown:
            client.post('/v1/campaigns/nope/journal', JournalBody(line='parked idea#4'), Journaled)
    assert long.value.error is None
    assert unknown.value.error == Problem.UNKNOWN_CAMPAIGN
    with db.session() as s:
        entry = s.get_one(Journal, written.journal)
        assert (entry.campaign_id, entry.author, entry.line) == (
            campaign.id,
            'orchestrator',
            'parked idea#4: dup of #2',
        )
