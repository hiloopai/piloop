"""The ``slack`` notifier, against a local server that answers as Slack's incoming webhooks do."""

from datetime import timedelta

import pytest
from pydantic import SecretStr, ValidationError

from piloop.actor import Person, System
from piloop.db.session import Database
from piloop.ids import CommentId, CommentKind
from piloop.plugins.notifier.contract import NotDelivered, Notice, NoticeKind
from piloop.plugins.notifier.slack import PLACEHOLDERS, Slack
from piloop.plugins.secrets.contract import HomeSecrets, set_secret
from piloop.plugins.secrets.local import LocalSecrets
from tests.fixtures.slack import FakeSlack


def home_secrets(db: Database, **values: str) -> HomeSecrets:
    local = LocalSecrets()
    with db.session() as s:
        for name, value in values.items():
            set_secret(s, local, System.HUMAN, None, name, SecretStr(value))
        s.commit()
    return HomeSecrets(db, local)


BODY = "Run 41's evaluation takes 3 h, over the 1 h timeout. Raise the timeout, or drop the idea?"


def notice(
    comment: int = 207,
    *,
    kind: NoticeKind = NoticeKind.NEW,
    comment_kind: CommentKind = CommentKind.ESCALATION,
    priority: int = 1,
    campaign: str | None = 'enwik8',
    body: str = BODY,
    owner: Person | None = None,
    link: str | None = None,
) -> Notice:
    return Notice(kind, CommentId(comment), comment_kind, priority, campaign, 'implementer#12', body, owner, link)


def test_it_posts_one_message_with_a_block_for_each_item(db: Database, slack: FakeSlack) -> None:
    secrets = home_secrets(db, SLACK_WEBHOOK_URL=slack.url)
    Slack().send([notice(), notice(208, priority=0, comment_kind=CommentKind.DECISION, body='a\nb')], secrets)
    assert slack.posts == [
        {
            'text': ':rotating_light: *enwik8* · escalation comment#207 · P1 · from implementer#12\n'
            "Run 41's evaluation takes 3 h, over the 1 h timeout. Raise the timeout, or drop the idea?\n"
            '`piloop inbox show 207`\n\n'
            ':rotating_light: *enwik8* · decision comment#208 · P0 · from implementer#12\n'
            'a\nb\n'
            '`piloop inbox show 208`'
        }
    ]


def test_a_reminder_links_the_item_mentions_its_owner_and_escapes_its_text() -> None:
    slack = Slack(mentions={'alice@example.com': 'U0123ABC'})
    text = slack.text(
        [
            notice(
                kind=NoticeKind.REMINDER,
                campaign=None,
                body='a < b & c > d',
                owner=Person(name='alice@example.com'),
                link='https://piloop.example.com/inbox/207',
            )
        ]
    )
    assert text == (
        ':alarm_clock: reminder · *piloop* · escalation comment#207 · P1 · from implementer#12 · <@U0123ABC>\n'
        'a &lt; b &amp; c &gt; d\n'
        '<https://piloop.example.com/inbox/207|open>'
    )
    unmapped = slack.text([notice(owner=Person(name='bob@example.com'))])
    assert '<@' not in unmapped, 'an owner without a member id is not mentioned'


def test_a_message_shows_max_items_and_counts_the_rest() -> None:
    text = Slack(max_items=1, body_chars=10).text([notice(body='one two three four'), notice(208), notice(209)])
    assert text.splitlines() == [
        ':rotating_light: *enwik8* · escalation comment#207 · P1 · from implementer#12',
        'one two t…',
        '`piloop inbox show 207`',
        '',
        '… and 2 more: `piloop inbox`',
    ]


def test_a_refusal_is_not_delivered_with_its_status_and_the_wait_that_slack_asks(
    db: Database, slack: FakeSlack
) -> None:
    secrets = home_secrets(db, SLACK_WEBHOOK_URL=slack.url)
    slack.answers += [(404, {}, 'no_service'), (429, {'Retry-After': '30'}, 'rate_limited')]
    with pytest.raises(NotDelivered, match='^HTTP 404: no_service$') as refused:
        Slack().send([notice()], secrets)
    assert refused.value.retry_after is None
    with pytest.raises(NotDelivered, match='^HTTP 429: rate_limited$') as limited:
        Slack().send([notice()], secrets)
    assert limited.value.retry_after == timedelta(seconds=30)


