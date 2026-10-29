"""The notifier machinery: what is due, what one pass sends, and how a failure is recorded and retried."""

from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import JsonValue, PrivateAttr, SecretStr, ValidationError
from pytest_mock import MockerFixture
from sqlalchemy import select

from piloop import config as config_module
from piloop.actor import Actor, Agent, Person, System
from piloop.api.views import CommentNews
from piloop.campaign import escalations
from piloop.campaign.campaigns import CampaignAdded
from piloop.campaign.inbox import ack, news
from piloop.campaign.ops import Refused as BacklogRefused
from piloop.config import CampaignConfig, load_campaign_config
from piloop.daemon.loop import Consumer, Phase, Turn
from piloop.db.events import emit, in_order
from piloop.db.feed import newest
from piloop.db.models import Campaign, Comment, Event, utcnow
from piloop.db.session import Database
from piloop.db.triggers.changelog import CampaignConfigChanged, CampaignStatusChanged, Commented, CommentEdited
from piloop.ids import HOME, CampaignId, CommentId, CommentKind, TargetKind, TargetRef
from piloop.otel import Tracer
from piloop.plugins.campaign_type.kit.backlog.ids import IDEA, IdeaStatus
from piloop.plugins.campaign_type.kit.backlog.tables import Idea
from piloop.plugins.campaign_type.kit.runs.controller import RunController
from piloop.plugins.campaign_type.kit.runs.ids import FINDING
from piloop.plugins.contribution import Contribution, DaemonHost, Leaving, Lifecycle
from piloop.plugins.escalation.contract import Escalated, Never, ToHuman
from piloop.plugins.escalation.structured import Structured
from piloop.plugins.executor.kit.tables import INSTANCE
from piloop.plugins.executor.workers import Workers
from piloop.plugins.host import PluginError, Plugins
from piloop.plugins.notifier import consumers
from piloop.plugins.notifier.consumers import (
    Listeners,
    Route,
    _answering,
    _failing,
    contribution,
    due,
    home_notifiers,
    received,
    routes,
    send_due,
)
from piloop.plugins.notifier.contract import (
    NOTIFIERS,
    Accepted,
    Ack,
    Act,
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
    NoticeKind,
    NotificationFailed,
    NotificationReceived,
    NotificationSent,
    Notifier,
    Reply,
)
from piloop.plugins.notifier.slack import Slack
from piloop.plugins.notifier.slack_app import SlackApp
from piloop.plugins.runtime.contract import make_runtime
from piloop.plugins.secrets.contract import (
    CampaignSecrets,
    HomeSecrets,
    SecretDeleted,
    SecretReader,
    SecretSet,
    set_secret,
)
from piloop.plugins.secrets.local import LocalSecrets
from piloop.settings import AgentSettings, DaemonSettings, Settings
from tests.conftest import reconfigured, store_config, wait_until
from tests.fixtures.slack import FakeSlack, fake_slack, fake_slack_app


class Outbox(Notifier):
    """A notifier that keeps what it sends, and raises each failure that ``failing`` holds first."""

    _sent: list[list[Notice]] = PrivateAttr(default_factory=list)
    _failing: list[NotDelivered] = PrivateAttr(default_factory=list)

    def send(self, notices: Sequence[Notice], secrets: SecretReader) -> Sequence[MessageRef | None]:
        if self._failing:
            raise self._failing.pop(0)
        self._sent.append(list(notices))
        return [None] * len(notices)


class Chat(Outbox):
    """A notifier that posts each notice with an id, ``C/<comment>.<send>``, and listens on the connection of the
    value of its secret ``key``. ``U<NAME>`` is the person ``<name>``."""

    key: str = 'CHAT_KEY'
    _hosts: list[ListenHost] = PrivateAttr(default_factory=list)
    _listeners: list[Recorder] = PrivateAttr(default_factory=list)

    def send(self, notices: Sequence[Notice], secrets: SecretReader) -> Sequence[MessageRef | None]:
        super().send(notices, secrets)
        return [MessageRef(f'C/{n.comment}.{len(self._sent)}') for n in notices]

    def listen(self, host: ListenHost) -> Lifecycle:
        self._hosts.append(host)
        self._listeners.append(Recorder())
        return self._listeners[-1]

    def connection(self, secrets: SecretReader) -> str | None:
        found = secrets.get(self.key)
        return None if found is None else f'chat:{found.get_secret_value()}'

    def person(self, member: str) -> Person | None:
        return Person(name=member[1:].lower()) if member in ('UALICE', 'UBOB') else None


