"""The inbox: comments with a priority, scoped to a campaign through every target, until someone acknowledges them."""

import re
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest
from inline_snapshot import snapshot
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from piloop.actor import Actor, Agent, Orchestrating, Person, Service, System, party_of
from piloop.api.views import AckOutcome, News, Relation, thread_lines
from piloop.campaign import escalations, inbox
from piloop.campaign.escalations import DecisionWritten
from piloop.db import feed as db_feed
from piloop.db.events import Topic, emit, in_order
from piloop.db.models import Ack, AgentSession, AgentSessionState, Campaign, Comment, Event, utcnow
from piloop.db.session import Database
from piloop.db.triggers.changelog import CommentAcked, Commented, IdeaCreated
from piloop.ids import CampaignId, CampaignStatus, CommentId, CommentKind, TargetKind, TargetRef
from piloop.plugins.campaign_type.kit.backlog.ids import IDEA, IdeaStatus
from piloop.plugins.campaign_type.kit.backlog.tables import Idea
from piloop.plugins.campaign_type.kit.candidates.ids import CANDIDATE, CandidateId
from piloop.plugins.campaign_type.kit.evaluation.ids import MEASUREMENT, TrackName
from piloop.plugins.campaign_type.kit.evaluation.tables import Measurement
from piloop.plugins.campaign_type.kit.pool.scored import CandidateScored
from piloop.plugins.campaign_type.kit.runs.ids import IMPLEMENTER, RUN
from piloop.plugins.campaign_type.kit.runs.tables import Run
from piloop.plugins.escalation.contract import DECISION, ESCALATION, Choice, EscalationView
from piloop.settings import DaemonSettings
from tests.conftest import elsewhere
from tests.reports.test_history import idea
from tests.reports.test_overview import render


def bodies(rows: list[tuple[Comment, Campaign | None]]) -> list[str]:
    return [c.body for c, _ in rows]


def test_items_scope_to_the_campaign_through_every_target(db: Database, campaign: Campaign) -> None:
    with db.session() as s:
        other = Campaign(name='other', path='/other', config=campaign.config)
        s.add(other)
        s.flush()
        mine = idea(s, campaign, 'mine', IdeaStatus.OPEN)
        theirs = idea(s, other, 'theirs', IdeaStatus.OPEN)
        run = Run(campaign_id=mine.campaign_id, idea=mine, attempt=1, base_candidate_id=1, harness='h', model='m')
        s.add(run)
        s.add(
            AgentSession(kind=IMPLEMENTER, campaign_id=campaign.id, state=AgentSessionState.EXITED, ended_at=utcnow())
        )
        s.flush()
        s.add(Measurement(candidate_id=1, evaluator_id=1, metrics={'score': 1}))
        s.flush()
        escalation = dict(author='implementer', kind=ESCALATION, priority=1)
        s.add_all(
            [
                Comment(target=TargetRef(TargetKind.CAMPAIGN, campaign.id), body='on campaign', **escalation),
                Comment(target=TargetRef(IDEA, mine.id), body='on idea', **escalation),
                Comment(target=TargetRef(RUN, run.id), body='on run', **escalation),
                Comment(target=TargetRef(CANDIDATE, 1), body='on candidate', **escalation),
                Comment(target=TargetRef(MEASUREMENT, 1), body='on measurement', **escalation),
                Comment(target=TargetRef(TargetKind.AGENT_SESSION, 1), body='on implementer', **escalation),
                Comment(target=TargetRef(IDEA, theirs.id), body='other campaign', **escalation),
                Comment(target=TargetRef(TargetKind.CAMPAIGN, other.id), body='other campaign too', **escalation),
                Comment(target=TargetRef(RUN, run.id), author='w', kind=CommentKind.NOTE, body='no'),
                Comment(target=TargetRef(IDEA, mine.id), author='h', kind=DECISION, body='park'),
                Comment(target=TargetRef(TargetKind.COMMENT, 1), body='on a comment of the campaign', **escalation),
                Comment(target=TargetRef(TargetKind.COMMENT, 7), body='on a comment of the other', **escalation),
                Comment(target=TargetRef(TargetKind.COMMENT, 11), body='two levels deep', **escalation),
                Comment(target=TargetRef(TargetKind.COMMENT, 13), body="three levels deep: nobody's", **escalation),
            ]
        )
        s.commit()
        assert bodies(inbox.items(s, inbox.HUMAN, campaign_id=campaign.id)) == [
            'on campaign',
            'on idea',
            'on run',
            'on candidate',
            'on measurement',
            'on implementer',
            'on a comment of the campaign',
            'two levels deep',
        ], 'the forum (no priority) is not in it'
        assert bodies(inbox.items(s, inbox.HUMAN, campaign_id=other.id)) == [
            'other campaign',
            'other campaign too',
            'on a comment of the other',
        ]
        everything = inbox.items(s, inbox.HUMAN)
        assert len(everything) == 12, 'unfiltered, the comment three levels deep is in it too'
        assert [p.name if p else None for _, p in everything][-3:] == ['other', 'demo', None]
        s.commit()  # its connection goes back: ``render`` reads in a session of its own
        lines = [render(db, p).split('## Recent events\n\n')[1].split('\n\n')[0] for p in (campaign, other)]
        recent = [re.sub(r' \d+[smhd] ago', '', line).splitlines() for line in lines]
        assert recent == snapshot(
            [
                [
                    '- comment.escalation comment#11',
                    '- comment.decision comment#10',
                    '- comment.note comment#9',
                    '- comment.escalation comment#6',
                    '- comment.escalation comment#5',
                ],
                [
                    '- comment.escalation comment#12',
                    '- comment.escalation comment#8',
                    '- comment.escalation comment#7',
                    '- idea.created idea#2',
                ],
            ]
        ), "each campaign's escalation events and none of the other's (campaign_of serves events too)"


