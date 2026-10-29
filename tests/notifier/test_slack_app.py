"""The ``slack_app`` notifier, against a local Web API and Socket Mode endpoint: no test reaches Slack."""

import sys
import threading
import time
from collections.abc import Iterator

import pytest
from inline_snapshot import snapshot
from pydantic import SecretStr

from piloop.actor import Person, System
from piloop.db.session import Database
from piloop.ids import CommentId, CommentKind
from piloop.plugins.contribution import Leaving
from piloop.plugins.escalation.contract import Choice, EscalationView
from piloop.plugins.host import Check
from piloop.plugins.notifier.contract import (
    Accepted,
    Ack,
    Again,
    Answered,
    Choose,
    Declined,
    Edit,
    FileSeed,
    Foreign,
    Inbound,
    ListenHost,
    MessageRef,
    NotDelivered,
    Notice,
    NoticeKind,
    Receipt,
    Reply,
)
from piloop.plugins.notifier.slack_app import HINT, SlackApp, ts_of
from piloop.plugins.secrets.contract import HomeSecrets, set_secret
from piloop.plugins.secrets.local import LocalSecrets
from tests.conftest import wait_until
from tests.fixtures.slack import FakeSlackApp, fake_slack_app


@pytest.fixture
def slack() -> Iterator[FakeSlackApp]:
    with fake_slack_app() as fake:
        yield fake


@pytest.fixture
def secrets(db: Database) -> HomeSecrets:
    local = LocalSecrets()
    with db.session() as s:
        set_secret(s, local, System.HUMAN, None, 'SLACK_BOT_TOKEN', SecretStr('xoxb-bot'))
        set_secret(s, local, System.HUMAN, None, 'SLACK_APP_TOKEN', SecretStr('xapp-app'))
        s.commit()
    return HomeSecrets(db, local)


def app_of(slack: FakeSlackApp, **keys: object) -> SlackApp:
    return SlackApp.model_validate({'channel': 'C1', 'api': slack.api, 'people': {'alice': 'UALICE'}, **keys})


def notice(comment: int, kind: NoticeKind = NoticeKind.NEW, thread: MessageRef | None = None) -> Notice:
    return Notice(
        kind, CommentId(comment), CommentKind.ESCALATION, 1, 'enwik8', 'implementer#1', 'x', None, None, thread
    )


def test_each_notice_is_a_post_of_its_own_and_an_answer_goes_to_its_thread(
    slack: FakeSlackApp, secrets: HomeSecrets
) -> None:
    app = app_of(slack)
    posts = app.send([notice(7), notice(8, NoticeKind.FOLLOW_UP, MessageRef('C1/1.000100'))], secrets)
    assert posts == [MessageRef('C1/1.000100'), MessageRef('C1/2.000100')]
    assert [(m, b.get('thread_ts'), auth) for m, b, auth in slack.calls] == [
        ('chat.postMessage', None, 'Bearer xoxb-bot'),
        ('chat.postMessage', '1.000100', 'Bearer xoxb-bot'),
    ]
    assert str(slack.calls[1][1]['text']).startswith(':speech_balloon: *enwik8* · escalation comment#8 · P1')
    assert isinstance(posts[1], MessageRef) and ts_of(posts[1]) == '2.000100'


def test_a_failed_post_says_why_and_what_the_posts_before_it_became(slack: FakeSlackApp, secrets: HomeSecrets) -> None:
    slack.answers['chat.postMessage'] = [
        (200, {'ok': True, 'channel': 'C1', 'ts': '5.1'}),
        (200, {'ok': False, 'error': 'not_in_channel'}),
    ]
    with pytest.raises(NotDelivered, match='^chat.postMessage: not_in_channel$') as failed:
        app_of(slack).send([notice(7), notice(8), notice(9)], secrets)
    assert failed.value.delivered == (MessageRef('C1/5.1'),)
    slack.answers['chat.postMessage'] = [(429, {'ok': False}), (200, {'ok': True})]
    with pytest.raises(NotDelivered, match='^HTTP 429') as limited:
        app_of(slack).send([notice(7)], secrets)
    assert limited.value.delivered == ()
    with pytest.raises(NotDelivered, match='^chat.postMessage: the answer names no channel and ts$'):
        app_of(slack).send([notice(7)], secrets)
    slack.answers['chat.postMessage'] = [(200, {'ok': False})]
    with pytest.raises(NotDelivered, match='^chat.postMessage: not ok$'):
        app_of(slack).send([notice(7)], secrets)