class Recorder:
    """A lifecycle that records its calls."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def start(self) -> None:
        self.calls.append('start')

    def in_flight(self) -> Mapping[str, Future[None]]:
        return {}

    def stop(self, leaving: Leaving, timeout: float) -> None:
        self.calls.append(f'stop {leaving}')


def host_of(
    db: Database, tmp_path: Path, tracer: Tracer, plugins: Plugins, defaults: Mapping[str, object] | None = None
) -> DaemonHost:
    controller = RunController(db, tracer, tmp_path, executor=Workers())
    return DaemonHost(
        db,
        tracer,
        tmp_path,
        controller,
        make_runtime,
        utcnow,
        no_wake,
        DaemonSettings(),
        AgentSettings(),
        {},
        no_thread,
        plugins,
        defaults or {},
    )


def route_of(db: Database, campaign: Campaign | None, name: str, notifier: Notifier) -> Route:
    """The route of ``notifier`` as ``name`` for ``campaign``'s items, or the home's for those of none."""
    if campaign is None:
        return Route(None, name, notifier, HomeSecrets(db, LocalSecrets()))
    return Route(campaign.id, name, notifier, CampaignSecrets(db, LocalSecrets(), campaign.id, campaign.name))


def no_wake(_name: str) -> None:
    return None


def no_thread(_body: Callable[[], None]) -> Future[None]:
    raise AssertionError('the notifiers start no thread')


def write(
    db: Database,
    campaign: Campaign,
    kind: CommentKind = CommentKind.ESCALATION,
    priority: int | None = 1,
    author: str = 'orchestrator',
) -> CommentId:
    with db.session() as s:
        comment = Comment(
            target=TargetRef(TargetKind.CAMPAIGN, campaign.id), author=author, kind=kind, priority=priority, body='x'
        )
        s.add(comment)
        s.commit()
        return comment.id


def spanned(spans: InMemorySpanExporter, name: str) -> list[dict[str, object]]:
    """The attributes of each ``name`` that the tracer recorded outside a span: a span of no length."""
    return [dict(s.attributes or {}) for s in spans.get_finished_spans() if s.name == name]


def sent_of(db: Database) -> list[tuple[int, NotificationSent]]:
    with db.session() as s:
        return [(e.target_id, sent) for e, sent in in_order(s, NotificationSent)]


def times(db: Database, model: type[NotificationSent | NotificationFailed]) -> list[datetime]:
    """When each event of ``model`` was recorded, oldest first: what the backoff and the reminders count from."""
    with db.session() as s:
        return [e.at for e, _ in in_order(s, model)]


def failed_of(db: Database) -> list[NotificationFailed]:
    with db.session() as s:
        return [failed for _, failed in in_order(s, NotificationFailed)]


TEAM = {'provider': 'slack', 'webhook': {'secret': 'TEAM_WEBHOOK'}, 'template': 'team: {campaign} {comment}'}
"""A webhook notifier whose posts say that they are its."""


def configure(db: Database, campaign: Campaign, notifier: Mapping[str, object]) -> None:
    """Give ``campaign`` the ``[plugins.notifier]`` table ``notifier``, as ``piloop init`` stores it."""
    with db.session() as s:
        stored = s.get_one(Campaign, campaign.id)
        stored.config = reconfigured(stored.config, plugins={'notifier': notifier})
        s.commit()


def of_no_campaign(db: Database) -> CommentId:
    with db.session() as s:
        item = Comment(
            target=TargetRef(INSTANCE, 1), author='daemon', kind=CommentKind.ESCALATION, priority=0, body='x'
        )
        s.add(item)
        s.commit()
        return item.id


def consumer_named(made: Contribution, name: str) -> Consumer:
    return next(c for c in made.consumers if c.name == name)


def test_each_item_goes_once_to_each_notifier_of_its_campaign_and_one_of_no_campaign_to_config_toml_s(
    db: Database, campaign: Campaign, tmp_path: Path, tracer: Tracer, spans: InMemorySpanExporter, slack: FakeSlack
) -> None:
    """config.toml's notifiers are the defaults of every campaign's: ``piloop init`` stores them in the campaign's
    config, which a campaign's ``piloop.toml`` overrides. An item goes to the notifiers of one ``use``, so a name that
    both files have posts once."""
    with db.session() as s:
        set_secret(s, LocalSecrets(), System.HUMAN, None, 'TEAM_WEBHOOK', SecretStr(slack.url))
        s.commit()
    home = Settings(
        home=tmp_path, plugins={'notifier': {'use': ['team'], 'team': {**TEAM, 'link': 'https://p/{comment}'}}}
    )
    configure(db, campaign, {'use': ['team'], 'team': {**TEAM, 'link': 'https://p/{comment}'}})
    made = contribution(host_of(db, tmp_path, tracer, home.providers, home.campaign_defaults))
    consumer = consumer_named(made, 'notify')
    assert (consumer.phase, consumer.wakes_on) == (Phase.UPKEEP, frozenset({Commented}))
    item, homeless = write(db, campaign), of_no_campaign(db)
    now = utcnow()
    again = consumer.run(Turn(now))
    assert sorted(str(post['text']) for post in slack.posts) == [f'team: demo {item}', f'team: piloop {homeless}']
    assert sorted(sent_of(db)) == [
        (item, NotificationSent(notifier='team', notice=NoticeKind.NEW)),
        (homeless, NotificationSent(notifier='team', notice=NoticeKind.NEW)),
    ]
    assert spanned(spans, 'notification.sent') == [{'notifier': 'team', 'comments': 1}] * 2
    assert again == min(times(db, NotificationSent)) + timedelta(hours=24), 'the reminder'
    assert consumer.run(Turn(utcnow())) == again
    assert len(slack.posts) == 2, 'a pass after a restart sends nothing twice: the sent event says it was sent'


def test_a_campaign_s_use_replaces_config_toml_s_and_its_secret_wins_over_the_home_s(
    db: Database, campaign: Campaign, tmp_path: Path, tracer: Tracer
) -> None:
    with fake_slack() as home_hook, fake_slack() as campaign_hook:
        with db.session() as s:
            set_secret(s, LocalSecrets(), System.HUMAN, None, 'TEAM_WEBHOOK', SecretStr(home_hook.url))
            set_secret(s, LocalSecrets(), System.HUMAN, campaign.id, 'TEAM_WEBHOOK', SecretStr(campaign_hook.url))
            s.commit()
        home = Settings(home=tmp_path, plugins={'notifier': {'use': ['team'], 'team': TEAM}})
        configure(db, campaign, {'use': ['mine'], 'mine': {**TEAM, 'template': 'mine: {comment}'}})
        made = contribution(host_of(db, tmp_path, tracer, home.providers, home.campaign_defaults))
        item, homeless = write(db, campaign), of_no_campaign(db)
        consumer_named(made, 'notify').run(Turn(utcnow()))
        assert [p['text'] for p in campaign_hook.posts] == [f'mine: {item}'], "the campaign's secret of the name"
        assert [p['text'] for p in home_hook.posts] == [f'team: piloop {homeless}'], 'only the item of no campaign'
    with db.session() as s:
        ours = [r for r in routes(s, db, LocalSecrets(), {}) if r.owner == campaign.id]
        assert [(r.name, type(r.secrets)) for r in ours] == [('mine', CampaignSecrets)]
        assert ours[0].secrets.missing('NOPE') == (
            'neither campaign demo nor the home has a secret NOPE: set it with `piloop secret set NOPE --campaign demo`,'
            ' or without --campaign for the home'
        )


def test_a_campaign_that_main_stored_before_notifiers_were_per_campaign_gets_config_toml_s(
    db: Database, campaign: Campaign
) -> None:
    with db.session() as s:
        raw = s.get_one(Campaign, campaign.id).config.model_dump(mode='json')
    del raw['plugins']['notifier']
    store_config(db, campaign.id, raw)
    home = {'outbox': Outbox()}
    with db.session() as s:
        assert [(r.owner, r.name) for r in routes(s, db, LocalSecrets(), home)] == [
            (None, 'outbox'),
            (campaign.id, 'outbox'),
        ]


def test_without_use_no_notifier_runs(db: Database, campaign: Campaign, tmp_path: Path, tracer: Tracer) -> None:
    consumer = consumer_named(contribution(host_of(db, tmp_path, tracer, Settings(home=tmp_path).providers)), 'notify')
    write(db, campaign)
    assert consumer.run(Turn(utcnow())) is None
    assert sent_of(db) == []


def test_an_escalation_or_a_p0_item_that_waits_and_that_no_person_wrote_is_due(
    db: Database, campaign: Campaign
) -> None:
    escalation = write(db, campaign)
    due_ones = [
        write(db, campaign, CommentKind.DECISION, 0, 'orchestrator'),
        write(db, campaign, CommentKind.NOTE, 0, 'orchestrator via human:alice'),
        write(db, campaign, FINDING, 0, 'not a signature #'),
    ]
    write(db, campaign, CommentKind.NOTE, 1)
    write(db, campaign, CommentKind.NOTE, None)
    write(db, campaign, author='human')
    write(db, campaign, author='human:alice')
    acked = write(db, campaign)
    with db.session() as s:
        ack(s, [acked], System.HUMAN)
        found = due(s, route_of(db, campaign, 'outbox', Outbox()), utcnow())
    assert [n.comment for n in found.notices] == [*due_ones, escalation], 'by priority, then oldest first'
    assert {n.kind for n in found.notices} == {NoticeKind.NEW}
    assert (found.failures, found.later) == (0, None)


def test_an_item_that_still_waits_after_remind_every_is_sent_again_unless_reminders_are_never(
    db: Database, campaign: Campaign, tmp_path: Path, tracer: Tracer
) -> None:
    host = host_of(db, tmp_path, tracer, {})
    item = write(db, campaign)
    outbox = Outbox(remind_every=timedelta(hours=2))
    now = utcnow()
    later = send_due(host, route_of(db, campaign, 'outbox', outbox), now)
    assert later is not None and later == times(db, NotificationSent)[0] + timedelta(hours=2)
    assert send_due(host, route_of(db, campaign, 'outbox', outbox), now) == later, (
        'nothing new: the reminder is due later'
    )
    assert send_due(host, route_of(db, campaign, 'outbox', outbox), later) == times(db, NotificationSent)[
        1
    ] + timedelta(hours=2)
    assert [[(n.comment, n.kind) for n in sent] for sent in outbox._sent] == [
        [(item, NoticeKind.NEW)],
        [(item, NoticeKind.REMINDER)],
    ]
    with db.session() as s:
        assert (
            due(s, route_of(db, campaign, 'outbox', Outbox(remind_every='never')), now + timedelta(days=9)).notices
            == ()
        )
        assert due(s, route_of(db, campaign, 'other', Outbox()), now).notices != (), 'each notifier has its own record'


def test_a_failure_is_an_event_one_inbox_item_and_a_backoff_and_a_send_ends_the_run_of_failures(
    db: Database, campaign: Campaign, tmp_path: Path, tracer: Tracer, spans: InMemorySpanExporter
) -> None:
    host = host_of(db, tmp_path, tracer, {})
    item = write(db, campaign)
    outbox = Outbox(retry=timedelta(minutes=1), retry_max=timedelta(minutes=3))
    outbox._failing += [NotDelivered('HTTP 404: no_service'), NotDelivered('HTTP 500: oops')]
    now = utcnow()
    assert send_due(host, route_of(db, campaign, 'outbox', outbox), now) == times(db, NotificationFailed)[
        0
    ] + timedelta(minutes=1)
    with db.session() as s:
        assert due(s, route_of(db, campaign, 'outbox', outbox), now).notices == (), (
            'nothing goes out while the backoff lasts'
        )
        [report] = s.scalars(select(Comment).where(Comment.author == 'daemon')).all()
        assert (report.target, report.in_reply_to, report.priority, report.kind) == (
            TargetRef(TargetKind.COMMENT, item),
            item,
            1,
            CommentKind.NOTE,
        )
        assert report.body.startswith("notifier 'outbox' could not send this: HTTP 404: no_service. Fix")
    then = now + timedelta(minutes=2)
    backoff = send_due(host, route_of(db, campaign, 'outbox', outbox), then)
    assert backoff == times(db, NotificationFailed)[1] + timedelta(minutes=2), 'the backoff doubles'
    assert failed_of(db) == [
        NotificationFailed(notifier='outbox', error='HTTP 404: no_service', comments=[item], campaign=campaign.id),
        NotificationFailed(notifier='outbox', error='HTTP 500: oops', comments=[item], campaign=campaign.id),
    ]
    with db.session() as s:
        assert len(s.scalars(select(Comment).where(Comment.author == 'daemon')).all()) == 1, 'one report a run'
        assert [e.target for e, _ in in_order(s, NotificationFailed)] == [HOME, HOME]
    assert spanned(spans, 'notification.failed')[0] == {
        'notifier': 'outbox',
        'comments': 1,
        'error': 'HTTP 404: no_service',
    }
    send_due(host, route_of(db, campaign, 'outbox', outbox), then + timedelta(minutes=2))
    assert [[n.comment for n in sent] for sent in outbox._sent] == [[item]], 'the report is no escalation'
    with db.session() as s:
        assert due(s, route_of(db, campaign, 'outbox', outbox), then).failures == 0, 'a send ends the run of failures'


def test_each_campaign_s_instance_of_a_name_backs_off_on_its_own(
    db: Database, campaign: Campaign, tmp_path: Path, tracer: Tracer
) -> None:
    """A campaign's wrong token does not hold back the items of the home, or of another campaign, of that name."""
    host = host_of(db, tmp_path, tracer, {})
    write(db, campaign)
    homeless = of_no_campaign(db)
    failing, working = Outbox(), Outbox()
    failing._failing.append(NotDelivered('HTTP 403: invalid_token'))
    now = utcnow()
    send_due(host, route_of(db, campaign, 'team', failing), now)
    assert [f.campaign for f in failed_of(db)] == [campaign.id]
    assert send_due(host, route_of(db, None, 'team', working), now) is not None
    assert [[n.comment for n in sent] for sent in working._sent] == [[homeless]]
    with db.session() as s:
        assert due(s, route_of(db, None, 'team', working), now).failures == 0
        assert due(s, route_of(db, campaign, 'team', failing), now).failures == 1


