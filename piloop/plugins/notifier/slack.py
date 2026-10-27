"""``slack``: a notifier that posts to a Slack channel through an incoming webhook."""

from collections.abc import Sequence
from datetime import timedelta
from string import Formatter
from typing import assert_never

import httpx2 as httpx
from pydantic import Field, field_validator

from piloop.plugins.harness.contract import SecretRef
from piloop.plugins.notifier.contract import NotDelivered, Notice, NoticeKind, Notifier
from piloop.plugins.secrets.contract import HomeSecrets
from piloop.text import block

ESCAPES = str.maketrans({'&': '&amp;', '<': '&lt;', '>': '&gt;'})
"""What Slack's mrkdwn reads as markup in text: https://docs.slack.dev/messaging/formatting-message-text#escaping."""
PLACEHOLDERS = ('mark', 'campaign', 'kind', 'comment', 'priority', 'author', 'mention', 'body', 'link')
"""What a ``template`` may hold, each as ``{name}``: ``Slack.template`` says what each one is."""
TEMPLATE = '{mark} *{campaign}* · {kind} comment#{comment} · P{priority} · from {author}{mention}\n{body}\n{link}'


class Slack(Notifier):
    """``slack``: one message in the channel of an incoming webhook (https://docs.slack.dev/messaging/sending-messages-using-incoming-webhooks)."""

    webhook: SecretRef = SecretRef(secret='SLACK_WEBHOOK_URL')
    """The secret that holds the webhook's URL: ``SLACK_WEBHOOK_URL`` unless set (``piloop secret set``)."""
    mentions: dict[str, str] = {}
    """A person's name, as the server names them, to their Slack member id: a message mentions a campaign's owner."""
    timeout: timedelta = Field(default=timedelta(seconds=10), gt=timedelta(0))
    """How long a post may take. The daemon's loop waits for it."""
    max_items: int = Field(default=20, gt=0)
    """How many items one message shows. It names how many more there are."""
    body_chars: int = Field(default=300, gt=1)
    """How much of an item's text a message shows, in whole lines."""
    template: str = TEMPLATE
    """How the message shows one item. Each ``{name}`` is filled in: ``{mark}`` is ``:rotating_light:``, or
    ``:alarm_clock: reminder ·`` for a reminder; ``{campaign}`` the campaign's name; ``{kind}`` the item's kind;
    ``{comment}`` its id; ``{priority}`` 0 to 4; ``{author}`` who wrote it; ``{mention}`` `` · <@member>`` for an owner
    in ``mentions``, else nothing; ``{body}`` its text, up to ``body_chars``; ``{link}`` its ``link``, else the command
    that shows it. Write ``{{`` and ``}}`` for a brace."""

    @field_validator('template')
    @classmethod
    def _placeholders_known(cls, template: str) -> str:
        try:
            parsed = list(Formatter().parse(template))
        except ValueError as e:
            raise ValueError(f'{template!r} is not a template: {e}') from e
        for _, name, spec, conversion in parsed:
            if name is not None and (name not in PLACEHOLDERS or spec or conversion):
                shown = '{' + name + (f'!{conversion}' if conversion else '') + (f':{spec}' if spec else '') + '}'
                raise ValueError(
                    f'{shown} is not a placeholder; a template may hold {", ".join(f"{{{n}}}" for n in PLACEHOLDERS)}'
                )
        return template

    def send(self, notices: Sequence[Notice], secrets: HomeSecrets) -> None:
        name = self.webhook.secret
        if (url := secrets.get(name)) is None:
            raise NotDelivered(f'the home has no secret {name}: set it with `piloop secret set {name}`')
        try:
            answer = httpx.post(
                url.get_secret_value(), json={'text': self.text(notices)}, timeout=self.timeout.total_seconds()
            )
        except httpx.InvalidURL as e:  # its message quotes a part of the URL, which is the secret
            raise NotDelivered(f'the secret {name} is not a URL to post to: set it to the webhook URL') from e
        except httpx.HTTPError as e:  # its message can hold the URL
            raise NotDelivered(f'{type(e).__name__}: {str(e).replace(url.get_secret_value(), name)}') from e
        if answer.is_success:
            return
        asked = answer.headers.get('retry-after')
        wait = timedelta(seconds=int(asked)) if asked is not None and asked.isdigit() else None
        raise NotDelivered(f'HTTP {answer.status_code}: {answer.text[:200]}', wait)

    def text(self, notices: Sequence[Notice]) -> str:
        """The message: one block for each of the first ``max_items`` notices, then how many more there are."""
        shown = [self._item(n) for n in notices[: self.max_items]]
        if more := len(notices) - len(shown):
            shown.append(f'… and {more} more: `piloop inbox`')
        return '\n\n'.join(shown)

    def _item(self, notice: Notice) -> str:
        return self.template.format_map(self.values(notice))

    def values(self, notice: Notice) -> dict[str, str]:
        """What each placeholder of ``template`` (``PLACEHOLDERS``) is for ``notice``, escaped for mrkdwn."""
        match notice.kind:
            case NoticeKind.NEW:
                mark = ':rotating_light:'
            case NoticeKind.REMINDER:
                mark = ':alarm_clock: reminder ·'
            case _:
                assert_never(notice.kind)
        member = self.mentions.get(notice.owner.name) if notice.owner is not None else None
        return {
            'mark': mark,
            'campaign': _escaped(notice.campaign or 'piloop'),
            'kind': str(notice.comment_kind),
            'comment': str(notice.comment),
            'priority': str(notice.priority),
            'author': _escaped(notice.author),
            'mention': f' · <@{member}>' if member is not None else '',
            'body': _escaped(block(notice.body, self.body_chars)),
            'link': f'<{notice.link}|open>' if notice.link else f'`piloop inbox show {notice.comment}`',
        }


def _escaped(text: str) -> str:
    return text.translate(ESCAPES)