def test_most_urgent_first_narrowed_by_kind_until_acknowledged(db: Database, campaign: Campaign) -> None:
    on_campaign = dict(target=TargetRef(TargetKind.CAMPAIGN, campaign.id), author='pi')
    with db.session() as s:
        s.add_all(
            [
                Comment(kind=DECISION, priority=3, body='requeued idea#4', **on_campaign),
                Comment(kind=ESCALATION, priority=1, body='SSO expired', **on_campaign),
                Comment(kind=DECISION, priority=0, body='lowered the floor', **on_campaign),
                Comment(kind=CommentKind.NOTE, priority=3, body='half the sweeps add nothing', **on_campaign),
            ]
        )
        s.commit()
        assert bodies(inbox.items(s, inbox.HUMAN)) == [
            'lowered the floor',
            'SSO expired',
            'requeued idea#4',
            'half the sweeps add nothing',
        ]
        assert bodies(inbox.items(s, inbox.HUMAN, kinds=[DECISION])) == ['lowered the floor', 'requeued idea#4']
        assert [
            r.line() for r in inbox.ack(s, [CommentId(3), CommentId(1), CommentId(3), CommentId(99)], System.HUMAN)
        ] == [
            'comment#3 acknowledged',
            'comment#1 acknowledged',
            'comment#3 was acknowledged already, by human',
            'comment#99 is not in the inbox',
        ]
        assert bodies(inbox.items(s, inbox.HUMAN)) == ['SSO expired', 'half the sweeps add nothing']
        assert bodies(inbox.items(s, inbox.HUMAN, acked=True)) == [
            'lowered the floor',
            'SSO expired',
            'requeued idea#4',
            'half the sweeps add nothing',
        ]
        acked = in_order(s, CommentAcked)
        assert [(e.target_id, a.party, a.acked_by) for e, a in acked] == [(3, 'human', 'human'), (1, 'human', 'human')]


def test_an_author_never_has_its_own_comment_waiting_nor_acknowledges_it(db: Database, campaign: Campaign) -> None:
    ana = Person(name='ana')
    with db.session() as s:
        sent = inbox.message(s, campaign.id, 'what next?', author=ana, priority=1)
        assert [c.id for c, _ in inbox.waiting_for(s, ana.party)] == [], 'not for its author'
        assert [c.id for c, _ in inbox.waiting_for(s, System.ORCHESTRATOR.party)] == [sent.id]
        assert [r.line() for r in inbox.ack(s, [sent.id], ana)] == [
            'comment#1 is yours: only its recipients acknowledge it'
        ]
        assert s.get_one(Comment, sent.id).acks == []
        assert [r.outcome for r in inbox.ack(s, [sent.id], System.ORCHESTRATOR)] == [AckOutcome.ACKED]
        [own] = inbox.ack(s, [sent.id], ana)
        assert (own.outcome, own.acked_by) == (AckOutcome.OWN, None), 'yours, whoever acknowledged it'