def test_a_wait_that_the_receiver_asks_for_wins_over_the_backoff(
    db: Database, campaign: Campaign, tmp_path: Path, tracer: Tracer
) -> None:
    host = host_of(db, tmp_path, tracer, {})
    write(db, campaign)
    outbox = Outbox()
    outbox._failing.append(NotDelivered('HTTP 429: rate_limited', timedelta(seconds=30)))
    now = utcnow()
    retry = send_due(host, route_of(db, campaign, 'outbox', outbox), now)
    assert retry == times(db, NotificationFailed)[0] + timedelta(seconds=30)
    with db.session() as s:
        assert due(s, route_of(db, campaign, 'outbox', outbox), now).later == retry
        assert due(s, route_of(db, campaign, 'outbox', outbox), now + timedelta(seconds=31)).notices != ()


@pytest.mark.parametrize(('adder', 'owner'), [(Person(name='alice'), Person(name='alice')), (System.HUMAN, None)])
def test_the_owner_of_a_campaign_is_the_person_who_added_it(
    db: Database, campaign: Campaign, adder: Actor, owner: Person | None
) -> None:
    write(db, campaign)
    with db.session() as s:
        emit(s, CampaignAdded(baseline=1), TargetRef(TargetKind.CAMPAIGN, campaign.id), adder)
        s.commit()
        [notice] = due(s, route_of(db, campaign, 'outbox', Outbox()), utcnow()).notices
    assert notice.owner == owner


