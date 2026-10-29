"""``slack_app``: a Slack app that posts each item on its own, and takes the answers in its threads over Socket Mode.

It posts with the bot token (``chat.postMessage``, which gives the post's ``ts``), and listens over Socket Mode
(https://docs.slack.dev/apis/events-api/using-socket-mode): an outbound websocket, so it needs no public URL and works
behind NAT. An escalation with choices is a Block Kit post, with a button for each choice (``Choose``). A reply in a
post's thread is a ``Reply``, ``idea: <hunch>`` there files a seed, a ✅ on the post is an ``Ack``, and an edit of a
reply changes it (``Edit``). Only ``people`` can answer. It needs the ``slack`` extra
(``uv tool install 'piloop[slack]'``), imported when the listener starts.
"""

import hashlib
from collections.abc import Mapping, Sequence
from concurrent.futures import Future
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, assert_never

import httpx2 as httpx
from pydantic import Field

from piloop.plugins.contribution import Leaving
from piloop.plugins.escalation.contract import EscalationView
from piloop.plugins.harness.contract import SecretRef
from piloop.plugins.host import Check, extra
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
    Receipt,
    Reply,
)
from piloop.plugins.notifier.kit.slack import SlackText, escaped, refused
from piloop.plugins.secrets.contract import SecretReader

if TYPE_CHECKING:  # the extra is optional: only a listener that starts imports it
    from slack_sdk.socket_mode.builtin import SocketModeClient
    from slack_sdk.socket_mode.client import BaseSocketModeClient
    from slack_sdk.socket_mode.request import SocketModeRequest

ACKS = frozenset({'white_check_mark', 'heavy_check_mark', 'ballot_box_with_check'})
"""The reactions on a post that acknowledge its item."""
SEED = 'idea:'
"""What a reply starts with to file a seed, case aside."""
HINT = 'piloop reads answers in the thread of its posts: reply there, or react with :white_check_mark: to ack.'
CHOOSE = 'piloop.choose.'
"""What the ``action_id`` of a choice's button starts with: the choice's id follows."""
HEADER_CHARS, BUTTON_CHARS, SECTION_CHARS = 150, 75, 3000
"""What Slack takes at most in a header, in a button's text, and in a section's text."""

type Block = dict[str, object]
"""A Block Kit block, as JSON."""