def test_a_post_that_cannot_connect_or_has_no_token_is_not_delivered(db: Database, secrets: HomeSecrets) -> None:
    with pytest.raises(NotDelivered, match='^chat.postMessage: ConnectError: ') as failed:
        SlackApp(channel='C1', api='http://127.0.0.1:1/api/', timeout=1).send([notice(7)], secrets)
    assert 'xoxb-bot' not in str(failed.value)
    with pytest.raises(
        NotDelivered, match='^the home has no secret NO_TOKEN: set it with `piloop secret set NO_TOKEN`'
    ):
        SlackApp.model_validate({'channel': 'C1', 'token': {'secret': 'NO_TOKEN'}}).send([notice(7)], secrets)


ASKED = EscalationView(
    headline='Do you raise the timeout?',
    why='Each run takes 3 hours. The timeout is 1 hour.',
    choices=(
        Choice(id='A', label='Raise it to 4 hours.', result='Each run costs more.'),
        Choice(id='B', label='Keep it.', result='The idea stops.'),
    ),
    recommended='A',
    details='run#41 timed out at a1b2c3d <after> 1 h & more.',
)


def escalation(comment: int, view: EscalationView | None = ASKED, kind: NoticeKind = NoticeKind.NEW) -> Notice:
    return Notice(
        kind,
        CommentId(comment),
        CommentKind.ESCALATION,
        1,
        'enwik8',
        'orchestrator',
        'x',
        Person(name='alice'),
        None,
        view=view,
    )


def test_an_escalation_with_choices_is_block_kit_with_a_button_for_each_and_the_ids_out_of_the_main_text(
    slack: FakeSlackApp, secrets: HomeSecrets
) -> None:
    app = app_of(slack)
    assert app.blocks(escalation(7, kind=NoticeKind.REMINDER), ASKED) == snapshot(
        [
            {'type': 'header', 'text': {'type': 'plain_text', 'text': 'Do you raise the timeout?'}},
            {'type': 'section', 'text': {'type': 'mrkdwn', 'text': 'Each run takes 3 hours. The timeout is 1 hour.'}},
            {
                'type': 'section',
                'text': {
                    'type': 'mrkdwn',
                    'text': """\
*A.* Raise it to 4 hours. _(recommended)_ → Each run costs more.
*B.* Keep it. → The idea stops.\
""",
                },
            },
            {
                'type': 'actions',
                'elements': [
                    {
                        'type': 'button',
                        'text': {'type': 'plain_text', 'text': 'A: Raise it to 4 hours.'},
                        'action_id': 'piloop.choose.A',
                        'value': 'A',
                        'style': 'primary',
                    },
                    {
                        'type': 'button',
                        'text': {'type': 'plain_text', 'text': 'B: Keep it.'},
                        'action_id': 'piloop.choose.B',
                        'value': 'B',
                    },
                ],
            },
            {
                'type': 'context',
                'elements': [
                    {
                        'type': 'mrkdwn',
                        'text': ':alarm_clock: reminder · *enwik8* · P1 · escalation comment#7 · from orchestrator · <@UALICE> · `piloop inbox show 7`',
                    }
                ],
            },
            {
                'type': 'section',
                'text': {
                    'type': 'mrkdwn',
                    'text': """\
*Details*
run#41 timed out at a1b2c3d &lt;after&gt; 1 h &amp; more.\
""",
                },
            },
        ]
    )
    free_text = ASKED.model_copy(update={'choices': (), 'recommended': None})
    app.send([escalation(7), escalation(8, free_text), escalation(9, None)], secrets)
    posted = [body for _, body, _ in slack.calls]
    assert [('blocks' in body, str(body['text']).split(' · ')[0]) for body in posted] == [
        (True, ':rotating_light: *enwik8*'),
        (False, ':rotating_light: *enwik8*'),
        (False, ':rotating_light: *enwik8*'),
    ], 'text stays the fallback; only an escalation that offers choices has buttons'