def test_a_campaign_with_no_added_event_has_no_owner_and_an_old_signature_is_no_person(
    db: Database, campaign: Campaign
) -> None:
    write(db, campaign)
    with db.session() as s:
        [notice] = due(s, route_of(db, campaign, 'outbox', Outbox()), utcnow()).notices
        assert notice.owner is None
        emit(s, CampaignAdded(baseline=1), TargetRef(TargetKind.CAMPAIGN, campaign.id), System.DAEMON)
        s.commit()
        [notice] = due(s, route_of(db, campaign, 'outbox', Outbox()), utcnow()).notices
        assert notice.owner is None
        target = TargetRef(TargetKind.CAMPAIGN, campaign.id)
        s.add(Event(actor='implementer#not-a-session', target=target, payload=CampaignAdded(baseline=1)))
        s.commit()
        [notice] = due(s, route_of(db, campaign, 'outbox', Outbox()), utcnow()).notices
        assert notice.owner is None


def test_a_use_that_names_no_instance_is_refused_in_config_toml_and_in_piloop_toml(
    db: Database, campaign: Campaign, tmp_path: Path, tracer: Tracer
) -> None:
    typo = Settings(home=tmp_path, plugins={'notifier': {'use': ['tema']}})
    with pytest.raises(PluginError, match=r'^config.toml: \[plugins.notifier\] use names tema, which no table defines'):
        contribution(host_of(db, tmp_path, tracer, typo.providers, typo.campaign_defaults))
    with db.session() as s:
        raw = s.get_one(Campaign, campaign.id).config.model_dump(mode='json')
    raw['plugins']['notifier'] = {'use': ['tema']}
    with pytest.raises(ValidationError, match=r"notifier.use\[0\] = 'tema': no \[plugins.notifier.tema\]"):
        CampaignConfig.model_validate(raw)