class SlackApp(SlackText):
    """``slack_app``: a post for each item in ``channel``, and the answers in its threads, over Socket Mode."""

    channel: str
    """The channel to post in, by its id (``C0123ABC``): the app's bot must be in it, or it hears no replies."""
    token: SecretRef = SecretRef(secret='SLACK_BOT_TOKEN')
    """The secret that holds the bot token (``xoxb-…``; ``chat:write``, ``channels:history``, ``groups:history``,
    ``reactions:read``): ``SLACK_BOT_TOKEN`` unless set."""
    app_token: SecretRef = SecretRef(secret='SLACK_APP_TOKEN')
    """The secret that holds the app-level token (``xapp-…``; ``connections:write``) that opens the socket:
    ``SLACK_APP_TOKEN`` unless set."""
    reply_priority: int = Field(default=2, ge=0, le=4)
    """The priority in the inbox of a reply from Slack, as ``piloop inbox reply`` gives one by default."""
    api: str = 'https://slack.com/api/'
    """Slack's Web API: another URL only for a proxy, or a test."""

    def send(self, notices: Sequence[Notice], secrets: SecretReader) -> Sequence[MessageRef | None]:
        token = self._secret(secrets, self.token)
        posted: list[MessageRef | None] = []
        for notice in notices:
            body: dict[str, object] = {'channel': self.channel, 'text': self.item(notice)}
            if notice.thread is not None:
                body['thread_ts'] = ts_of(notice.thread)
            elif notice.view is not None and notice.view.choices:
                body['blocks'] = self.blocks(notice, notice.view)
            try:
                answer = self._call('chat.postMessage', token, body)
            except NotDelivered as e:
                raise NotDelivered(str(e), e.retry_after, posted) from e
            match answer:
                case {'channel': str(channel), 'ts': str(ts)}:
                    posted.append(MessageRef(f'{channel}/{ts}'))
                case _:
                    raise NotDelivered('chat.postMessage: the answer names no channel and ts', None, posted)
        return posted

    def blocks(self, notice: Notice, view: EscalationView) -> list[Block]:
        """An escalation as Block Kit: the ask, why and choices, a button for each choice, then where it is from.

        The details come last, and Slack folds them behind "Show more" when they are long. The ids are only in the
        context line and the details. ``text`` stays the fallback that a notification shows.
        """
        values = self.values(notice)
        mark = {view.recommended: ' _(recommended)_'}
        choices = '\n'.join(
            f'*{c.id}.* {escaped(c.label)}{mark.get(c.id, "")} → {escaped(c.result)}' for c in view.choices
        )
        buttons: list[Block] = [
            {
                'type': 'button',
                'text': _plain(f'{c.id}: {c.label}', BUTTON_CHARS),
                'action_id': f'{CHOOSE}{c.id}',
                'value': c.id,
                **({'style': 'primary'} if c.id == view.recommended else {}),
            }
            for c in view.choices
        ]
        about = (
            f'{values["mark"]} *{values["campaign"]}* · P{values["priority"]} · {values["kind"]} comment#{notice.comment}'
            f' · from {values["author"]}{values["mention"]} · {values["link"]}'
        )
        blocks: list[Block] = [{'type': 'header', 'text': _plain(view.headline, HEADER_CHARS)}]
        if view.why:
            blocks.append(_section(escaped(view.why)))
        blocks += [
            _section(choices),
            {'type': 'actions', 'elements': buttons},
            {'type': 'context', 'elements': [{'type': 'mrkdwn', 'text': _clip(about, SECTION_CHARS)}]},
        ]
        if view.details:
            blocks.append(_section(f'*Details*\n{escaped(view.details)}'))
        return blocks

    def settle(self, token: str, click: Click, receipt: Accepted | Answered) -> None:
        """Mark the post that ``click`` was on: its buttons give way to who answered it, and how (``chat.update``).

        Raises:
            NotDelivered: Slack refused the update.
        """
        match receipt:
            case Accepted():
                line = f':white_check_mark: <@{click.member}> chose *{click.choice}*'
            case Answered(by=by, answer=answer):
                line = f':white_check_mark: Answered by {escaped(by)}' + (
                    f': {escaped(answer)}' if answer is not None else ''
                )
            case _:
                assert_never(receipt)
        settled = {'type': 'context', 'elements': [{'type': 'mrkdwn', 'text': _clip(line, SECTION_CHARS)}]}
        blocks = [settled if b.get('type') == 'actions' else b for b in click.blocks]
        body = {'channel': click.channel, 'ts': click.ts, 'text': click.text, 'blocks': blocks}
        self._call('chat.update', token, body)

    def clicked(self, payload: Mapping[str, object]) -> Click | None:
        """The choice that a ``block_actions`` payload makes on one of the app's posts, or None for another action."""
        match payload:
            case {
                'type': 'block_actions',
                'user': {'id': str(member)},
                'container': {'channel_id': str(channel), 'message_ts': str(ts)},
                'message': {'text': str(text), 'blocks': list(blocks)},
                'actions': [{'action_id': str(action), 'value': str(choice), 'action_ts': str(at)}],
            } if action.startswith(CHOOSE):
                return Click(channel, ts, member, choice, at, text, blocks)
            case _:
                return None

    def checks(self) -> list[Check]:
        """The ``slack`` extra, which the listener imports."""
        return [extra('slack app', 'slack_sdk', 'slack')]

    def listen(self, host: ListenHost) -> SocketListener:
        return SocketListener(self, host)

    def connection(self, secrets: SecretReader) -> str | None:
        """A digest of the app token: Slack splits an app's events across its open sockets, so one app gets one."""
        found = secrets.get(self.app_token.secret)
        return None if found is None else hashlib.sha256(found.get_secret_value().encode()).hexdigest()

    def inbound(self, event: Mapping[str, object]) -> Inbound | Literal['hint'] | None:
        """What a Slack event asks of piloop: an answer, ``hint`` for a message outside any thread, or None."""
        match event:
            case {'type': 'message', 'user': str(user), 'channel': str(channel), 'ts': str(ts), 'text': str(text)} if (
                'subtype' not in event and 'bot_id' not in event
            ):
                if not isinstance(thread := event.get('thread_ts'), str):
                    return 'hint'
                said = text.strip()
                act = (
                    FileSeed(said[len(SEED) :].strip())
                    if said.lower().startswith(SEED)
                    else Reply(said, self.reply_priority)
                )
                return Inbound(MessageRef(f'{channel}/{thread}'), MessageRef(f'{channel}/{ts}'), user, act)
            case {
                'type': 'message',
                'subtype': 'message_changed',
                'channel': str(channel),
                'message': {'user': str(user), 'ts': str(ts), 'thread_ts': str(thread), 'text': str(text)} as changed,
            } if 'bot_id' not in changed:
                return Inbound(
                    MessageRef(f'{channel}/{thread}'),
                    MessageRef(f'{channel}/{ts}'),
                    user,
                    Edit(text.strip()),
                )
            case {
                'type': 'reaction_added',
                'user': str(user),
                'reaction': str(reaction),
                'item': {'type': 'message', 'channel': str(channel), 'ts': str(ts)},
                'event_ts': str(at),
            } if reaction in ACKS:
                post = MessageRef(f'{channel}/{ts}')
                return Inbound(post, MessageRef(f'{channel}/{ts}/{reaction}/{user}/{at}'), user, Ack())
            case _:
                return None

    def answer(self, token: str, event: Mapping[str, object], text: str) -> None:
        """Tell the person who wrote ``event``, and only them, ``text``, in its thread (``chat.postEphemeral``).

        A message is in its channel and thread, an edit names the message, and a reaction is on its post, in whose thread
        it is answered.
        """
        match event:
            case {'item': {'channel': channel, 'ts': thread}, 'user': user}:
                pass
            case {'channel': channel, 'message': {'user': user, 'thread_ts': thread}}:  # an edit
                pass
            case _:
                channel, user, thread = event['channel'], event['user'], event.get('thread_ts')
        self.tell(token, channel, user, thread if isinstance(thread, str) else None, text)

    def tell(self, token: str, channel: object, user: object, thread: str | None, text: str) -> None:
        """Tell ``user``, and only them, ``text`` in ``channel``, in the thread of ``thread`` if set."""
        body: dict[str, object] = {'channel': channel, 'user': user, 'text': text}
        if thread is not None:
            body['thread_ts'] = thread
        self._call('chat.postEphemeral', token, body)

    def _call(self, method: str, token: str, body: Mapping[str, object]) -> dict[str, object]:
        """Call ``method`` of the Web API with the bot token, and return its answer.

        Raises:
            NotDelivered: The call failed, or Slack said ``ok: false``: the reason, never the token.
        """
        try:
            answer = httpx.post(
                f'{self.api}{method}',
                json=body,
                headers={'Authorization': f'Bearer {token}'},
                timeout=self.timeout.total_seconds(),
            )
        except httpx.HTTPError as e:
            raise NotDelivered(f'{method}: {type(e).__name__}: {str(e).replace(token, self.token.secret)}') from e
        if not answer.is_success:
            raise refused(answer)
        found: dict[str, object] = answer.json()
        if found.get('ok') is not True:
            raise NotDelivered(f'{method}: {found.get("error", "not ok")}')
        return found

    @staticmethod
    def _secret(secrets: SecretReader, ref: SecretRef) -> str:
        if (value := secrets.get(ref.secret)) is None:
            raise NotDelivered(secrets.missing(ref.secret))
        return value.get_secret_value()