def test_the_blocks_keep_within_slack_s_limits() -> None:
    long = 'word ' * 700
    view = EscalationView(
        headline=long,
        why=long,
        choices=tuple(Choice(id=c, label=long, result=long) for c in 'AB'),
        details=long,
    )
    blocks = SlackApp(channel='C1').blocks(escalation(7, view), view)
    header, why, choices, actions, _, details = blocks
    assert [len(text_of(b)) for b in (header, why, choices, details)] == [150, 3000, 3000, 3000]
    match actions:
        case {'elements': [dict(a), dict(b)]}:
            assert [len(text_of(a)), len(text_of(b))] == [75, 75]
        case _:
            pytest.fail(f'no two buttons: {actions}')


def test_an_escalation_without_a_why_or_details_has_no_block_for_them() -> None:
    view = ASKED.model_copy(update={'why': '', 'details': ''})
    blocks = SlackApp(channel='C1').blocks(escalation(7, view), view)
    assert [b['type'] for b in blocks] == ['header', 'section', 'actions', 'context']


def text_of(block: dict[str, object]) -> str:
    match block:
        case {'text': {'text': str(text)}}:
            return text
        case _:
            raise AssertionError(f'no text: {block}')


CLICK: dict[str, object] = {
    'type': 'block_actions',
    'user': {'id': 'UALICE'},
    'container': {'type': 'message', 'channel_id': 'C1', 'message_ts': '5.1'},
    'message': {'text': 'fallback', 'blocks': [{'type': 'header'}, {'type': 'actions', 'elements': []}]},
    'actions': [{'action_id': 'piloop.choose.A', 'value': 'A', 'action_ts': '9.9', 'type': 'button'}],
}


@pytest.mark.parametrize(
    ('payload', 'found'),
    [
        (
            CLICK,
            Inbound(MessageRef('C1/5.1'), MessageRef('C1/5.1/choose/UALICE/9.9'), 'UALICE', Choose('A')),
        ),
        ({**CLICK, 'actions': [{'action_id': 'other', 'value': 'A', 'action_ts': '9.9'}]}, None),
        ({**CLICK, 'type': 'view_submission'}, None),
    ],
)
def test_a_click_on_a_choice_s_button_chooses_it(payload: dict[str, object], found: Inbound | None) -> None:
    click = SlackApp(channel='C1').clicked(payload)
    assert (click.inbound() if click is not None else None) == found


@pytest.mark.parametrize(
    ('receipt', 'updated'),
    [
        (Accepted('piloop recorded comment#3, Chose A: Raise it.'), ':white_check_mark: <@UALICE> chose *A*'),
        (
            Answered('comment#1 was answered already', 'human:bob', 'Chose B: Keep <it> & go.'),
            ':white_check_mark: Answered by human:bob: Chose B: Keep &lt;it&gt; &amp; go.',
        ),
        (
            Answered('comment#1 was acknowledged already', 'orchestrator', None),
            ':white_check_mark: Answered by orchestrator',
        ),
        (Declined('you are not in people'), None),
    ],
)
def test_a_choice_replaces_the_buttons_with_who_answered_and_the_person_is_told(
    slack: FakeSlackApp, secrets: HomeSecrets, receipt: Accepted | Answered | Declined, updated: str | None
) -> None:
    heard = Heard(secrets, receipt)
    listener = app_of(slack).listen(heard.host)
    listener.start()
    try:
        wait_until(lambda: slack.connections == 1, 'the socket')
        slack.push({'envelope_id': 'env-1', 'type': 'interactive', 'payload': CLICK})
        wait_until(lambda: any(m == 'chat.postEphemeral' for m, _, _ in slack.calls), 'the receipt')
        assert {'envelope_id': 'env-1'} in slack.frames
        assert [i.act for i in heard.inbound] == [Choose('A')]
        calls = {m: body for m, body, _ in slack.calls}
        assert (calls['chat.postEphemeral']['user'], calls['chat.postEphemeral']['text']) == ('UALICE', _said(receipt))
        assert 'thread_ts' not in calls['chat.postEphemeral'], 'beside the post, where the person clicked'
        if updated is not None:
            assert calls['chat.update'] == {
                'channel': 'C1',
                'ts': '5.1',
                'text': 'fallback',
                'blocks': [{'type': 'header'}, {'type': 'context', 'elements': [{'type': 'mrkdwn', 'text': updated}]}],
            }
        else:
            assert 'chat.update' not in calls
    finally:
        listener.stop(Leaving.STOP, 1)