def posted(db: Database, campaign: Campaign, tmp_path: Path, tracer: Tracer) -> tuple[Chat, CommentId, MessageRef]:
    """A ``Chat`` that posted one escalation: the notifier, the item, and its post."""
    chat = Chat()
    item = write(db, campaign)
    send_due(host_of(db, tmp_path, tracer, {}), route_of(db, campaign, 'chat', chat), utcnow())
    assert sent_of(db) == [(item, NotificationSent(notifier='chat', notice=NoticeKind.NEW, message=f'C/{item}.1'))]
    return chat, item, MessageRef(f'C/{item}.1')


def inbound(post: MessageRef, act: Act, message: str = 'C/9', member: str = 'UALICE') -> Inbound:
    return Inbound(post, MessageRef(message), member, act)


def test_a_reply_in_the_thread_of_a_post_is_a_reply_on_its_item_signed_as_its_person_once(
    db: Database, campaign: Campaign, tmp_path: Path, tracer: Tracer
) -> None:
    chat, item, post = posted(db, campaign, tmp_path, tracer)
    assert received(
        db, tracer, [route_of(db, campaign, 'chat', chat)], inbound(post, Reply('raise it', 2))
    ) == Accepted(f'piloop recorded this as comment#{item + 1}, a reply to comment#{item}')
    assert (
        received(db, tracer, [route_of(db, campaign, 'chat', chat)], inbound(post, Reply('raise it', 2))) == Again()
    ), 'a redelivery'
    with db.session() as s:
        [reply] = s.scalars(select(Comment).where(Comment.author == 'human:alice')).all()
        assert (reply.target, reply.in_reply_to, reply.kind, reply.priority, reply.body) == (
            TargetRef(TargetKind.COMMENT, item),
            item,
            CommentKind.MESSAGE,
            2,
            'raise it',
        )
        [(event, heard)] = in_order(s, NotificationReceived)
        assert (event.target, event.actor, heard) == (
            TargetRef(TargetKind.COMMENT, reply.id),
            'human:alice',
            NotificationReceived(notifier='chat', message='C/9'),
        )
    assert (
        received(
            db,
            tracer,
            [route_of(db, campaign, 'chat', chat)],
            inbound(MessageRef('C/elsewhere'), Reply('hi', 2), 'C/11'),
        )
        == Foreign()
    )
    assert (
        received(db, tracer, [route_of(db, campaign, 'other', chat)], inbound(post, Reply('hi', 2), 'C/12'))
        == Foreign()
    ), "another notifier's post"
    assert received(
        db, tracer, [route_of(db, campaign, 'chat', chat)], inbound(post, Reply('hi', 2), 'C/10', 'UNOBODY')
    ) == Declined('you are not in [plugins.notifier.chat] people: ask whoever runs piloop to add you')


def test_piloop_s_answers_in_a_posted_thread_go_back_to_it_and_what_came_from_it_does_not(
    db: Database, campaign: Campaign, tmp_path: Path, tracer: Tracer
) -> None:
    before = write(db, campaign, CommentKind.NOTE, None)
    chat, item, post = posted(db, campaign, tmp_path, tracer)
    host = host_of(db, tmp_path, tracer, {})
    received(db, tracer, [route_of(db, campaign, 'chat', chat)], inbound(post, Reply('raise it', 2)))
    with db.session() as s:
        [person_reply] = s.scalars(select(Comment).where(Comment.author == 'human:alice')).all()
        target = TargetRef(TargetKind.COMMENT, item)
        early = s.get_one(Comment, before)
        answer = Comment(
            target=target, in_reply_to=person_reply.id, author='orchestrator', kind=CommentKind.DECISION, body='raised'
        )
        note = Comment(target=target, author='daemon', kind=CommentKind.NOTE, body='the daemon says')
        on_other = Comment(target=TargetRef(TargetKind.COMMENT, early.id), author='orchestrator', body='not posted')
        s.add_all([answer, note, on_other])
        s.commit()
        answer_id = answer.id
    send_due(host, route_of(db, campaign, 'chat', chat), utcnow())
    [sent] = chat._sent[1:]
    assert [(n.kind, n.comment, n.thread, n.priority) for n in sent] == [
        (NoticeKind.FOLLOW_UP, answer_id, post, None),
    ]
    assert sent[0].campaign == campaign.name
    send_due(host, route_of(db, campaign, 'chat', chat), utcnow())
    assert len(chat._sent) == 2, 'each answer goes back once'
    outbox = Outbox()
    send_due(host, route_of(db, campaign, 'outbox', outbox), utcnow())
    assert all(n.kind is not NoticeKind.FOLLOW_UP for sent in outbox._sent for n in sent), 'a webhook has no threads'