def test_every_agent_of_a_role_is_one_party_and_so_is_the_outside_orchestrator_for_what_waits_and_what_is_news(
    db: Database, campaign: Campaign
) -> None:
    earlier, later = Agent(role='auditor', session=41), Agent(role='auditor', session=42)
    outside = (System.ORCHESTRATOR, Orchestrating(by=Person(name='ana')))
    for author, same in ((earlier, (later,)), (outside[1], outside)):
        with db.session() as s:
            cursor = db_feed.newest(s)
            decided = Comment(
                author=author.signature,
                kind=DECISION,
                priority=3,
                body='parked idea#1',
                target=TargetRef(TargetKind.CAMPAIGN, campaign.id),
            )
            s.add(decided)
            s.flush()
            emit(s, scored('mine'), TargetRef(CANDIDATE, 1), author)
            emit(s, scored('the daemon'), TargetRef(CANDIDATE, 1), System.DAEMON)
            s.commit()
            for me in same:
                assert decided.id not in [c.id for c, _ in inbox.waiting_for(s, me.party)], f'{me} wrote it'
                assert news_lines(s, cursor, me=me)[1] == ['candidate#1: the daemon'], 'its own event is no news'
                assert [r.outcome for r in inbox.ack(s, [decided.id], me)] == [AckOutcome.OWN]
            assert decided.id in [c.id for c, _ in inbox.waiting_for(s, Agent(role='implementer', session=41).party)]
    with db.session() as s:
        assert [c.id for c, _ in inbox.waiting_for(s, System.ORCHESTRATOR.party)] == [1], "an agent's, for the others"
    assert party_of('not an actor') == 'not an actor'
    assert Service(name='ci').party == Service(name='ci').signature, 'a service writes for itself'
    assert party_of('orchestrator via human:ana') == party_of('orchestrator') == 'orchestrator'
    assert party_of('auditor#7') == 'auditor'


def test_thread_follows_citations_both_ways_and_the_replies_not_the_rest_of_the_target(
    db: Database, campaign: Campaign
) -> None:
    with db.session() as s:
        on_campaign = dict(target=TargetRef(TargetKind.CAMPAIGN, campaign.id))
        s.add_all(
            [
                Comment(author='research#2', kind=CommentKind.NOTE, body='a sweep', **on_campaign),
                Comment(author='research#2', kind=ESCALATION, priority=1, body='floor exact?', **on_campaign),
                Comment(target=TargetRef(TargetKind.COMMENT, 2), author='pi', body='looking'),
                Comment(author='pi', kind=DECISION, priority=0, body='floor 2687\nRe: comment#2', **on_campaign),
                Comment(author='pi', kind=DECISION, priority=3, body='see comment#20, not #2', **on_campaign),
                Comment(target=TargetRef(IDEA, 4), author='pi', body='as comment#4 decided, comment#2.'),
            ]
        )
        s.commit()
        escalation, decision = s.get_one(Comment, 2), s.get_one(Comment, 4)
        assert [(c.body, r) for c, r in inbox.thread(s, escalation)] == [
            ('looking', Relation.REPLY),
            ('floor 2687\nRe: comment#2', Relation.CITES),
            ('as comment#4 decided, comment#2.', Relation.CITES),
        ]
        assert [(c.id, r) for c, r in inbox.thread(s, decision)] == [
            (2, Relation.CITED_BY),
            (6, Relation.CITES),
        ], 'what it answers, and who cites it'