@dataclass(frozen=True)
class Click:
    """A person's click on a choice's button, and the post it is on, as Slack sends it."""

    channel: str
    ts: str
    """The post's ``ts``."""
    member: str
    choice: str
    at: str
    """The click's ``action_ts``: two clicks are two answers, and a redelivery is one."""
    text: str
    """The post's fallback text, which an update keeps."""
    blocks: list[Block]
    """The post's blocks, which an update changes."""

    def inbound(self) -> Inbound:
        post = f'{self.channel}/{self.ts}'
        return Inbound(
            MessageRef(post), MessageRef(f'{post}/choose/{self.member}/{self.at}'), self.member, Choose(self.choice)
        )


def _plain(text: str, chars: int) -> dict[str, object]:
    return {'type': 'plain_text', 'text': _clip(' '.join(text.split()), chars)}


def _section(text: str) -> Block:
    return {'type': 'section', 'text': {'type': 'mrkdwn', 'text': _clip(text, SECTION_CHARS)}}


def _clip(text: str, chars: int) -> str:
    return text if len(text) <= chars else text[: chars - 1] + '…'


def _said(receipt: Receipt) -> str | None:
    """What piloop tells the person of ``receipt``: nothing for a redelivery or a post that is not piloop's."""
    match receipt:
        case Accepted(said=said) | Declined(reason=said) | Answered(said=said):
            return said
        case Again() | Foreign():
            return None
        case _:
            assert_never(receipt)