def test_a_post_that_cannot_be_updated_is_a_failure_of_the_listener(slack: FakeSlackApp, secrets: HomeSecrets) -> None:
    heard = Heard(secrets, Accepted('recorded'))
    slack.answers['chat.update'] = [(200, {'ok': False, 'error': 'cant_update_message'})]
    listener = app_of(slack).listen(heard.host)
    listener.start()
    try:
        wait_until(lambda: slack.connections == 1, 'the socket')
        slack.push({'envelope_id': 'env-1', 'type': 'interactive', 'payload': {'type': 'shortcut'}})
        slack.push({'envelope_id': 'env-2', 'type': 'interactive', 'payload': CLICK})
        wait_until(lambda: heard.failures != [], 'the failure')
        assert heard.failures == ['could not answer in Slack: chat.update: cant_update_message']
        assert len(heard.inbound) == 1, 'an action that is no choice is acked, and does nothing'
    finally:
        listener.stop(Leaving.STOP, 1)


def test_a_redelivered_click_changes_nothing_in_slack(slack: FakeSlackApp, secrets: HomeSecrets) -> None:
    heard = Heard(secrets, Again())
    listener = app_of(slack).listen(heard.host)
    listener.start()
    try:
        wait_until(lambda: slack.connections == 1, 'the socket')
        slack.push({'envelope_id': 'env-1', 'type': 'interactive', 'payload': CLICK})
        wait_until(lambda: len(heard.inbound) == 1, 'the click')
        assert [m for m, _, _ in slack.calls] == ['apps.connections.open']
    finally:
        listener.stop(Leaving.STOP, 1)


def _said(receipt: Accepted | Answered | Declined) -> str:
    return receipt.reason if isinstance(receipt, Declined) else receipt.said


@pytest.mark.parametrize(
    ('event', 'found'),
    [
        (
            {
                'type': 'message',
                'user': 'UALICE',
                'channel': 'C1',
                'ts': '9.1',
                'thread_ts': '5.1',
                'text': ' raise it ',
            },
            Inbound(MessageRef('C1/5.1'), MessageRef('C1/9.1'), 'UALICE', Reply('raise it', 2)),
        ),
        (
            {
                'type': 'message',
                'user': 'UBOB',
                'channel': 'C1',
                'ts': '9.1',
                'thread_ts': '5.1',
                'text': 'Idea: halve it',
            },
            Inbound(MessageRef('C1/5.1'), MessageRef('C1/9.1'), 'UBOB', FileSeed('halve it')),
        ),
        ({'type': 'message', 'user': 'UALICE', 'channel': 'C1', 'ts': '9.1', 'text': 'hello'}, 'hint'),
        ({'type': 'message', 'subtype': 'bot_message', 'channel': 'C1', 'ts': '9.1', 'text': 'x'}, None),
        (
            {
                'type': 'message',
                'user': 'UB',
                'bot_id': 'B1',
                'channel': 'C1',
                'ts': '9.1',
                'text': 'x',
                'thread_ts': '5',
            },
            None,
        ),
        (
            {
                'type': 'reaction_added',
                'user': 'UALICE',
                'reaction': 'white_check_mark',
                'item': {'type': 'message', 'channel': 'C1', 'ts': '5.1'},
                'event_ts': '9.9',
            },
            Inbound(MessageRef('C1/5.1'), MessageRef('C1/5.1/white_check_mark/UALICE/9.9'), 'UALICE', Ack()),
        ),
        (
            {
                'type': 'reaction_added',
                'user': 'UALICE',
                'reaction': 'eyes',
                'item': {'type': 'message', 'channel': 'C1', 'ts': '5.1'},
                'event_ts': '9.9',
            },
            None,
        ),
        (
            {
                'type': 'message',
                'subtype': 'message_changed',
                'channel': 'C1',
                'message': {'user': 'UALICE', 'ts': '9.1', 'thread_ts': '5.1', 'text': ' raise it to 4 h '},
            },
            Inbound(MessageRef('C1/5.1'), MessageRef('C1/9.1'), 'UALICE', Edit('raise it to 4 h')),
        ),
        (
            {
                'type': 'message',
                'subtype': 'message_changed',
                'channel': 'C1',
                'message': {'user': 'UALICE', 'ts': '9.1', 'text': 'top level'},
            },
            None,
        ),
        (
            {
                'type': 'message',
                'subtype': 'message_changed',
                'channel': 'C1',
                'message': {'user': 'UB', 'bot_id': 'B1', 'ts': '9.1', 'thread_ts': '5.1', 'text': 'x'},
            },
            None,
        ),
        ({'type': 'channel_joined'}, None),
    ],
)
def test_an_event_becomes_what_it_asks_of_piloop(event: dict[str, object], found: object) -> None:
    assert SlackApp(channel='C1', people={'alice': 'UALICE'}).inbound(event) == found