def test_a_reaction_acks_the_item_and_a_seed_is_filed_in_its_campaign(
    db: Database, campaign: Campaign, tmp_path: Path, tracer: Tracer, mocker: MockerFixture
) -> None:
    chat, item, post = posted(db, campaign, tmp_path, tracer)
    assert received(db, tracer, [route_of(db, campaign, 'chat', chat)], inbound(post, Ack(), 'C/ack')) == Accepted(
        f'piloop acknowledged comment#{item}'
    )
    assert received(
        db, tracer, [route_of(db, campaign, 'chat', chat)], inbound(post, FileSeed('halve the window'), 'C/seed')
    ) == Accepted('piloop filed this as seed idea#1, for research')
    assert received(
        db, tracer, [route_of(db, campaign, 'chat', chat)], inbound(post, FileSeed(''), 'C/empty')
    ) == Declined('write the hunch after "idea:", in the thread of an item of a campaign')
    mocker.patch.object(consumers, 'file_seed', side_effect=BacklogRefused('human:alice files nothing'))
    assert received(
        db, tracer, [route_of(db, campaign, 'chat', chat)], inbound(post, FileSeed('x'), 'C/refused')
    ) == Declined('human:alice files nothing')
    with db.session() as s:
        acked = s.get_one(Comment, item)
        assert (acked.acked_by, acked.acked_at is not None) == ('human:alice', True)
        [seed] = s.scalars(select(Idea).where(Idea.campaign_id == campaign.id, Idea.title == 'halve the window')).all()
        assert (seed.status, seed.created_by, seed.provenance) == (
            IdeaStatus.SEED,
            'human:alice',
            f'comment#{item}, via chat',
        )
        assert [e.target for e, _ in in_order(s, NotificationReceived)] == [
            TargetRef(TargetKind.COMMENT, item),
            TargetRef(IDEA, seed.id),
        ]


def test_an_item_of_no_campaign_takes_no_seed(db: Database, tmp_path: Path, tracer: Tracer) -> None:
    of_no_campaign(db)
    chat = Chat()
    send_due(host_of(db, tmp_path, tracer, {}), route_of(db, None, 'chat', chat), utcnow())
    [[notice]] = chat._sent
    assert notice.campaign is None
    post = MessageRef(f'C/{notice.comment}.1')
    assert received(db, tracer, [route_of(db, None, 'chat', chat)], inbound(post, FileSeed('x'))) == Declined(
        'write the hunch after "idea:", in the thread of an item of a campaign'
    )


def test_a_failure_after_some_posts_records_those_posts_as_sent(
    db: Database, campaign: Campaign, tmp_path: Path, tracer: Tracer
) -> None:
    first, second = write(db, campaign), write(db, campaign)
    chat = Chat()
    chat._failing.append(NotDelivered('chat.postMessage: ratelimited', None, [MessageRef('C/first')]))
    send_due(host_of(db, tmp_path, tracer, {}), route_of(db, campaign, 'chat', chat), utcnow())
    assert sent_of(db) == [(first, NotificationSent(notifier='chat', notice=NoticeKind.NEW, message='C/first'))]
    assert failed_of(db) == [
        NotificationFailed(
            notifier='chat', error='chat.postMessage: ratelimited', comments=[second], campaign=campaign.id
        )
    ]


def set_key(db: Database, owner: CampaignId | None, value: str) -> None:
    with db.session() as s:
        set_secret(s, LocalSecrets(), System.HUMAN, owner, 'CHAT_KEY', SecretStr(value))
        s.commit()