def ts_of(post: MessageRef) -> str:
    """The ``ts`` of a post of ``slack_app``: what a reply in its thread names as ``thread_ts``."""
    return post.id.rpartition('/')[2]


class SocketListener:
    """The ``Lifecycle`` of a ``slack_app``'s listener: one Socket Mode client, which Slack's SDK runs in its threads."""

    def __init__(self, app: SlackApp, host: ListenHost) -> None:
        self._app, self._host = app, host
        self._client: SocketModeClient | None = None

    def start(self) -> None:
        """Connect. Slack's SDK reconnects on its own until ``stop``.

        Raises:
            ImportError: The ``slack`` extra is not installed.
        """
        try:  # here, not at the top: the extra is optional, and only a listener needs it
            from slack_sdk.errors import SlackApiError
            from slack_sdk.socket_mode.builtin import SocketModeClient
            from slack_sdk.web import WebClient
        except ImportError as e:
            raise ImportError(
                "[plugins.notifier] provider = 'slack_app' needs the slack extra:"
                " uv tool install --reinstall 'piloop[slack]'"
            ) from e
        try:
            token = SlackApp._secret(self._host.secrets, self._app.token)
            app_token = SlackApp._secret(self._host.secrets, self._app.app_token)
        except NotDelivered as e:  # a secret removed since the daemon chose this listener: setting it again reopens it
            self._host.failed(f'Socket Mode did not open: {e}')
            return
        client = SocketModeClient(
            app_token=app_token,
            web_client=WebClient(base_url=self._app.api),
            on_error_listeners=[lambda e: self._host.failed(f'Socket Mode: {type(e).__name__}: {e}')],
        )
        client.socket_mode_request_listeners.append(lambda c, request: self._on(c, request, token))
        self._client = client
        try:
            client.connect()
        except SlackApiError as e:  # a wrong app token, or no Socket Mode: the daemon runs on, without answers
            self._host.failed(f'Socket Mode did not open: {e.response.get("error")}')

    def in_flight(self) -> Mapping[str, Future[None]]:
        return {}

    def stop(self, leaving: Leaving, timeout: float) -> None:
        if self._client is not None:
            self._client.close()

    def _on(self, client: BaseSocketModeClient, request: SocketModeRequest, token: str) -> None:
        """Ack the envelope at once, as Slack asks within 3 seconds, then act on its event or its click."""
        from slack_sdk.socket_mode.response import SocketModeResponse

        client.send_socket_mode_response(SocketModeResponse(envelope_id=request.envelope_id))
        payload = request.payload if isinstance(request.payload, dict) else {}
        try:
            match request.type, payload:
                case 'events_api', {'event': dict(event)}:
                    self._event(token, event)
                case 'interactive', _ if (click := self._app.clicked(payload)) is not None:
                    self._click(token, click)
                case _:
                    return
        except NotDelivered as e:
            self._host.failed(f'could not answer in Slack: {e}')

    def _event(self, token: str, event: Mapping[str, object]) -> None:
        match self._app.inbound(event):
            case None:
                return
            case 'hint':
                said: str | None = HINT
            case Inbound() as inbound:
                said = _said(self._host.received(inbound))
            case found:
                assert_never(found)
        if said is not None:
            self._app.answer(token, event, said)

    def _click(self, token: str, click: Click) -> None:
        receipt = self._host.received(click.inbound())
        if isinstance(receipt, Accepted | Answered):
            self._app.settle(token, click, receipt)
        if (said := _said(receipt)) is not None:
            self._app.tell(token, click.channel, click.member, None, said)