def test_each_comment_of_a_thread_has_the_thread_of_its_root(db: Database, campaign: Campaign) -> None:
    on_campaign = TargetRef(TargetKind.CAMPAIGN, campaign.id)
    with db.session() as s:
        s.add(Comment(target=on_campaign, author='research#2', kind=CommentKind.NOTE, body='the login expires'))
        root = Comment(
            target=on_campaign, author='implementer#1', kind=ESCALATION, priority=1, body='SSO, see comment#1'
        )
        s.add(root)
        s.commit()
        first = inbox.reply(s, root.id, 'logged in, Re: comment#2', author=System.ORCHESTRATOR, priority=3)
        second = inbox.reply(s, first.id, 'thanks', author=System.HUMAN, priority=3)
        threads = [inbox.thread_of(s, c) for c in (root, first, second)]
        assert threads[1:] == threads[:-1], 'every member answers the same thread'
        assert threads[0].comment.id == root.id
        assert [(c.id, c.relation, c.in_reply_to) for c in threads[0].linked] == [
            (first.id, Relation.REPLY, root.id),
            (second.id, Relation.REPLY, first.id),
            (1, Relation.CITED_BY, None),
        ], 'a reply that cites its root is a reply'
        s.add_all(
            [
                Comment(target=TargetRef(TargetKind.COMMENT, root.id), author='pi', body='an old reply\nsays nothing'),
                Comment(target=TargetRef(IDEA, 1), author='pi', body='as comment#2 said'),
            ]
        )
        s.commit()
        title, lines = thread_lines(inbox.thread_of(s, second), datetime.now(UTC))
        assert [title.split(', ')[0], *(re.sub(r'\d+s ago', '0s ago', line) for line in lines)] == snapshot(
            [
                'comment#2 escalation on campaign#1 by implementer#1',
                'SSO, see comment#1',
                '',
                '- comment#3 decision by orchestrator, 0s ago, answers comment#2: logged in, Re: comment#2',
                '- comment#4 decision by human, 0s ago, answers comment#3: thanks',
                '- comment#5 note by pi, 0s ago, a reply: an old reply',
                '  says nothing',
                '- comment#1 note by research#2, 0s ago, cited by it: the login expires',
                '- comment#6 note by pi, 0s ago, cites it: as comment#2 said',
            ]
        )


def test_news_is_what_happened_since_the_cursor_that_the_orchestrator_did_not_write_or_see(
    db: Database, campaign: Campaign
) -> None:
    def on(p: Campaign) -> dict[str, object]:
        return dict(target=TargetRef(TargetKind.CAMPAIGN, p.id))

    with db.session() as s:
        other = Campaign(name='other', path='/other', config=campaign.config)
        s.add(other)
        s.add(Comment(author='implementer#1', kind=ESCALATION, priority=1, body='before', **on(campaign)))
        s.commit()
        cursor = s.scalar(select(func.max(Event.id)))
        assert cursor is not None
        s.add_all(
            [
                Comment(author='implementer#1', kind=ESCALATION, priority=1, body='SSO\nlog in', **on(campaign)),
                Comment(author='orchestrator', kind=DECISION, priority=3, body='mine', **on(campaign)),
                Comment(author='research#2', kind=CommentKind.NOTE, body='the forum', **on(campaign)),
                Comment(
                    author='human',
                    kind=DECISION,
                    priority=2,
                    body='seen',
                    acks=[Ack(party='orchestrator', acked_by='orchestrator')],
                    **on(campaign),
                ),
                Comment(author='implementer#5', kind=ESCALATION, priority=0, body='theirs', **on(other)),
            ]
        )
        ideas = [
            Idea(
                track='score',
                campaign_id=p.id,
                title=f'hunch {n}',
                hypothesis='h',
                status=status,
                created_by=by,
                provenance='run#1',
            )
            for n, (p, status, by) in enumerate(
                [
                    (campaign, IdeaStatus.OPEN, 'implementer#3'),
                    (other, IdeaStatus.OPEN, 'implementer#1'),
                ]
            )
        ]
        s.add_all(ideas)  # an idea on the board is no news: the lead reads the board
        s.flush()
        candidate = TargetRef(CANDIDATE, 1)
        emit(s, scored('score 2 over 1 eval: the best of the score track now, rank 1 of 1'), candidate, System.DAEMON)
        row = s.get_one(Campaign, campaign.id)
        row.status, row.stopped_reason = CampaignStatus.STOPPED, 'max_tokens 100 reached'
        s.commit()
        last, lines = news_lines(s, cursor, me=System.ORCHESTRATOR)
        assert lines == snapshot(
            [
                'comment#6 escalation p0 on campaign#2 (other) by implementer#5: theirs',
                'comment#2 escalation p1 on campaign#1 (demo) by implementer#1: SSO',
                'candidate#1: score 2 over 1 eval: the best of the score track now, rank 1 of 1',
                'campaign demo stopped: max_tokens 100 reached',
            ]
        )
        assert news_lines(s, last, me=System.ORCHESTRATOR) == (last, []), 'the cursor moved past all of it'
        assert news_lines(s, cursor, campaign_id=other.id, me=System.ORCHESTRATOR)[1] == snapshot(
            [
                'comment#6 escalation p0 on campaign#2 (other) by implementer#5: theirs',
            ]
        )


