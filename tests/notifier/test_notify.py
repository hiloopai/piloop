"""The notifier machinery: what is due, what one pass sends, and how a failure is recorded and retried."""

from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import PrivateAttr, SecretStr
from pytest_mock import MockerFixture
from sqlalchemy import select

from piloop.actor import Actor, Person, System
from piloop.api.views import CommentNews
from piloop.campaign.campaigns import CampaignAdded
from piloop.campaign.inbox import ack, news
from piloop.campaign.ops import Refused as BacklogRefused
from piloop.daemon.loop import Phase, Turn
from piloop.db.events import emit, in_order
from piloop.db.feed import newest
from piloop.db.models import Campaign, Comment, Event, utcnow
from piloop.db.session import Database
from piloop.db.triggers.changelog import Commented, CommentEdited
from piloop.ids import HOME, CommentId, CommentKind, TargetKind, TargetRef
from piloop.otel import Tracer
from piloop.plugins.campaign_type.kit.backlog.ids import IdeaStatus
from piloop.plugins.campaign_type.kit.backlog.tables import Idea
from piloop.plugins.campaign_type.kit.runs.controller import RunController
from piloop.plugins.contribution import DaemonHost, Leaving, Lifecycle
from piloop.plugins.executor.workers import Workers
from piloop.plugins.host import Plugins
from piloop.plugins.notifier import consumers
from piloop.plugins.notifier.consumers import Listeners, _answering, _failing, contribution, due, received, send_due
from piloop.plugins.notifier.contract import (
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
from piloop.plugins.notifier.slack_app import SocketListener
from piloop.plugins.secrets.contract import HomeSecrets, set_secret
from piloop.plugins.secrets.local import LocalSecrets
from piloop.runtime.factory import make_runtime
from piloop.settings import AgentSettings, DaemonSettings, Settings
from tests.fixtures.slack import FakeSlack


class Outbox(Notifier):
    """A notifier that keeps what it sends, and raises each failure that ``failing`` holds first."""

    _sent: list[list[Notice]] = PrivateAttr(default_factory=list)
    _failing: list[NotDelivered] = PrivateAttr(default_factory=list)

    def send(self, notices: Sequence[Notice], secrets: HomeSecrets) -> Sequence[MessageRef | None]:
        if self._failing:
            raise self._failing.pop(0)
        self._sent.append(list(notices))
        return [None] * len(notices)


class Chat(Outbox):
    """A notifier that posts each notice with an id, ``C/<comment>.<send>``, and listens."""

    _hosts: list[ListenHost] = PrivateAttr(default_factory=list)

    def send(self, notices: Sequence[Notice], secrets: HomeSecrets) -> Sequence[MessageRef | None]:
        super().send(notices, secrets)
        return [MessageRef(f'C/{n.comment}.{len(self._sent)}') for n in notices]

    def listen(self, host: ListenHost) -> Lifecycle:
        self._hosts.append(host)
        return Recorder()


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


def host_of(db: Database, tmp_path: Path, tracer: Tracer, plugins: Plugins) -> DaemonHost:
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
    )


def no_wake(_name: str) -> None:
    return None


def no_thread(_body: Callable[[], None]) -> Future[None]:
    raise AssertionError('the notifiers start no thread')


