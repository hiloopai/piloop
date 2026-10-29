"""``slack_app``: a Slack app that posts each item on its own, and takes the answers in its threads over Socket Mode.

It posts with the bot token (``chat.postMessage``, which gives the post's ``ts``), and listens over Socket Mode
(https://docs.slack.dev/apis/events-api/using-socket-mode): an outbound websocket, so it needs no public URL and works
behind NAT. A reply in a post's thread is a ``Reply``, ``idea: <hunch>`` there files a seed, a ✅ on the post is an
``Ack``, and an edit of a reply changes it (``Edit``). Only ``people`` can answer. It needs the ``slack`` extra
(``uv tool install 'piloop[slack]'``), imported when the listener starts.
"""

from collections.abc import Mapping, Sequence
from concurrent.futures import Future
from typing import TYPE_CHECKING, Literal, assert_never

import httpx2 as httpx
from pydantic import Field

from piloop.plugins.contribution import Leaving
from piloop.plugins.harness.contract import SecretRef
from piloop.plugins.host import Check, extra
from piloop.plugins.notifier.contract import (
    Accepted,
    Ack,
    Again,
    Declined,
    Edit,
    FileSeed,
    Foreign,
    Inbound,
    ListenHost,
    MessageRef,
    NotDelivered,
    Notice,
    Reply,
)
from piloop.plugins.notifier.kit.slack import SlackText, refused
from piloop.plugins.secrets.contract import HomeSecrets

if TYPE_CHECKING:  # the extra is optional: only a listener that starts imports it
    from slack_sdk.socket_mode.builtin import SocketModeClient
    from slack_sdk.socket_mode.client import BaseSocketModeClient
    from slack_sdk.socket_mode.request import SocketModeRequest

ACKS = frozenset({'white_check_mark', 'heavy_check_mark', 'ballot_box_with_check'})
"""The reactions on a post that acknowledge its item."""
SEED = 'idea:'
"""What a reply starts with to file a seed, case aside."""
HINT = 'piloop reads answers in the thread of its posts: reply there, or react with :white_check_mark: to ack.'


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

    def send(self, notices: Sequence[Notice], secrets: HomeSecrets) -> Sequence[MessageRef | None]:
        token = self._secret(secrets, self.token)
        posted: list[MessageRef | None] = []
        for notice in notices:
            body: dict[str, object] = {'channel': self.channel, 'text': self.item(notice)}
            if notice.thread is not None:
                body['thread_ts'] = ts_of(notice.thread)
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

    def checks(self) -> list[Check]:
        """The ``slack`` extra, which the listener imports."""
        return [extra('slack app', 'slack_sdk', 'slack')]

    def listen(self, host: ListenHost) -> SocketListener:
        return SocketListener(self, host)

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
                return Inbound(MessageRef(f'{channel}/{thread}'), MessageRef(f'{channel}/{ts}'), self.person(user), act)
            case {
                'type': 'message',
                'subtype': 'message_changed',
                'channel': str(channel),
                'message': {'user': str(user), 'ts': str(ts), 'thread_ts': str(thread), 'text': str(text)} as changed,
            } if 'bot_id' not in changed:
                return Inbound(
                    MessageRef(f'{channel}/{thread}'),
                    MessageRef(f'{channel}/{ts}'),
                    self.person(user),
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
                return Inbound(post, MessageRef(f'{channel}/{ts}/{reaction}/{user}/{at}'), self.person(user), Ack())
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
        body: dict[str, object] = {'channel': channel, 'user': user, 'text': text}
        if isinstance(thread, str):
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
    def _secret(secrets: HomeSecrets, ref: SecretRef) -> str:
        if (value := secrets.get(ref.secret)) is None:
            raise NotDelivered(f'the home has no secret {ref.secret}: set it with `piloop secret set {ref.secret}`')
        return value.get_secret_value()


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
        except NotDelivered as e:  # `piloop secret set` needs the daemon: it starts, and listens after a restart
            self._host.failed(f'Socket Mode did not open: {e}, then `piloop restart`')
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
        """Ack the envelope at once, as Slack asks within 3 seconds, then act on its event."""
        from slack_sdk.socket_mode.response import SocketModeResponse

        client.send_socket_mode_response(SocketModeResponse(envelope_id=request.envelope_id))
        payload = request.payload if isinstance(request.payload, dict) else {}
        event = payload.get('event')
        if request.type != 'events_api' or not isinstance(event, dict):
            return
        match self._app.inbound(event):
            case None:
                return
            case 'hint':
                said: str | None = HINT
            case Inbound() as inbound:
                match self._host.received(inbound):
                    case Accepted(said=said) | Declined(reason=said):
                        pass
                    case Again() | Foreign():
                        said = None
                    case receipt:
                        assert_never(receipt)
            case found:
                assert_never(found)
        if said is not None:
            try:
                self._app.answer(token, event, said)
            except NotDelivered as e:
                self._host.failed(f'could not answer in Slack: {e}')