def test_news_reads_only_what_wakes_a_page_at_a_time_until_a_page_has_a_line(
    db: Database, campaign: Campaign, monkeypatch: pytest.MonkeyPatch
) -> None:
    paged = inbox.settings.model_copy(update={'daemon': DaemonSettings(inbox_page=1)})
    monkeypatch.setattr(inbox, 'settings', paged)
    with db.session() as s:
        cursor = s.scalar(select(func.max(Event.id))) or 0
        on_campaign = TargetRef(TargetKind.CAMPAIGN, campaign.id)
        for n in range(3):
            seen = Ack(party='orchestrator', acked_by='orchestrator')
            s.add(Comment(target=on_campaign, author='implementer#1', priority=1, body=f'acked {n}', acks=[seen]))
        emit(s, IdeaCreated(), TargetRef(IDEA, 99), System.HUMAN)  # a deleted idea: no line
        s.add(Comment(target=on_campaign, author='implementer#1', priority=1, body='open'))
        s.add(Comment(target=on_campaign, author='implementer#1', body='a note: an event, but not of the inbox'))
        s.commit()
        last, lines = news_lines(s, cursor, me=System.ORCHESTRATOR)
        assert lines == ['comment#4 note p1 on campaign#1 (demo) by implementer#1: open']
        newest = db_feed.newest(s)
        assert last < newest and news_lines(s, last, me=System.ORCHESTRATOR) == (newest, []), 'it passes the note'


def test_a_reply_answers_the_root_of_the_thread_and_wakes_the_orchestrator_but_not_its_author(
    db: Database, campaign: Campaign
) -> None:
    with db.session() as s:
        item = Comment(
            target=TargetRef(CANDIDATE, 1),
            author='orchestrator',
            kind=DECISION,
            priority=1,
            body='Keep #1.',
        )
        s.add(item)
        s.flush()
        follow_up = Comment(
            target=TargetRef(TargetKind.COMMENT, item.id), author='orchestrator', priority=3, body='Details.'
        )
        s.add(follow_up)
        s.commit()
        cursor = s.scalar(select(func.max(Event.id))) or 0
        answer = inbox.reply(s, follow_up.id, '  Revert it: the floor moved.\n', author=System.HUMAN, priority=2)
        assert (
            answer.target_kind,
            answer.target_id,
            answer.in_reply_to,
            answer.kind,
            answer.priority,
            answer.body,
        ) == (
            TargetKind.COMMENT,
            item.id,
            follow_up.id,
            DECISION,
            2,
            'Revert it: the floor moved.',
        ), 'a reply to a reply goes to the root, one level from a campaign, and says what it answers'
        assert [p for e, p in in_order(s, Commented) if e.target_id == answer.id] == [
            DecisionWritten(target_kind=TargetKind.COMMENT, target_id=item.id, in_reply_to=follow_up.id)
        ]
        assert [c.id for c, _ in inbox.thread(s, item)] == [follow_up.id, answer.id]
        assert [c.id for c, _ in inbox.items(s, inbox.HUMAN, campaign_id=campaign.id)] == [item.id, answer.id], (
            'the follow-up that the human answered waits for no party'
        )
        assert news_lines(s, cursor, campaign_id=campaign.id, me=System.ORCHESTRATOR)[1] == [
            'comment#3 decision p2 on comment#1 (demo) by human: Revert it: the floor moved.'
        ]
        assert news_lines(s, cursor, me=System.HUMAN)[1] == [], 'not news to whoever wrote it'