def test_a_person_is_whom_people_names_and_the_connection_is_the_app_token_never_shown(
    db: Database, secrets: HomeSecrets
) -> None:
    """Instances of one app token share a socket: Slack splits an app's events across its open sockets."""
    app = SlackApp(channel='C1', people={'alice': 'UALICE'})
    assert (app.person('UALICE'), app.person('UBOB')) == (Person(name='alice'), None)
    key = app.connection(secrets)
    assert key is not None and 'xapp-app' not in key
    assert SlackApp(channel='C2', token={'secret': 'OTHER_BOT'}).connection(secrets) == key, 'the same app'
    assert SlackApp.model_validate({'channel': 'C1', 'app_token': {'secret': 'NONE'}}).connection(secrets) is None


class Heard:
    """A ``ListenHost`` that answers with ``receipt`` and keeps what it heard and what failed."""

    def __init__(self, secrets: HomeSecrets, receipt: Receipt) -> None:
        self.inbound: list[Inbound] = []
        self.failures: list[str] = []
        self.receipt = receipt
        self.host = ListenHost(secrets, self._received, self.failures.append)

    def _received(self, inbound: Inbound) -> Receipt:
        self.inbound.append(inbound)
        return self.receipt


ACK: dict[str, object] = {
    'type': 'reaction_added',
    'user': 'UALICE',
    'reaction': 'white_check_mark',
    'item': {'type': 'message', 'channel': 'C1', 'ts': '5.1'},
    'event_ts': '9.9',
}
EDIT: dict[str, object] = {
    'type': 'message',
    'subtype': 'message_changed',
    'channel': 'C1',
    'message': {'user': 'UALICE', 'ts': '9.1', 'thread_ts': '5.1', 'text': 'raise it to 4 h'},
}
REPLY: dict[str, object] = {
    'type': 'message',
    'user': 'UALICE',
    'channel': 'C1',
    'ts': '9.1',
    'thread_ts': '5.1',
    'text': 'raise it',
}


@pytest.mark.parametrize(
    ('receipt', 'event', 'told'),
    [
        (Accepted('piloop recorded this as comment#3'), REPLY, 'piloop recorded this as comment#3'),
        (Again(), REPLY, None),
        (Foreign(), REPLY, None),
        (Declined('you are not in people'), REPLY, 'you are not in people'),
        (Again(), {**{k: v for k, v in REPLY.items() if k != 'thread_ts'}, 'text': 'hello'}, HINT),
        (Accepted('piloop acknowledged comment#1'), ACK, 'piloop acknowledged comment#1'),
        (Accepted('piloop updated comment#3'), EDIT, 'piloop updated comment#3'),
    ],
)
def test_the_listener_acks_each_envelope_and_tells_the_person_what_piloop_declined(
    slack: FakeSlackApp, secrets: HomeSecrets, receipt: Receipt, event: dict[str, object], told: str | None
) -> None:
    heard = Heard(secrets, receipt)
    listener = app_of(slack).listen(heard.host)
    listener.start()
    try:
        assert listener.in_flight() == {}
        wait_until(lambda: slack.connections == 1, 'the socket')
        slack.push({'envelope_id': 'env-0', 'type': 'slash_commands', 'payload': {}})
        slack.envelope({'type': 'channel_joined'}, 'env-2')
        slack.envelope(event)
        acks = [{'envelope_id': 'env-0'}, {'envelope_id': 'env-1'}, {'envelope_id': 'env-2'}]
        wait_until(lambda: all(a in slack.frames for a in acks), 'the acks: every envelope is acked')
        if told is None:
            wait_until(lambda: len(heard.inbound) == 1, 'the answer')
            assert [m for m, _, _ in slack.calls] == ['apps.connections.open']
        else:
            wait_until(lambda: any(m == 'chat.postEphemeral' for m, _, _ in slack.calls), 'the ephemeral answer')
            [(_, body, auth)] = [c for c in slack.calls if c[0] == 'chat.postEphemeral']
            assert (body['channel'], body['user'], body['text'], body.get('thread_ts'), auth) == (
                'C1',
                'UALICE',
                told,
                '5.1' if {'thread_ts', 'item', 'message'} & event.keys() else None,
                'Bearer xoxb-bot',
            )
        assert slack.calls[0][2] == 'Bearer xapp-app', 'the app token opens the socket'
    finally:
        listener.stop(Leaving.STOP, 1)