def test_routes_of_one_connection_share_one_listener_which_opens_closes_and_reopens_as_campaigns_and_secrets_change(
    db: Database, campaign: Campaign, tmp_path: Path, tracer: Tracer, spans: InMemorySpanExporter, mocker: MockerFixture
) -> None:
    """A service such as Slack splits one app's events across its open connections: a second one loses answers."""
    home_chat, campaign_chat = Chat(), Chat()
    found = [
        route_of(db, None, 'outbox', Outbox()),
        route_of(db, None, 'chat', home_chat),
        route_of(db, campaign, 'chat', campaign_chat),
    ]
    mocker.patch.object(consumers, 'routes', side_effect=lambda *_: found)
    made = contribution(host_of(db, tmp_path, tracer, {}))
    listeners, consumer = made.lifecycle, consumer_named(made, 'listeners')
    assert isinstance(listeners, Listeners)
    assert (consumer.phase, consumer.wakes_on) == (
        Phase.CONFIGURE,
        frozenset({CampaignAdded, CampaignConfigChanged, CampaignStatusChanged, SecretSet, SecretDeleted}),
    )
    listeners.start()
    assert (home_chat._hosts, campaign_chat._hosts) == ([], []), 'no key: nothing to listen with'
    assert {e['notifier'] for e in spanned(spans, 'notification.listen_failed')} == {'chat'}

    set_key(db, None, 'k1')
    assert consumer.run(Turn(utcnow())) is None
    assert (len(home_chat._hosts), campaign_chat._hosts) == (1, []), 'one listener for both, which the first opens'
    [opened] = home_chat._listeners
    consumer.run(Turn(utcnow()))
    assert opened.calls == ['start'] and len(home_chat._hosts) == 1, 'nothing changed: it stays open'

    item = write(db, campaign)
    send_due(host_of(db, tmp_path, tracer, {}), found[2], utcnow())
    post = MessageRef(f'C/{item}.1')
    assert home_chat._hosts[0].received(inbound(post, Ack(), 'C/ack')) == Accepted(
        f'piloop acknowledged comment#{item}'
    )
    with db.session() as s:
        [(_, heard)] = in_order(s, NotificationReceived)
        assert heard.notifier == 'chat', "the campaign's post, answered through the shared listener"

    set_key(db, campaign.id, 'k2')
    consumer.run(Turn(utcnow()))
    assert opened.calls == ['start'] and [len(c._hosts) for c in (home_chat, campaign_chat)] == [1, 1], (
        "the campaign's own key: a connection of its own"
    )
    set_key(db, None, 'k3')
    consumer.run(Turn(utcnow()))
    assert opened.calls == ['start', 'stop stop'] and len(home_chat._hosts) == 2, 'a new key: closed and reopened'
    found.pop()
    consumer.run(Turn(utcnow()))
    assert campaign_chat._listeners[0].calls == ['start', 'stop stop'], 'a route that went: its connection closes'
    listeners.stop(Leaving.HAND_OVER, 1)
    assert home_chat._listeners[-1].calls == ['start', 'stop hand_over']
    assert listeners.in_flight() == {}


def test_a_failure_to_write_an_answer_is_declined_and_a_span(
    db: Database, campaign: Campaign, tmp_path: Path, tracer: Tracer, spans: InMemorySpanExporter, mocker: MockerFixture
) -> None:
    chat, _, post = posted(db, campaign, tmp_path, tracer)
    host = host_of(db, tmp_path, tracer, {})
    mocker.patch.object(consumers, 'received', side_effect=RuntimeError('disk full'))
    assert _answering(host, lambda: [route_of(db, campaign, 'chat', chat)])(inbound(post, Reply('x', 2))) == Declined(
        'piloop could not write this: RuntimeError: disk full'
    )
    _failing(host, 'chat')('could not answer in Slack: user_not_in_channel')
    assert spanned(spans, 'notification.receive_failed') == [{'notifier': 'chat', 'error': 'RuntimeError: disk full'}]
    assert spanned(spans, 'notification.listen_failed') == [
        {'notifier': 'chat', 'error': 'could not answer in Slack: user_not_in_channel'}
    ]


def test_a_slack_app_of_config_toml_and_one_of_a_campaign_with_one_app_token_open_one_socket_once_it_is_set(
    db: Database, campaign: Campaign, tmp_path: Path, tracer: Tracer
) -> None:
    with fake_slack_app() as slack:
        app = {'provider': 'slack_app', 'api': slack.api}
        home = Settings(home=tmp_path, plugins={'notifier': {'use': ['team'], 'team': {**app, 'channel': 'C1'}}})
        configure(db, campaign, {'use': ['mine'], 'mine': {**app, 'channel': 'C2'}})
        made = contribution(host_of(db, tmp_path, tracer, home.providers, home.campaign_defaults))
        listeners = made.lifecycle
        assert isinstance(listeners, Listeners)
        listeners.start()
        try:
            assert slack.connections == 0, 'no app token yet'
            with db.session() as s:
                for name, value in (('SLACK_BOT_TOKEN', 'xoxb-bot'), ('SLACK_APP_TOKEN', 'xapp-app')):
                    set_secret(s, LocalSecrets(), System.HUMAN, None, name, SecretStr(value))
                s.commit()
            consumer_named(made, 'listeners').run(Turn(utcnow()))
            wait_until(lambda: slack.connections == 1, 'the socket, with no restart')
            consumer_named(made, 'listeners').run(Turn(utcnow()))
            assert slack.connections == 1, 'one socket for both instances of the app'
        finally:
            listeners.stop(Leaving.STOP, 1)


def test_an_edit_of_a_reply_changes_it_as_its_author_and_it_waits_to_be_read_again(
    db: Database, campaign: Campaign, tmp_path: Path, tracer: Tracer
) -> None:
    chat, item, post = posted(db, campaign, tmp_path, tracer)
    received(db, tracer, [route_of(db, campaign, 'chat', chat)], inbound(post, Reply('aise it', 2), 'C/9'))
    received(db, tracer, [route_of(db, campaign, 'chat', chat)], inbound(post, FileSeed('a sieve'), 'C/seed'))
    with db.session() as s:
        [reply] = s.scalars(select(Comment).where(Comment.author == 'human:alice')).all()
        ack(s, [reply.id], System.ORCHESTRATOR)
        cursor = newest(s)
    edit = Edit('raise it')
    assert received(db, tracer, [route_of(db, campaign, 'chat', chat)], inbound(post, edit, 'C/9')) == Accepted(
        f'piloop updated comment#{reply.id}'
    )
    assert received(db, tracer, [route_of(db, campaign, 'chat', chat)], inbound(post, edit, 'C/9')) == Again(), (
        'the same text'
    )
    assert received(db, tracer, [route_of(db, campaign, 'chat', chat)], inbound(post, edit, 'C/9', 'UBOB')) == Declined(
        f'comment#{reply.id} is by human:alice: only its author changes it'
    )
    assert received(
        db, tracer, [route_of(db, campaign, 'chat', chat)], inbound(post, edit, 'C/9', 'UNOBODY')
    ) == Declined('you are not in [plugins.notifier.chat] people: ask whoever runs piloop to add you')
    assert received(db, tracer, [route_of(db, campaign, 'chat', chat)], inbound(post, edit, 'C/never')) == Foreign()
    assert received(db, tracer, [route_of(db, campaign, 'chat', chat)], inbound(post, edit, 'C/seed')) == Foreign(), (
        'a seed is kept as filed'
    )
    with db.session() as s:
        edited = s.get_one(Comment, reply.id)
        assert (edited.body, edited.acked_at, edited.acked_by) == ('raise it', None, None)
        [(_, change)] = in_order(s, CommentEdited)
        assert (change.old, change.new) == ('aise it', 'raise it')
        found = news(s, cursor, campaign_id=campaign.id, party='orchestrator')
    assert [(n.comment.id, n.comment.body) for n in found.items if isinstance(n, CommentNews)] == [
        (reply.id, 'raise it')
    ], 'the orchestrator that read the old text gets the new one as news'