def test_an_escalation_left_unacknowledged_comes_back_with_its_age_on_each_wait_until_acked_or_answered(
    db: Database, campaign: Campaign
) -> None:
    """An orchestrator that moved past an escalation sees it again, on the next wait that returns for anything else."""
    now = utcnow()
    on_campaign = TargetRef(TargetKind.CAMPAIGN, campaign.id)
    with db.session() as s:
        start = db_feed.newest(s)
        for author, minutes in (('implementer#3', 120), ('research#4', 5), ('orchestrator', 60)):
            s.add(
                Comment(
                    target=on_campaign,
                    author=author,
                    kind=ESCALATION,
                    priority=1,
                    body='which floor?',
                    created_at=now - timedelta(minutes=minutes),
                )
            )
        s.commit()
        first = news(s, start, campaign_id=campaign.id, party=System.ORCHESTRATOR.party)
        assert [i.text() for i in first.items] == [
            'comment#1 escalation p1 on campaign#1 (demo) by implementer#3: which floor?',
            'comment#2 escalation p1 on campaign#1 (demo) by research#4: which floor?',
        ]
        assert first.reminder(now) is None, 'news already'

        def score() -> News:
            emit(s, scored('score 2 over 1 eval'), TargetRef(CANDIDATE, 1), System.DAEMON)
            s.commit()
            return news(s, db_feed.newest(s) - 1, campaign_id=campaign.id, party=System.ORCHESTRATOR.party)

        s.add(Comment(target=TargetRef(TargetKind.COMMENT, 1), author='implementer#3', body='still stuck'))
        assert score().reminder(now) == '2 escalations wait unacknowledged: comment#1 (2h old), comment#2 (5m old)', (
            'its own follow-up answers nothing'
        )
        inbox.ack(s, [CommentId(1)], System.ORCHESTRATOR)
        s.commit()
        quiet = news(s, db_feed.newest(s), campaign_id=campaign.id, party=System.ORCHESTRATOR.party)
        assert (quiet.items, quiet.reminder(now)) == ([], '1 escalation waits unacknowledged: comment#2 (5m old)'), (
            'a timeout says it too, but it ends no wait'
        )
        inbox.reply(s, CommentId(2), 'the old one', author=System.ORCHESTRATOR, priority=2)
        assert score().reminder(now) is None, 'answered: the human acknowledges the answer'


def scored(told: str) -> CandidateScored:
    """The daemon's score of the baseline, the best, as its worker hears ``told``."""
    return CandidateScored(track=TrackName('score'), score=2.0, measured=1, best=CandidateId(1), rank=1, told=told)


def news(s: Session, after: int, *, campaign_id: CampaignId | None = None, party: str) -> News:
    """The news after ``after``, with the escalations, as ``GET /v1/inbox/news`` answers it without a wait."""
    found, cursor = inbox.news_after(s, after, campaign_id=campaign_id, party=party)
    return News(next=cursor, items=found, escalations=inbox.escalations(s, party, campaign_id=campaign_id, shown=found))


def news_lines(s: Session, after: int, *, campaign_id: CampaignId | None = None, me: Actor) -> tuple[int, list[str]]:
    """The news after ``after``, as ``piloop inbox wait`` prints it, and the next cursor."""
    found = news(s, after, campaign_id=campaign_id, party=me.party)
    return found.next, [item.text() for item in found.items]