def test_an_answer_that_cannot_be_posted_is_a_failure_of_the_listener(
    slack: FakeSlackApp, secrets: HomeSecrets
) -> None:
    heard = Heard(secrets, Declined('no'))
    slack.answers['chat.postEphemeral'] = [(200, {'ok': False, 'error': 'user_not_in_channel'})]
    listener = app_of(slack).listen(heard.host)
    listener.start()
    try:
        wait_until(lambda: slack.connections == 1, 'the socket')
        slack.envelope(REPLY)
        wait_until(lambda: heard.failures != [], 'the failure')
        assert heard.failures == ['could not answer in Slack: chat.postEphemeral: user_not_in_channel']
    finally:
        listener.stop(Leaving.STOP, 1)


def test_a_socket_that_does_not_open_is_a_failure_and_the_daemon_runs_on(
    slack: FakeSlackApp, secrets: HomeSecrets
) -> None:
    heard = Heard(secrets, Again())
    slack.answers['apps.connections.open'] = [(200, {'ok': False, 'error': 'invalid_auth'})]
    listener = app_of(slack).listen(heard.host)
    listener.start()
    listener.stop(Leaving.STOP, 1)
    assert heard.failures == ['Socket Mode did not open: invalid_auth']
    unreachable = Heard(secrets, Again())
    slack.answers['apps.connections.open'] = [(200, {'ok': True, 'url': 'ws://127.0.0.1:1/link'})]
    listener = app_of(slack).listen(unreachable.host)
    listener.start()
    try:
        assert unreachable.failures[0].startswith('Socket Mode: ConnectionRefusedError')
        wait_until(lambda: slack.connections == 1, "the SDK's reconnect, which asks for a new socket URL")
    finally:
        listener.stop(Leaving.STOP, 1)


def test_a_listener_needs_the_slack_extra_and_both_tokens(
    db: Database, secrets: HomeSecrets, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = SlackApp(channel='C1')
    SlackApp(channel='C1').listen(Heard(secrets, Again()).host).stop(Leaving.STOP, 1)
    monkeypatch.setitem(sys.modules, 'slack_sdk.socket_mode.builtin', None)
    with pytest.raises(ImportError, match=r"needs the slack extra: uv tool install --reinstall 'piloop\[slack\]'"):
        app.listen(Heard(secrets, Again()).host).start()
    monkeypatch.undo()
    none = HomeSecrets(db, LocalSecrets())
    with db.session() as s:
        LocalSecrets().delete(s, None, 'SLACK_APP_TOKEN')
        s.commit()
    missing = Heard(none, Again())
    app.listen(missing.host).start()
    assert missing.failures == [
        'Socket Mode did not open: the home has no secret SLACK_APP_TOKEN: set it with'
        ' `piloop secret set SLACK_APP_TOKEN`'
    ]


def test_the_doctor_checks_the_slack_extra_that_the_listener_imports() -> None:
    assert SlackApp(channel='C1').checks() == [Check('slack app', 'slack_sdk is installed')]


def test_a_stop_waits_for_the_socket_to_close_only_up_to_its_timeout(
    slack: FakeSlackApp, secrets: HomeSecrets, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Slack's SDK joins its session thread with no timeout on close, and a reconnect in flight can keep it alive."""
    from slack_sdk.socket_mode.builtin import SocketModeClient

    released = threading.Event()
    close = SocketModeClient.close

    def stuck(client: SocketModeClient) -> None:
        released.wait()
        close(client)

    monkeypatch.setattr(SocketModeClient, 'close', stuck)
    heard = Heard(secrets, Again())
    listener = app_of(slack).listen(heard.host)
    listener.start()
    began = time.monotonic()
    try:
        listener.stop(Leaving.STOP, 0.2)
        assert time.monotonic() - began < 10, 'the stop returned at its timeout'
        assert heard.failures == []
    finally:
        released.set()