def write(
    db: Database,
    campaign: Campaign,
    kind: CommentKind = CommentKind.ESCALATION,
    priority: int | None = 1,
    author: str = 'implementer#12',
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


def test_the_consumer_posts_each_due_item_once_to_each_notifier_in_use(
    db: Database, campaign: Campaign, tmp_path: Path, tracer: Tracer, spans: InMemorySpanExporter, slack: FakeSlack
) -> None:
    with db.session() as s:
        set_secret(s, LocalSecrets(), System.HUMAN, None, 'TEAM_WEBHOOK', SecretStr(slack.url))
        s.commit()
    home = Settings(
        home=tmp_path,
        plugins={
            'notifier': {
                'use': ['team'],
                'team': {
                    'provider': 'slack',
                    'webhook': {'secret': 'TEAM_WEBHOOK'},
                    'link': 'https://p.example/{comment}',
                },
            }
        },
    )
    [consumer] = contribution(host_of(db, tmp_path, tracer, home.providers)).consumers
    assert (consumer.name, consumer.phase, consumer.wakes_on) == ('notify', Phase.UPKEEP, frozenset({Commented}))
    item = write(db, campaign)
    now = utcnow()
    again = consumer.run(Turn(now))
    [sent_at] = times(db, NotificationSent)
    assert [post['text'] for post in slack.posts] == [
        f':rotating_light: *{campaign.name}* · escalation comment#{item} · P1 · from implementer#12\nx\n'
        f'<https://p.example/{item}|open>'
    ]
    assert sent_of(db) == [(item, NotificationSent(notifier='team', notice=NoticeKind.NEW))]
    assert spanned(spans, 'notification.sent') == [{'notifier': 'team', 'comments': 1}]
    assert again == sent_at + timedelta(hours=24), 'the reminder'
    assert consumer.run(Turn(utcnow())) == again
    assert len(slack.posts) == 1, 'a pass after a restart sends nothing twice: the sent event says it was sent'


def test_without_use_no_notifier_runs(db: Database, campaign: Campaign, tmp_path: Path, tracer: Tracer) -> None:
    [consumer] = contribution(host_of(db, tmp_path, tracer, Settings(home=tmp_path).providers)).consumers
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
        write(db, campaign, CommentKind.FINDING, 0, 'not a signature #'),
    ]
    write(db, campaign, CommentKind.NOTE, 1)
    write(db, campaign, CommentKind.NOTE, None)
    write(db, campaign, author='human')
    write(db, campaign, author='human:alice')
    acked = write(db, campaign)
    with db.session() as s:
        ack(s, [acked], System.HUMAN)
        found = due(s, 'outbox', Outbox(), utcnow())
    assert [n.comment for n in found.notices] == [*due_ones, escalation], 'by priority, then oldest first'
    assert {n.kind for n in found.notices} == {NoticeKind.NEW}
    assert (found.failures, found.later) == (0, None)


def test_an_item_that_still_waits_after_remind_every_is_sent_again_unless_reminders_are_never(
    db: Database, campaign: Campaign, tmp_path: Path, tracer: Tracer
) -> None:
    host = host_of(db, tmp_path, tracer, {})
    secrets = HomeSecrets(db, LocalSecrets())
    item = write(db, campaign)
    outbox = Outbox(remind_every=timedelta(hours=2))
    now = utcnow()
    later = send_due(host, secrets, 'outbox', outbox, now)
    assert later is not None and later == times(db, NotificationSent)[0] + timedelta(hours=2)
    assert send_due(host, secrets, 'outbox', outbox, now) == later, 'nothing new: the reminder is due later'
    assert send_due(host, secrets, 'outbox', outbox, later) == times(db, NotificationSent)[1] + timedelta(hours=2)
    assert [[(n.comment, n.kind) for n in sent] for sent in outbox._sent] == [
        [(item, NoticeKind.NEW)],
        [(item, NoticeKind.REMINDER)],
    ]
    with db.session() as s:
        assert due(s, 'outbox', Outbox(remind_every='never'), now + timedelta(days=9)).notices == ()
        assert due(s, 'other', Outbox(), now).notices != (), 'each notifier has its own record'


def test_a_failure_is_an_event_one_inbox_item_and_a_backoff_and_a_send_ends_the_run_of_failures(
    db: Database, campaign: Campaign, tmp_path: Path, tracer: Tracer, spans: InMemorySpanExporter
) -> None:
    host = host_of(db, tmp_path, tracer, {})
    secrets = HomeSecrets(db, LocalSecrets())
    item = write(db, campaign)
    outbox = Outbox(retry=timedelta(minutes=1), retry_max=timedelta(minutes=3))
    outbox._failing += [NotDelivered('HTTP 404: no_service'), NotDelivered('HTTP 500: oops')]
    now = utcnow()
    assert send_due(host, secrets, 'outbox', outbox, now) == times(db, NotificationFailed)[0] + timedelta(minutes=1)
    with db.session() as s:
        assert due(s, 'outbox', outbox, now).notices == (), 'nothing goes out while the backoff lasts'
        [report] = s.scalars(select(Comment).where(Comment.author == 'daemon')).all()
        assert (report.target, report.in_reply_to, report.priority, report.kind) == (
            TargetRef(TargetKind.COMMENT, item),
            item,
            1,
            CommentKind.NOTE,
        )
        assert report.body.startswith("notifier 'outbox' could not send this: HTTP 404: no_service. Fix")
    then = now + timedelta(minutes=2)
    backoff = send_due(host, secrets, 'outbox', outbox, then)
    assert backoff == times(db, NotificationFailed)[1] + timedelta(minutes=2), 'the backoff doubles'
    assert failed_of(db) == [
        NotificationFailed(notifier='outbox', error='HTTP 404: no_service', comments=[item]),
        NotificationFailed(notifier='outbox', error='HTTP 500: oops', comments=[item]),
    ]
    with db.session() as s:
        assert len(s.scalars(select(Comment).where(Comment.author == 'daemon')).all()) == 1, 'one report a run'
        assert [e.target for e, _ in in_order(s, NotificationFailed)] == [HOME, HOME]
    assert spanned(spans, 'notification.failed')[0] == {
        'notifier': 'outbox',
        'comments': 1,
        'error': 'HTTP 404: no_service',
    }
    send_due(host, secrets, 'outbox', outbox, then + timedelta(minutes=2))
    assert [[n.comment for n in sent] for sent in outbox._sent] == [[item]], 'the report is no escalation'
    with db.session() as s:
        assert due(s, 'outbox', outbox, then).failures == 0, 'a send ends the run of failures'


