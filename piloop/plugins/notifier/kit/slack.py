"""What every Slack notifier shares: how a notice reads in mrkdwn, the people it knows, and what an HTTP failure says."""

from datetime import timedelta
from string import Formatter
from typing import assert_never

import httpx2 as httpx
from pydantic import Field, field_validator

from piloop.actor import Person
from piloop.plugins.escalation.contract import EscalationView
from piloop.plugins.notifier.contract import NotDelivered, Notice, NoticeKind, Notifier
from piloop.text import block

ESCAPES = str.maketrans({'&': '&amp;', '<': '&lt;', '>': '&gt;'})
"""What Slack's mrkdwn reads as markup in text: https://docs.slack.dev/messaging/formatting-message-text#escaping."""
PLACEHOLDERS = ('mark', 'campaign', 'kind', 'comment', 'priority', 'author', 'mention', 'body', 'link')
"""What a ``template`` may hold, each as ``{name}``: ``SlackText.template`` says what each one is."""
TEMPLATE = '{mark} *{campaign}* · {kind} comment#{comment} · P{priority} · from {author}{mention}\n{body}\n{link}'


class SlackText(Notifier):
    """The keys of every Slack notifier: who its people are, and how a message shows an item."""

    people: dict[str, str] = {}
    """A person's name, as the server names them, to their Slack member id. A message mentions a campaign's owner, and
    only these people can answer a notifier that listens, signed as themselves."""
    timeout: timedelta = Field(default=timedelta(seconds=10), gt=timedelta(0))
    """How long a post may take. The daemon's loop waits for it."""
    max_items: int = Field(default=20, gt=0)
    """How many items one message shows. It names how many more there are."""
    body_chars: int = Field(default=300, gt=1)
    """How much of an item's text a message shows, in whole lines."""
    template: str = TEMPLATE
    """How the message shows one item. Each ``{name}`` is filled in: ``{mark}`` is ``:rotating_light:``, or
    ``:alarm_clock: reminder ·`` for a reminder, or ``:speech_balloon:`` for an answer in a thread; ``{campaign}`` the
    campaign's name; ``{kind}`` the item's kind; ``{comment}`` its id; ``{priority}`` 0 to 4, or ``–``; ``{author}`` who wrote
    it; ``{mention}`` `` · <@member>`` for an owner in ``people``, else nothing; ``{body}`` its text, up to
    ``body_chars`` (an escalation's ask, why and choices, as its provider shows them); ``{link}`` its ``link``, else the command that shows it. Write ``{{`` and ``}}`` for a brace."""

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

    def item(self, notice: Notice) -> str:
        """How the message shows ``notice``: ``template``, filled in."""
        return self.template.format_map(self.values(notice))

    def values(self, notice: Notice) -> dict[str, str]:
        """What each placeholder of ``template`` (``PLACEHOLDERS``) is for ``notice``, escaped for mrkdwn."""
        match notice.kind:
            case NoticeKind.NEW:
                mark = ':rotating_light:'
            case NoticeKind.REMINDER:
                mark = ':alarm_clock: reminder ·'
            case NoticeKind.FOLLOW_UP:
                mark = ':speech_balloon:'
            case _:
                assert_never(notice.kind)
        member = self.people.get(notice.owner.name) if notice.owner is not None else None
        return {
            'mark': mark,
            'campaign': escaped(notice.campaign or 'piloop'),
            'kind': str(notice.comment_kind),
            'comment': str(notice.comment),
            'priority': str(notice.priority) if notice.priority is not None else '–',
            'author': escaped(notice.author),
            'mention': f' · <@{member}>' if member is not None else '',
            'body': escaped(block(plain(notice.view) if notice.view else notice.body, self.body_chars)),
            'link': f'<{notice.link}|open>' if notice.link else f'`piloop inbox show {notice.comment}`',
        }

    def person(self, member: str) -> Person | None:
        """The person whose Slack member id is ``member``, in ``people``, or None."""
        return next((Person(name=name) for name, found in self.people.items() if found == member), None)


def plain(view: EscalationView) -> str:
    """An escalation in plain text, the evidence left out: the ask, why, and each choice."""
    lines = [view.headline, *(['', view.why] if view.why else [])]
    if view.choices:
        mark = {view.recommended: ' (recommended)'}
        lines += ['', *(f'{c.id}. {c.label}{mark.get(c.id, "")} → {c.result}' for c in view.choices)]
    return '\n'.join(lines)


def escaped(text: str) -> str:
    """``text`` with what mrkdwn reads as markup escaped."""
    return text.translate(ESCAPES)


def refused(answer: httpx.Response) -> NotDelivered:
    """What a failed HTTP answer says, with the wait that a 429 asks for."""
    asked = answer.headers.get('retry-after')
    wait = timedelta(seconds=int(asked)) if asked is not None and asked.isdigit() else None
    return NotDelivered(f'HTTP {answer.status_code}: {answer.text[:200]}', wait)