def test_a_failed_connection_is_not_delivered_and_the_error_never_holds_the_url(db: Database) -> None:
    url = 'http://127.0.0.1:1/services/T0/B0/secret'
    secrets = home_secrets(db, TEAM_WEBHOOK=url)
    slack = Slack.model_validate({'webhook': {'secret': 'TEAM_WEBHOOK'}, 'timeout': 1})
    with pytest.raises(NotDelivered, match='^ConnectError: ') as failed:
        slack.send([notice()], secrets)
    assert 'secret' not in str(failed.value)
    bad = home_secrets(db, BAD_WEBHOOK='http://[::1/x')
    with pytest.raises(NotDelivered, match='^the secret BAD_WEBHOOK is not a URL to post to') as invalid:
        Slack.model_validate({'webhook': {'secret': 'BAD_WEBHOOK'}}).send([notice()], bad)
    assert '[::1' not in str(invalid.value), 'the secret is named, never shown'


def test_a_missing_secret_is_not_delivered_and_says_how_to_set_it(db: Database) -> None:
    with pytest.raises(
        NotDelivered, match=r'^the home has no secret SLACK_WEBHOOK_URL: set it with `piloop secret set'
    ):
        Slack().send([notice()], home_secrets(db))


def test_a_link_holds_only_the_fields_that_piloop_fills_in() -> None:
    assert Slack(link='https://x.example/{campaign}/{comment}').link == 'https://x.example/{campaign}/{comment}'
    assert Slack(link=None).link is None
    for link in ('https://x.example/{run}', 'https://x.example/{0}', 'https://x.example/{'):
        with pytest.raises(ValidationError, match=r'is not a link that piloop fills in .* it may hold \{comment\}'):
            Slack(link=link)


def test_the_backoff_doubles_up_to_its_cap_and_a_receiver_s_wait_wins() -> None:
    slack = Slack(retry=timedelta(minutes=1), retry_max=timedelta(minutes=5))
    assert [slack.wait(n, None) for n in (1, 2, 3, 4)] == [timedelta(minutes=m) for m in (1, 2, 4, 5)]
    assert slack.wait(9, timedelta(seconds=30)) == timedelta(seconds=30)


def test_a_template_shows_each_item_from_its_placeholders() -> None:
    slack = Slack(template='{{{kind}}} {campaign}/{comment}: {body}', mentions={'alice': 'U1'})
    assert slack.text([notice(body='a < b')]) == '{escalation} enwik8/207: a &lt; b'
    every = Slack(template=' '.join(f'{{{name}}}' for name in PLACEHOLDERS), mentions={'alice': 'U1'})
    assert every.text([notice(owner=Person(name='alice'))]) == (
        f':rotating_light: enwik8 escalation 207 1 implementer#12  · <@U1> {BODY} `piloop inbox show 207`'
    )


@pytest.mark.parametrize(
    ('template', 'error'),
    [
        ('{run}', r'\{run\} is not a placeholder; a template may hold \{mark\}, \{campaign\}'),
        ('{body!r}', r'\{body!r\} is not a placeholder'),
        ('{priority:>3}', r'\{priority:>3\} is not a placeholder'),
        ('{}', r'\{\} is not a placeholder'),
        ('{body', r"'\{body' is not a template: "),
    ],
)
def test_a_template_refuses_what_is_not_a_placeholder(template: str, error: str) -> None:
    with pytest.raises(ValidationError, match=error):
        Slack(template=template)


def test_the_template_s_docstring_names_every_placeholder_that_it_fills_in() -> None:
    assert tuple(Slack().values(notice())) == PLACEHOLDERS
    described = Slack.model_fields['template'].description or ''
    assert [name for name in PLACEHOLDERS if f'``{{{name}}}``' not in described] == []