def test_an_item_that_lands_while_news_reads_is_news_now_or_after_its_cursor(
    db: Database, campaign: Campaign, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each statement of a session reads the latest commit, so the items and the page of events can disagree."""
    after = db_feed.after

    def then_escalate(s: Session, cursor: int, topics: Sequence[Topic] = (), *, limit: int) -> db_feed.Page:
        page = after(s, cursor, topics, limit=limit)
        if not page.ids:
            with elsewhere(db) as second, second.session() as other:
                other.add(
                    Comment(
                        target=TargetRef(TargetKind.CAMPAIGN, campaign.id),
                        author='implementer#2',
                        kind=ESCALATION,
                        priority=1,
                        body='SSO expired',
                    )
                )
                other.commit()
        return page

    monkeypatch.setattr(db_feed, 'after', then_escalate)
    with db.session() as s:
        start = db_feed.newest(s)
        cursor, now = news_lines(s, start, campaign_id=campaign.id, me=System.ORCHESTRATOR)
        _, then = news_lines(s, cursor, campaign_id=campaign.id, me=System.ORCHESTRATOR)
    assert [*now, *then] == ['comment#1 escalation p1 on campaign#1 (demo) by implementer#2: SSO expired']


def test_two_acks_that_race_acknowledge_once_and_the_second_learns_who_did(db: Database, campaign: Campaign) -> None:
    """B26: ``ack`` read the item, then wrote it, so two concurrent acks both returned ACKED and the first was lost."""
    with db.session() as s:
        item = Comment(
            target=TargetRef(TargetKind.CAMPAIGN, campaign.id), author='implementer#3', priority=1, body='look'
        )
        s.add(item)
        s.commit()
    ana, bob = Person(name='ana'), Person(name='bob')
    other = Database(str(db.engine.url), actor=bob)  # a second process: bob's CLI
    with db.session() as first, other.session() as second:
        assert first.get_one(Comment, item.id).acks == second.get_one(Comment, item.id).acks == [], 'both read it'
        [mine] = inbox.ack(first, [item.id], ana)
        first.commit()
        [theirs] = inbox.ack(second, [item.id], bob)
        second.commit()
    other.close()
    assert (mine.outcome, mine.acked_by, theirs.outcome, theirs.acked_by) == (
        AckOutcome.ACKED,
        ana.signature,
        AckOutcome.ALREADY,
        ana.signature,
    )
    with db.session() as s:
        assert [(a.party, a.acked_by) for a in s.get_one(Comment, item.id).acks] == [('human', ana.signature)]
        assert [a.acked_by for _, a in in_order(s, CommentAcked)] == [ana.signature], 'one event'


def test_a_person_s_ack_leaves_the_escalation_waiting_for_the_orchestrator_and_an_answer_ends_it_for_both(
    db: Database, campaign: Campaign
) -> None:
    """B27: one global ack hid an unanswered escalation from the orchestrator. Each party acknowledges for itself."""
    with db.session() as s:
        asked = Comment(
            target=TargetRef(TargetKind.CAMPAIGN, campaign.id),
            author='implementer#3',
            kind=ESCALATION,
            priority=1,
            body='which floor?',
        )
        s.add(asked)
        s.commit()
        assert [r.outcome for r in inbox.ack(s, [asked.id], System.HUMAN)] == [AckOutcome.ACKED]
        s.commit()

        def waits(party: str) -> list[CommentId]:
            return [c.id for c, _ in inbox.waiting_for(s, party, campaign_id=campaign.id)]

        assert (waits(System.HUMAN.party), waits(System.ORCHESTRATOR.party)) == ([], [asked.id])
        later = news(s, db_feed.newest(s), campaign_id=campaign.id, party='orchestrator')
        assert [c.id for c in later.escalations] == [asked.id], 'still unanswered: it comes with each wait'
        decision = inbox.reply(s, asked.id, 'the old one', author=System.ORCHESTRATOR, priority=2)
        assert (waits(System.HUMAN.party), waits(System.ORCHESTRATOR.party)) == ([decision.id], []), (
            'answered: the decision replaces it in the human inbox'
        )


def test_an_escalation_takes_one_answer_whoever_writes_the_second(db: Database, campaign: Campaign) -> None:
    """B28: ``piloop inbox choose`` recorded a second, conflicting decision; only the Slack path checked."""
    view = EscalationView(
        headline='Go on?',
        why='w',
        choices=(Choice(id='A', label='a', result='ra'), Choice(id='B', label='b', result='rb')),
        details='d',
    )
    with db.session() as s:
        item = escalations.escalate_once(s, TargetRef(TargetKind.CAMPAIGN, campaign.id), view, priority=0)
        assert item is not None
        s.commit()
    with db.session() as s:
        escalations.choose(s, item.id, 'A', author=System.ORCHESTRATOR)
        s.commit()
    with db.session() as s, pytest.raises(inbox.Answered, match=rf'^comment#{item.id} was answered already, by'):
        escalations.choose(s, item.id, 'B', author=System.HUMAN)
    with db.session() as s, pytest.raises(inbox.Answered, match='by orchestrator: Chose A'):
        inbox.reply(s, item.id, 'B, after all', author=System.HUMAN, priority=2)
    with db.session() as s:
        decisions = s.scalars(select(Comment).where(Comment.in_reply_to == item.id, Comment.kind == DECISION)).all()
        assert [d.author for d in decisions] == ['orchestrator']