def test_an_agent_s_escalation_waits_its_grace_for_the_orchestrator_then_reaches_a_human_with_its_view(
    db: Database, campaign: Campaign
) -> None:
    form: JsonValue = {
        'ask': 'Do you approve a longer timeout?',
        'why': 'Each run takes 3 hours.',
        'options': [
            {'choice': 'Raise it.', 'result': 'Runs cost more.'},
            {'choice': 'Keep it.', 'result': 'It stops.'},
        ],
        'recommend': 1,
    }
    with db.session() as s:
        filed = escalations.escalate(
            s,
            TargetRef(TargetKind.CAMPAIGN, campaign.id),
            form,
            author=Agent(role='implementer', session=12),
            priority=1,
        )
        found = due(s, route_of(db, campaign, 'outbox', Outbox()), filed.created_at)
        assert (found.notices, found.later) == ((), filed.created_at + timedelta(minutes=30))
        [notice] = due(s, route_of(db, campaign, 'outbox', Outbox()), filed.created_at + timedelta(minutes=30)).notices
    assert notice.view is not None and [c.id for c in notice.view.choices] == ['A', 'B']
    assert Slack().text([notice]).splitlines()[1:6] == [
        'Do you approve a longer timeout?',
        '',
        'Each run takes 3 hours.',
        '',
        'A. Raise it. → Runs cost more.',
    ]


class Silent(Structured):
    """A provider whose escalations never reach a human through the notifiers."""

    def reach(self, item: Escalated, now: datetime) -> ToHuman:
        return Never()


def test_a_provider_decides_whether_an_escalation_reaches_a_human_and_a_broken_config_falls_back_to_the_default(
    db: Database, campaign: Campaign, mocker: MockerFixture
) -> None:
    write(db, campaign, author='daemon')
    with db.session() as s:
        s.add(
            Comment(
                target=TargetRef(INSTANCE, 1),
                author='daemon',
                kind=CommentKind.ESCALATION,
                priority=0,
                body='a worker is gone',
            )
        )
        s.commit()
    store_config(db, campaign.id, {'name': 'demo', 'plugins': {'escalation': {'provider': 'nope'}}})
    with db.session() as s:
        assert isinstance(escalations.policy(s, campaign.id), Structured), 'a stored config that no longer loads'
        routes_of = [route_of(db, owner, 'outbox', Outbox()) for owner in (campaign, None)]
        assert [len(due(s, route, utcnow()).notices) for route in routes_of] == [1, 1]
        mocker.patch.object(escalations, 'policy', return_value=Silent())
        assert [due(s, route, utcnow()).notices for route in routes_of] == [(), ()]


def test_config_toml_s_notifiers_are_defaults_that_a_campaign_s_piloop_toml_overrides_key_by_key(
    campaign_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / 'home'
    home.mkdir()
    (home / 'config.toml').write_text(
        '[plugins.notifier]\nuse = ["team"]\n\n[plugins.notifier.team]\nprovider = "slack_app"\nchannel = "C1"\n'
        'people = { alice = "UALICE" }\n\n[plugins.notifier.spare]\nprovider = "slack"\n'
    )
    toml = campaign_path / 'piloop.toml'
    toml.write_text(
        toml.read_text() + '\n[plugins.notifier.team]\nchannel = "C2"\ntoken = { secret = "ENWIK8_BOT_TOKEN" }\n'
    )
    settings = Settings(home=home)
    monkeypatch.setattr(config_module, 'settings', settings)
    cfg = load_campaign_config(campaign_path)
    assert NOTIFIERS.used(cfg.plugins) == ('team',), "the home's use, and a default that it does not use is no error"
    team = NOTIFIERS.instances(cfg.plugins)['team']
    assert isinstance(team, SlackApp)
    assert (team.channel, team.token.secret, team.people) == ('C2', 'ENWIK8_BOT_TOKEN', {'alice': 'UALICE'})
    assert 'notifier' not in settings.providers, "the home's own [plugins] leave the campaigns' defaults out"
    assert home_notifiers(settings.campaign_defaults) == {'team': SlackApp(channel='C1', people={'alice': 'UALICE'})}