def test_a_wait_that_the_receiver_asks_for_wins_over_the_backoff(
    db: Database, campaign: Campaign, tmp_path: Path, tracer: Tracer
) -> None:
    host = host_of(db, tmp_path, tracer, {})
    write(db, campaign)
    outbox = Outbox()
    outbox._failing.append(NotDelivered('HTTP 429: rate_limited', timedelta(seconds=30)))
    now = utcnow()
    retry = send_due(host, HomeSecrets(db, LocalSecrets()), 'outbox', outbox, now)
    assert retry == times(db, NotificationFailed)[0] + timedelta(seconds=30)
    with db.session() as s:
        assert due(s, 'outbox', outbox, now).later == retry
        assert due(s, 'outbox', outbox, now + timedelta(seconds=31)).notices != ()


@pytest.mark.parametrize(('adder', 'owner'), [(Person(name='alice'), Person(name='alice')), (System.HUMAN, None)])
def test_the_owner_of_a_campaign_is_the_person_who_added_it(
    db: Database, campaign: Campaign, adder: Actor, owner: Person | None
) -> None:
    write(db, campaign)
    with db.session() as s:
        emit(s, CampaignAdded(baseline=1), TargetRef(TargetKind.CAMPAIGN, campaign.id), adder)
        s.commit()
        [notice] = due(s, 'outbox', Outbox(), utcnow()).notices
    assert notice.owner == owner


def test_a_campaign_with_no_added_event_has_no_owner_and_an_old_signature_is_no_person(
    db: Database, campaign: Campaign
) -> None:
    write(db, campaign)
    with db.session() as s:
        [notice] = due(s, 'outbox', Outbox(), utcnow()).notices
        assert notice.owner is None
        emit(s, CampaignAdded(baseline=1), TargetRef(TargetKind.CAMPAIGN, campaign.id), System.DAEMON)
        s.commit()
        [notice] = due(s, 'outbox', Outbox(), utcnow()).notices
        assert notice.owner is None
        target = TargetRef(TargetKind.CAMPAIGN, campaign.id)
        s.add(Event(actor='implementer#not-a-session', target=target, payload=CampaignAdded(baseline=1)))
        s.commit()
        [notice] = due(s, 'outbox', Outbox(), utcnow()).notices
        assert notice.owner is None


def test_config_toml_refuses_a_notifier_that_no_use_names_and_a_use_that_names_none(tmp_path: Path) -> None:
    unused = Settings(home=tmp_path, plugins={'notifier': {'team': {'provider': 'slack'}}})
    with pytest.raises(
        ValueError,
        match=r'\[plugins.notifier.team\] is configured, and nothing uses it: add it to \[plugins.notifier\] use',
    ):
        _ = unused.providers
    typo = Settings(home=tmp_path, plugins={'notifier': {'use': ['tema']}})
    with pytest.raises(ValueError, match=r"notifier.use\[0\] = 'tema': no \[plugins.notifier.tema\]; there are slack"):
        _ = typo.providers
    assert Settings(home=tmp_path, plugins={'notifier': {'use': ['slack']}}).providers['notifier'] is not None


def posted(db: Database, campaign: Campaign, tmp_path: Path, tracer: Tracer) -> tuple[Chat, CommentId, MessageRef]:
    """A ``Chat`` that posted one escalation: the notifier, the item, and its post."""
    chat = Chat()
    item = write(db, campaign)
    send_due(host_of(db, tmp_path, tracer, {}), HomeSecrets(db, LocalSecrets()), 'chat', chat, utcnow())
    assert sent_of(db) == [(item, NotificationSent(notifier='chat', notice=NoticeKind.NEW, message=f'C/{item}.1'))]
    return chat, item, MessageRef(f'C/{item}.1')


ALICE = Person(name='alice')


def inbound(post: MessageRef, act: Act, message: str = 'C/9', person: Person | None = ALICE) -> Inbound:
    return Inbound(post, MessageRef(message), person, act)


def test_a_reply_in_the_thread_of_a_post_is_a_reply_on_its_item_signed_as_its_person_once(
    db: Database, campaign: Campaign, tmp_path: Path, tracer: Tracer
) -> None:
    chat, item, post = posted(db, campaign, tmp_path, tracer)
    assert received(db, tracer, 'chat', chat, inbound(post, Reply('raise it', 2))) == Accepted(
        f'piloop recorded this as comment#{item + 1}, a reply to comment#{item}'
    )
    assert received(db, tracer, 'chat', chat, inbound(post, Reply('raise it', 2))) == Again(), 'a redelivery'
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
    assert received(db, tracer, 'chat', chat, inbound(MessageRef('C/elsewhere'), Reply('hi', 2), 'C/11')) == Foreign()
    assert received(db, tracer, 'other', chat, inbound(post, Reply('hi', 2), 'C/12')) == Foreign(), (
        "another notifier's post"
    )
    assert received(db, tracer, 'chat', chat, inbound(post, Reply('hi', 2), 'C/10', None)) == Declined(
        'you are not in [plugins.notifier.chat] people: ask whoever runs piloop to add you'
    )


def test_piloop_s_answers_in_a_posted_thread_go_back_to_it_and_what_came_from_it_does_not(
    db: Database, campaign: Campaign, tmp_path: Path, tracer: Tracer
) -> None:
    before = write(db, campaign, CommentKind.NOTE, None)
    chat, item, post = posted(db, campaign, tmp_path, tracer)
    host, secrets = host_of(db, tmp_path, tracer, {}), HomeSecrets(db, LocalSecrets())
    received(db, tracer, 'chat', chat, inbound(post, Reply('raise it', 2)))
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
    send_due(host, secrets, 'chat', chat, utcnow())
    [sent] = chat._sent[1:]
    assert [(n.kind, n.comment, n.thread, n.priority) for n in sent] == [
        (NoticeKind.FOLLOW_UP, answer_id, post, None),
    ]
    assert sent[0].campaign == campaign.name
    send_due(host, secrets, 'chat', chat, utcnow())
    assert len(chat._sent) == 2, 'each answer goes back once'
    outbox = Outbox()
    send_due(host, secrets, 'outbox', outbox, utcnow())
    assert all(n.kind is not NoticeKind.FOLLOW_UP for sent in outbox._sent for n in sent), 'a webhook has no threads'


def test_a_reaction_acks_the_item_and_a_seed_is_filed_in_its_campaign(
    db: Database, campaign: Campaign, tmp_path: Path, tracer: Tracer, mocker: MockerFixture
) -> None:
    chat, item, post = posted(db, campaign, tmp_path, tracer)
    assert received(db, tracer, 'chat', chat, inbound(post, Ack(), 'C/ack')) == Accepted(
        f'piloop acknowledged comment#{item}'
    )
    assert received(db, tracer, 'chat', chat, inbound(post, FileSeed('halve the window'), 'C/seed')) == Accepted(
        'piloop filed this as seed idea#1, for research'
    )
    assert received(db, tracer, 'chat', chat, inbound(post, FileSeed(''), 'C/empty')) == Declined(
        'write the hunch after "idea:", in the thread of an item of a campaign'
    )
    mocker.patch.object(consumers, 'file_seed', side_effect=BacklogRefused('human:alice files nothing'))
    assert received(db, tracer, 'chat', chat, inbound(post, FileSeed('x'), 'C/refused')) == Declined(
        'human:alice files nothing'
    )
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
            TargetRef(TargetKind.IDEA, seed.id),
        ]


def test_an_item_of_no_campaign_takes_no_seed(db: Database, tmp_path: Path, tracer: Tracer) -> None:
    with db.session() as s:
        s.add(
            Comment(
                target=TargetRef(TargetKind.INSTANCE, 1),
                author='daemon',
                kind=CommentKind.ESCALATION,
                priority=0,
                body='x',
            )
        )
        s.commit()
    chat = Chat()
    send_due(host_of(db, tmp_path, tracer, {}), HomeSecrets(db, LocalSecrets()), 'chat', chat, utcnow())
    [[notice]] = chat._sent
    assert notice.campaign is None
    post = MessageRef(f'C/{notice.comment}.1')
    assert received(db, tracer, 'chat', chat, inbound(post, FileSeed('x'))) == Declined(
        'write the hunch after "idea:", in the thread of an item of a campaign'
    )


def test_a_failure_after_some_posts_records_those_posts_as_sent(
    db: Database, campaign: Campaign, tmp_path: Path, tracer: Tracer
) -> None:
    first, second = write(db, campaign), write(db, campaign)
    chat = Chat()
    chat._failing.append(NotDelivered('chat.postMessage: ratelimited', None, [MessageRef('C/first')]))
    send_due(host_of(db, tmp_path, tracer, {}), HomeSecrets(db, LocalSecrets()), 'chat', chat, utcnow())
    assert sent_of(db) == [(first, NotificationSent(notifier='chat', notice=NoticeKind.NEW, message='C/first'))]
    assert failed_of(db) == [
        NotificationFailed(notifier='chat', error='chat.postMessage: ratelimited', comments=[second])
    ]


def test_each_listening_notifier_in_use_starts_and_stops_with_the_daemon_and_a_failure_is_a_span(
    db: Database, campaign: Campaign, tmp_path: Path, tracer: Tracer, spans: InMemorySpanExporter, mocker: MockerFixture
) -> None:
    home = Settings(
        home=tmp_path, plugins={'notifier': {'use': ['team'], 'team': {'provider': 'slack_app', 'channel': 'C1'}}}
    )
    made = contribution(host_of(db, tmp_path, tracer, home.providers))
    assert isinstance(made.lifecycle, Listeners) and [type(e) for e in made.lifecycle.each] == [SocketListener]
    recorders = Listeners((Recorder(), Recorder()))
    recorders.start()
    recorders.stop(Leaving.HAND_OVER, 1)
    assert recorders.in_flight() == {}
    assert [r.calls for r in recorders.each if isinstance(r, Recorder)] == [['start', 'stop hand_over']] * 2
    chat, _, post = posted(db, campaign, tmp_path, tracer)
    host = host_of(db, tmp_path, tracer, {})
    mocker.patch.object(consumers, 'received', side_effect=RuntimeError('disk full'))
    assert _answering(host, 'chat', chat)(inbound(post, Reply('x', 2))) == Declined(
        'piloop could not write this: RuntimeError: disk full'
    )
    _failing(host, 'chat')('could not answer in Slack: user_not_in_channel')
    assert spanned(spans, 'notification.receive_failed') == [{'notifier': 'chat', 'error': 'RuntimeError: disk full'}]
    assert spanned(spans, 'notification.listen_failed') == [
        {'notifier': 'chat', 'error': 'could not answer in Slack: user_not_in_channel'}
    ]


def test_an_edit_of_a_reply_changes_it_as_its_author_and_it_waits_to_be_read_again(
    db: Database, campaign: Campaign, tmp_path: Path, tracer: Tracer
) -> None:
    chat, item, post = posted(db, campaign, tmp_path, tracer)
    received(db, tracer, 'chat', chat, inbound(post, Reply('aise it', 2), 'C/9'))
    received(db, tracer, 'chat', chat, inbound(post, FileSeed('a sieve'), 'C/seed'))
    with db.session() as s:
        [reply] = s.scalars(select(Comment).where(Comment.author == 'human:alice')).all()
        ack(s, [reply.id], System.ORCHESTRATOR)
        cursor = newest(s)
    edit = Edit('raise it')
    assert received(db, tracer, 'chat', chat, inbound(post, edit, 'C/9')) == Accepted(
        f'piloop updated comment#{reply.id}'
    )
    assert received(db, tracer, 'chat', chat, inbound(post, edit, 'C/9')) == Again(), 'the same text'
    bob = Person(name='bob')
    assert received(db, tracer, 'chat', chat, inbound(post, edit, 'C/9', bob)) == Declined(
        f'comment#{reply.id} is by human:alice: only its author changes it'
    )
    assert received(db, tracer, 'chat', chat, inbound(post, edit, 'C/9', None)) == Declined(
        'you are not in [plugins.notifier.chat] people: ask whoever runs piloop to add you'
    )
    assert received(db, tracer, 'chat', chat, inbound(post, edit, 'C/never')) == Foreign()
    assert received(db, tracer, 'chat', chat, inbound(post, edit, 'C/seed')) == Foreign(), 'a seed is kept as filed'
    with db.session() as s:
        edited = s.get_one(Comment, reply.id)
        assert (edited.body, edited.acked_at, edited.acked_by) == ('raise it', None, None)
        [(_, change)] = in_order(s, CommentEdited)
        assert (change.old, change.new) == ('aise it', 'raise it')
        found = news(s, cursor, campaign_id=campaign.id, party='orchestrator')
    assert [(n.comment.id, n.comment.body) for n in found.items if isinstance(n, CommentNews)] == [
        (reply.id, 'raise it')
    ], 'the orchestrator that read the old text gets the new one as news'
