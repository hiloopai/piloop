"""Tests for the history that an agent reads in its brief and that ``piloop history`` prints."""

import re
import sqlite3
import time
from datetime import timedelta
from importlib.resources import files
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.event import listen, remove
from sqlalchemy.orm import Session

from piloop.actor import Agent
from piloop.config import CampaignConfig
from piloop.db.events import emit
from piloop.db.models import AgentSession, Campaign, Comment, utcnow
from piloop.db.session import Database
from piloop.db.triggers import DAEMON_ACTOR
from piloop.ids import AgentSessionId, CommentKind, Revision, TargetKind, TargetRef
from piloop.plugins.campaign_type.kit.backlog.ideas import Yield, yields
from piloop.plugins.campaign_type.kit.backlog.ids import IDEA, IdeaStatus, LinkKind
from piloop.plugins.campaign_type.kit.backlog.tables import Idea, IdeaLink
from piloop.plugins.campaign_type.kit.candidates.ids import CANDIDATE, Origin
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.evaluation.ids import RungName
from piloop.plugins.campaign_type.kit.evaluation.submissions import ideas_of, record, waiting
from piloop.plugins.campaign_type.kit.evaluation.tables import Measurement
from piloop.plugins.campaign_type.kit.pool.pool import Pool, track_of
from piloop.plugins.campaign_type.kit.pool.scored import CandidateScored, record_scored
from piloop.plugins.campaign_type.kit.runs.ids import IMPLEMENTER, RUN, RunEnd, RunStatus
from piloop.plugins.campaign_type.kit.runs.tables import Run, RunClaim, Submission, working_on
from piloop.plugins.campaign_type.optimization.history import (
    FIRST_LINE_CHARS,
    campaign_notes,
    comment_line,
    history,
    newest,
)
from piloop.plugins.campaign_type.optimization.section.history import History, _ranked
from piloop.plugins.campaign_type.optimization.section.pack import Pack
from piloop.plugins.section.contract import Briefing, Rendering, markdown
from piloop.text import CELL_CHARS, cell, cost
from tests.conftest import cas_of, has_lines, spent

SHOWN = Pack()
"""The research pack's default bounds."""


def idea(
    s: Session,
    campaign: Campaign,
    title: str,
    status: IdeaStatus = IdeaStatus.DONE,
    hypothesis: str | None = None,
    *,
    after: tuple[int, ...] = (),
    **fields: object,
) -> Idea:
    row = Idea(
        track='score',
        campaign_id=campaign.id,
        title=title,
        hypothesis=hypothesis or f'{title} helps',
        status=status,
        links=[IdeaLink(target_id=t, kind=LinkKind.AFTER) for t in after],
        **{'created_by': 't', **fields},
    )
    s.add(row)
    s.flush()
    return row


def scored(
    s: Session,
    row: Idea,
    base: int,
    *outcomes: tuple[dict[str, float], str | None],
    tokens: int | None = None,
) -> Candidate:
    """Add one run of ``row`` from candidate ``base`` with one candidate per measurement, each scored. Return the last."""
    run = Run(
        campaign_id=row.campaign_id,
        base_candidate_id=base,
        harness='h',
        model='m',
        claims=[RunClaim(idea=row, attempt=len(row.claims) + 1)],
    )
    run.status, run.end = RunStatus.SUCCEEDED, RunEnd.FINISHED
    s.add(run)
    s.flush()
    usage = None if tokens is None else spent(tokens, 3)
    s.add(AgentSession(kind=IMPLEMENTER, campaign_id=row.campaign_id, subject=TargetRef(RUN, run.id), usage=usage))
    evaluator_id = row.campaign.evaluator_id
    assert evaluator_id is not None
    parent = base
    for n, (metrics, error) in enumerate(outcomes):
        c = Candidate(
            campaign_id=row.campaign_id,
            run_id=run.id,
            idea_id=row.id,
            parent_id=parent,
            tree_sha=f'{row.id}-{run.id}-{n}',
            revision=f'{row.id}{run.id}{n}0000deadbeef',
            summary=f'{row.title} commit' if error is None else None,
        )
        s.add(c)
        s.flush()
        s.add(Measurement(candidate_id=c.id, evaluator_id=evaluator_id, metrics=metrics, error=error))
        s.flush()
        record_scored(s, Pool.of(s, row.campaign, track_of(row.campaign.config, c)), c.id)
        parent = c.id
    return c


def comment(s: Session, kind: TargetKind, target: int, body: str, author: str = 'implementer#1') -> None:
    s.add(Comment(target=TargetRef(kind, target), author=author, kind=CommentKind.NOTE, body=body))


def render(db: Database, campaign: Campaign) -> str:
    """Render the history with the Rich column padding collapsed, so that assertions match the rows."""
    return '\n'.join(compact(line) for line in raw(db, campaign).splitlines()) + '\n'


def full(s: Session, campaign: Campaign, cfg: CampaignConfig) -> str:
    """The history as the research pack shows it, with its default bounds."""
    notes = campaign_notes(s, campaign, SHOWN.campaign_findings)
    return history(s, campaign, cfg, ideas=newest(s, campaign, SHOWN.ideas), notes=notes)


def raw(db: Database, campaign: Campaign) -> str:
    with db.session() as s:
        return full(s, s.get_one(Campaign, campaign.id), CampaignConfig.model_validate(campaign.config))


def compact(line: str) -> str:
    if not line.startswith('|'):
        return line
    if set(line) <= {'|', '-', ' '}:
        return '|' + '---|' * (line.count('|') - 1)
    return '| ' + ' | '.join(c.strip() for c in line.strip('|').split('|')) + ' |'


def test_history_lists_every_idea_with_its_outcome_the_lineage_and_the_findings(
    db: Database, campaign: Campaign
) -> None:
    with db.session() as s:
        s.get_one(Candidate, 1).revision = Revision('ba5e11ae0000')
        kept = idea(s, campaign, 'more lines', priority=1, created_by='research#9')
        e2 = scored(
            s,
            kept,
            1,
            ({'score': 3, 'size': 12}, None),
            tokens=234_444,
        )
        comment(s, IDEA, kept.id, 'appending works | keep appending\nsecond line is not shown')
        worse = idea(s, campaign, 'fewer lines', created_by='implementer#4')
        scored(s, worse, e2.id, ({'score': 2, 'size': 8}, None))
        crashed = idea(s, campaign, 'delete solve.txt', IdeaStatus.DROPPED, priority=3)
        scored(s, crashed, e2.id, ({}, 'no such file'))
        comment(s, IDEA, crashed.id, 'first attempt: evaluate needs the file')
        comment(s, IDEA, crashed.id, 'second attempt agrees; rejected', author='auditor#2')
        mixed = idea(s, campaign, 'two commits', after=(kept.id,))
        scored(s, mixed, e2.id, ({}, 'syntax'), ({'score': 4, 'size': 16}, None))
        idea(s, campaign, 'a' * 200, IdeaStatus.OPEN, 'h' * 200, priority=0, after=(crashed.id, kept.id))
        idea(s, campaign, 'human should look', IdeaStatus.OPEN, 'the metric | is wrong\nsecond line')
        idea(s, campaign, 'fewer but longer lines', IdeaStatus.OPEN, after=(mixed.id, kept.id))
        idea(s, campaign, 'no lines at all', IdeaStatus.OPEN)
        comment(
            s, TargetKind.CAMPAIGN, campaign.id, 'batch 1: extend appending\nrejected: deleting', author='research#1'
        )
        comment(s, TargetKind.CAMPAIGN, campaign.id, 'idle: best is #2; try longer lines', author='auditor#2')
        comment(s, TargetKind.CAMPAIGN, campaign.id, 'batch 2: nothing new worth proposing', author='research#3')
        comment(s, TargetKind.CAMPAIGN, campaign.id, 'the cache keep holds on fresh data', author='reviewer#7')
        comment(s, TargetKind.CAMPAIGN, campaign.id, 'digest: on track', author='orchestrator')
        s.commit()
    text = render(db, campaign)
    has_lines(
        text,
        '### History: ideas (8 of 8)',
        '| 1 | done | research#9 | — | 1 | more lines | 3 (+2 vs candidate#1) (candidate#2) |',
        '| 2 | done | implementer#4 | — | 2 | fewer lines | 2 (-1 vs candidate#2) (candidate#3) |',
        '| 3 | dropped | t | — | 3 | delete solve.txt | crash (candidate#4) |',
        '| 4 | done | t | after idea#1 | 2 | two commits | 4 (+1 vs candidate#2) (candidate#6) |',
        f'| 5 | open | t | after idea#1, idea#3 | 0 | {"a" * 99}… | — |',
        '| 6 | open | t | — | 2 | human should look | — |',
        '| 7 | open | t | after idea#1, idea#4 | 2 | fewer but longer lines | — |',
        '| 8 | open | t | — | 2 | no lines at all | — |',
        '### History: new bests',
        '| #2 | #1 | 3 | +2 | idea#1 (research#9) | 1100000 | 234k | more lines commit |',
        '| #6 | #5 | 4 | — | idea#4 (t) | 4410000 | — | two commits commit |',
        '### History: latest notes (newest first)',
        '- comment#7 reviewer#7 on campaign#1: the cache keep holds on fresh data',
        '- comment#6 research#3 on campaign#1: batch 2: nothing new worth proposing',
        '- comment#5 auditor#2 on campaign#1: idle: best is #2; try longer lines',
        '- comment#4 research#1 on campaign#1: batch 1: extend appending (+1 lines)',
    )
    assert 'digest: on track' not in text, "the orchestrator's findings are in the pack's digests"


def test_history_is_bounded_and_counts_what_it_omits(db: Database, campaign: Campaign) -> None:
    with db.session() as s:
        for n in range(SHOWN.ideas + 3):
            idea(s, campaign, f'idea {n}', IdeaStatus.OPEN)
        for n in range(SHOWN.campaign_findings + 2):
            comment(s, TargetKind.CAMPAIGN, campaign.id, f'finding {n}', author='research#1')
        s.commit()
    rendered = render(db, campaign)
    rows = [line for line in rendered.splitlines() if line.startswith('| ') and line.split(' | ')[0][2:].isdigit()]
    assert rendered.startswith(f'### History: ideas ({SHOWN.ideas} of {SHOWN.ideas + 3}; 3 older omitted)')
    assert [r.split(' | ')[0] for r in rows[:2]] == ['| 4', '| 5'], 'the newest ideas are kept, the oldest dropped'
    assert len(rows) == SHOWN.ideas
    findings = [line for line in rendered.splitlines() if ' research#1 on campaign#1: finding' in line]
    assert findings[0].endswith(f' research#1 on campaign#1: finding {SHOWN.campaign_findings + 1}')
    assert len(findings) == SHOWN.campaign_findings


def test_history_worst_case_stays_under_the_documented_size(db: Database, campaign: Campaign) -> None:
    with db.session() as s:
        previous: Idea | None = None
        for n in range(SHOWN.ideas):
            links = (previous.id,) if previous is not None else ()
            row = idea(s, campaign, f'{n} ' + 't' * 200, IdeaStatus.DROPPED, 'h' * 200, after=links)
            scored(s, row, 1, ({'score': 0.123456, 'size': 1}, None))
            comment(s, IDEA, row.id, 'f' * 200)
            previous = row
        for _ in range(SHOWN.campaign_findings):
            comment(s, TargetKind.CAMPAIGN, campaign.id, '\n'.join(['r' * 79] * 30), author='research#1')
        s.commit()
    rendered = render(db, campaign)
    assert 'after idea#58' in rendered, 'every link: a dropped prerequisite frees its dependent'
    assert len(raw(db, campaign)) < 64_000
    assert len(rendered) < 18_000


def test_history_of_an_idea_that_builds_on_an_unscored_candidate(db: Database, campaign: Campaign) -> None:
    with db.session() as s:
        unscored = Candidate(campaign_id=campaign.id, tree_sha='y', revision='y', origin=Origin.RECOVERED)
        s.add(unscored)
        s.flush()
        row = idea(s, campaign, 'from an unscored base')
        scored(s, row, unscored.id, ({'score': 5, 'size': 1}, None))
        s.commit()
    rendered = render(db, campaign)
    assert '| 1 | done | t | — | 2 | from an unscored base | 5 (candidate#3) |' in rendered, (
        'no delta without a base score'
    )
    assert '| #3 | #2 | 5 | — | idea#1 (t) |' in rendered, 'no gain over a parent without a score'


def test_cell_is_one_bounded_line() -> None:
    assert cell('  \n') == '—'
    assert cell('a | b\nc') == 'a / b'
    assert len(cell('x' * 500)) == CELL_CHARS
    assert len(cell('x' * 500, 40)) == 40


def test_a_comment_is_its_id_and_first_line_and_counts_the_rest() -> None:
    def line(body: str) -> str:
        return comment_line(Comment(id=7, body=body), 'research#1').plain

    assert line('  \n') == '- comment#7 research#1: —'
    assert line('done\nrejected: a\nrejected: b') == '- comment#7 research#1: done (+2 lines)'
    assert line('x' * 500) == f'- comment#7 research#1: {"x" * (FIRST_LINE_CHARS - 1)}…'


def test_an_implementers_history_shows_the_new_bests_then_the_newest_within_its_budget(
    db: Database, campaign: Campaign
) -> None:
    with db.session() as s:
        idea(s, campaign, 'older')
        kept = idea(s, campaign, 'kept')
        scored(s, kept, 1, ({'score': 1.5, 'size': 1}, None))
        for n in range(30):
            idea(s, campaign, f'filler {n}')
        mine = idea(s, campaign, 'mine', IdeaStatus.CLAIMED)
        scored(s, mine, 1, ({'score': 0.1, 'size': 1}, None))
        reader = s.scalars(select(AgentSession.id).where(working_on(mine.claims[0].run_id))).one()
        s.commit()
        p = s.get_one(Campaign, campaign.id)
        section = History(budget=500, findings=0)
        text = markdown(
            [section],
            Rendering(
                s,
                utcnow(),
                p,
                Path('/no/workspace'),
                cas_of(s),
                brief=Briefing(Agent(role=IMPLEMENTER, session=reader), 'x'),
            ),
        )
        everything = markdown([History(budget=100_000)], Rendering(s, utcnow(), p, Path('/no/workspace'), cas_of(s)))
    shown = [int(line.split('|')[1]) for line in text.splitlines() if re.match(r'\| \d+ ', line)]
    assert shown[:1] == [kept.id] and len(shown) < 31, 'the ideas that made a new best first, then what fits'
    assert shown[1:] == list(range(mine.id - len(shown) + 1, mine.id)), 'then the newest, oldest first'
    assert f'of 33; besides idea#{mine.id}, those that made a new best, then the newest)' in text
    assert cost(text.split('\n', 2)[2].splitlines()) <= section.budget
    assert '| mine ' in everything and '(33 of 33)' in everything, 'a reader without a run sees every idea'


def test_an_idea_whose_candidate_stopped_at_a_rung_says_where_and_why(db: Database, campaign: Campaign) -> None:
    with db.session() as s:
        c = scored(s, idea(s, campaign, 'a cheaper screen'), 1, ({'score': 0.5}, None))
        [m] = c.measurements
        m.rung, m.stopped = RungName('screen'), 'score 0.5 vs 1 (-0.5; margin 0.1)'
        s.commit()
    rendered = render(db, campaign)
    assert '| a cheaper screen | run, stopped at screen: score 0.5 vs 1 (-0.5; margin 0.1) (candidate#2) |' in (
        rendered
    ), rendered


def test_a_brief_shows_the_latest_notes_on_ideas_and_runs_and_its_ideas_within_its_budget(
    db: Database, campaign: Campaign
) -> None:
    with db.session() as s:
        first = idea(s, campaign, 'a long kept chain')
        base = 1
        for n in range(40):  # a lineage that alone is past the budget
            base = scored(s, first, base, ({'score': 2 + n, 'size': 1}, None)).id
        later = idea(s, campaign, 'a later idea')
        scored(s, later, base, ({'score': 1, 'size': 1}, None))
        comment(s, IDEA, first.id, 'on the idea: the 120 s floor is tight')
        comment(s, RUN, later.claims[0].run_id, 'on the run: merged the head first', author='implementer#2')
        comment(s, IDEA, later.id, 'open: run#41 gave the idea back', author=DAEMON_ACTOR)
        comment(s, TargetKind.CAMPAIGN, campaign.id, 'digest: on track', author='orchestrator')
        s.commit()
        section = History()
        rendering = Rendering(s, utcnow(), s.get_one(Campaign, campaign.id), Path('/w'), cas_of(s))
        text = '\n'.join(compact(line) for line in markdown([section], rendering).splitlines())
    notes = [line for line in text.splitlines() if line.startswith('- comment#')]
    assert notes == [
        f'- comment#3 daemon on idea#{later.id}: open: run#41 gave the idea back',
        f'- comment#2 implementer#2 on run#{later.claims[0].run_id}: on the run: merged the head first',
        f'- comment#1 implementer#1 on idea#{first.id}: on the idea: the 120 s floor is tight',
    ], 'the notes on ideas and runs, newest first, and the run ends that the daemon notes'
    assert '| a later idea |' in text and '| a long kept chain |' in text, 'the ideas fit beside a long lineage'
    assert 'of 41)' in text, 'the baseline and the new bests say how many they leave out'
    assert cost(text.split('\n', 2)[2].splitlines()) <= section.budget


def submit(s: Session, run: Run, candidate: Candidate, row: Idea) -> None:
    """Record ``run``'s submission of ``candidate`` for its claim of ``row`` (``submissions.record``)."""
    record(
        s,
        run.id,
        candidate.id,
        Revision(candidate.revision),
        row.id,
        Agent(role=IMPLEMENTER, session=AgentSessionId(1)),
    )


def test_a_tree_submitted_for_two_claims_is_the_outcome_of_both_ideas(db: Database, campaign: Campaign) -> None:
    """A run submits one tree for idea A, then again for idea B: the candidate names B, and both ideas show it."""
    with db.session() as s:
        a = idea(s, campaign, 'a')
        made = scored(s, a, 1, ({'score': 3, 'size': 1}, None))
        b = idea(s, campaign, 'b')
        run = s.get_one(Run, made.run_id)
        run.claims.append(RunClaim(idea=b, attempt=1))
        submit(s, run, made, a)
        submit(s, run, made, b)
        made.idea_id = b.id
        s.commit()
    rendered = render(db, campaign)
    assert '| 1 | done | t | — | 2 | a | 3 (+2 vs candidate#1) (candidate#2) |' in rendered, 'its first claim'
    assert '| 2 | done | t | — | 2 | b | 3 (+2 vs candidate#1) (candidate#2) |' in rendered, 'its second claim'


def test_a_version_that_another_run_submitted_is_the_outcome_of_that_runs_idea(
    db: Database, campaign: Campaign
) -> None:
    """Run 1 left a version of idea X, and run 2, from the version's parent, submitted it for idea Y: Y shows it,
    against run 2's base."""
    with db.session() as s:
        x = idea(s, campaign, 'x', IdeaStatus.OPEN)
        left = scored(s, x, 1, ({'score': 2, 'size': 1}, None), ({'score': 5, 'size': 1}, None))
        left.origin = Origin.SKIPPED
        y = idea(s, campaign, 'y')
        assert left.parent_id is not None
        other = Run(
            campaign_id=campaign.id,
            base_candidate_id=left.parent_id,
            harness='h',
            model='m',
            claims=[RunClaim(idea=y, attempt=1)],
        )
        other.status, other.end = RunStatus.SUCCEEDED, RunEnd.FINISHED
        s.add(other)
        s.flush()
        submit(s, other, left, y)
        left.idea_id = y.id
        s.commit()
    rendered = render(db, campaign)
    assert '| 2 | done | t | — | 2 | y | 5 (+3 vs candidate#2) (candidate#3) |' in rendered, rendered


def test_a_new_best_submitted_for_two_claims_counts_for_both_ideas(db: Database, campaign: Campaign) -> None:
    """The yields of each source and the ideas that a brief shows first read the claims of a best's submissions."""
    with db.session() as s:
        a = idea(s, campaign, 'a', created_by='research#1')
        made = scored(s, a, 1, ({'score': 3, 'size': 1}, None))
        b = idea(s, campaign, 'b', created_by='orchestrator')
        idea(s, campaign, 'newest')
        run = s.get_one(Run, made.run_id)
        run.claims.append(RunClaim(idea=b, attempt=1))
        submit(s, run, made, a)
        submit(s, run, made, b)
        made.idea_id = b.id
        s.commit()
        row = s.get_one(Campaign, campaign.id)
        assert yields(s, campaign.id) == [
            Yield('orchestrator', ideas=1, ran=1, bests=1),
            Yield('research', ideas=1, ran=1, bests=1),
            Yield('t', ideas=1, ran=0, bests=0),
        ]
        assert [i.title for i in _ranked(s, row, set())] == ['b', 'a', 'newest'], 'the ideas that made a best first'


def test_a_version_that_a_retry_of_its_idea_submitted_counts_against_the_retrys_base(
    db: Database, campaign: Campaign
) -> None:
    """Run 1 left a version of idea A from the baseline (1); run 2 tried A again from a candidate that scores 3, and
    submitted that version, which scores 5: its gain is over run 2's base."""
    with db.session() as s:
        three = scored(s, idea(s, campaign, 'other'), 1, ({'score': 3, 'size': 1}, None))
        a = idea(s, campaign, 'a')
        left = scored(s, a, 1, ({'score': 5, 'size': 1}, None))
        left.origin = Origin.SKIPPED
        retry = Run(
            campaign_id=campaign.id,
            base_candidate_id=three.id,
            harness='h',
            model='m',
            claims=[RunClaim(idea=a, attempt=2)],
        )
        retry.status, retry.end = RunStatus.SUCCEEDED, RunEnd.FINISHED
        s.add(retry)
        s.flush()
        submit(s, retry, left, a)
        s.commit()
    assert '| 2 | done | t | — | 2 | a | 5 (+2 vs candidate#2) (candidate#3) |' in render(db, campaign)


def test_the_past_campaigns_query_counts_a_tree_submitted_for_two_ideas_for_both(
    db: Database, campaign: Campaign
) -> None:
    """The query of the past-campaigns guide, verbatim but for the campaign id, and its lookup of an implementation."""
    guide = (files('piloop.plugins.section') / 'prompts' / 'past_campaigns.md').read_text()
    query = guide.split('```sql\n', 1)[1].split('```', 1)[0].replace('<campaign id>', str(campaign.id))
    with db.session() as s:
        s.get_one(Campaign, campaign.id).repo = 'the repo'
        a = idea(s, campaign, 'sync acquisition')
        made = scored(s, a, 1, ({'score': 3, 'size': 1}, None))
        b = idea(s, campaign, 'onset')
        run = s.get_one(Run, made.run_id)
        run.claims.append(RunClaim(idea=b, attempt=1))
        submit(s, run, made, a)
        submit(s, run, made, b)
        made.idea_id = b.id
        s.commit()
        counted = {row.title: row.submitted for row in s.execute(text(query))}
        lookup = 'SELECT i.title, c.revision FROM ideas i JOIN submissions s ON s.idea_id = i.id'
        found = s.execute(text(f'{lookup} JOIN candidates c ON c.id = s.candidate_id ORDER BY i.id')).all()
    assert counted == {'sync acquisition': 1, 'onset': 1}
    assert found == [('sync acquisition', made.revision), ('onset', made.revision)]


def test_the_yields_of_thousands_of_new_bests_take_a_few_queries(db: Database, campaign: Campaign) -> None:
    """Which ideas each best was submitted for is one batch: one lookup a best scanned the events, 12 s at 4,000."""
    n = 4000
    with db.session() as s:
        a = idea(s, campaign, 'a', created_by='research#1')
        run = Run(
            campaign_id=campaign.id, base_candidate_id=1, harness='h', model='m', claims=[RunClaim(idea=a, attempt=1)]
        )
        run.status, run.end = RunStatus.SUCCEEDED, RunEnd.FINISHED
        s.add(run)
        s.flush()
        rows = [
            Candidate(campaign_id=campaign.id, run_id=run.id, idea_id=a.id, tree_sha=f't{i}', revision=f'r{i}')
            for i in range(n)
        ]
        s.add_all(rows)
        s.flush()
        me = Agent(role=IMPLEMENTER, session=AgentSessionId(1))
        for c in rows:
            record(s, run.id, c.id, Revision(c.revision), a.id, me)
            best = CandidateScored(track='score', score=float(c.id), measured=1, best=c.id, rank=1, told='best')
            emit(s, best, TargetRef(CANDIDATE, c.id), me)
        s.commit()
        statements: list[object] = []

        def counted(*args: object) -> None:
            statements.append(args[2])

        listen(db.engine, 'before_cursor_execute', counted)
        try:
            started = time.monotonic()
            found = yields(s, campaign.id)
            took = time.monotonic() - started
        finally:
            remove(db.engine, 'before_cursor_execute', counted)
    assert found == [Yield('research', ideas=1, ran=1, bests=n)]
    assert took < 3, f'{took:.1f} s for {n} new bests'
    assert len(statements) < 10, f'{len(statements)} queries for {n} new bests'


def test_a_submission_before_0_6_names_no_claim_so_its_candidate_names_its_idea(
    db: Database, campaign: Campaign
) -> None:
    with db.session() as s:
        a = idea(s, campaign, 'a')
        made = scored(s, a, 1, ({'score': 3, 'size': 1}, None))
        assert made.run_id is not None
        s.add(
            Submission(run_id=made.run_id, candidate_id=made.id, idea_id=None, event_id=0)
        )  # 0018 moved it from a claimless run
        s.flush()
        assert ideas_of(s, {made.id}) == {made.id: {a.id}}


def test_an_idea_whose_submissions_all_crashed_shows_its_newest(db: Database, campaign: Campaign) -> None:
    with db.session() as s:
        a = idea(s, campaign, 'a')
        last = scored(s, a, 1, ({}, 'boom'), ({}, 'boom again'))
        run = s.get_one(Run, last.run_id)
        for made in run.candidates:
            submit(s, run, made, a)
        s.commit()
    assert '| 1 | done | t | — | 2 | a | crash (candidate#3) |' in render(db, campaign), 'the newest, not the oldest'


def test_what_waits_for_a_score_and_whose_ideas_read_past_sqlites_limit_on_parameters(
    db: Database, campaign: Campaign
) -> None:
    """With more runs and candidates than SQLite takes parameters in one statement (here lowered to 20), what waits for
    a score and which ideas each candidate was submitted for read as for a few: the ids never go one per parameter."""
    n = 30
    with db.session() as s:
        a = idea(s, campaign, 'a')
        me = Agent(role=IMPLEMENTER, session=AgentSessionId(1))
        made = []
        for i in range(n):
            run = Run(campaign_id=campaign.id, base_candidate_id=1, harness='h', model='m', status=RunStatus.RUNNING)
            s.add(run)
            s.flush()
            c = Candidate(campaign_id=campaign.id, run_id=run.id, idea_id=a.id, tree_sha=f't{i}', revision=f'r{i}')
            s.add(c)
            s.flush()
            record(s, run.id, c.id, Revision(c.revision), a.id, me)
            made.append(c.id)
        s.commit()
        raw = s.connection().connection.driver_connection
        assert isinstance(raw, sqlite3.Connection)
        was = raw.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 20)
        try:
            assert [c.id for c in waiting(s, s.get_one(Campaign, campaign.id))] == made
            assert ideas_of(s, set(made)) == dict.fromkeys(made, {a.id})
        finally:
            raw.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, was)


def test_an_unsubmitted_draft_of_an_earlier_run_comes_before_a_later_runs_crashed_submission(
    db: Database, campaign: Campaign
) -> None:
    """Run 1 kept a draft of idea A and gave it back; run 2 tried A again and submitted a version that crashed. With no
    score, the history shows the newest: the crash, not the draft."""
    with db.session() as s:
        a = idea(s, campaign, 'a')
        draft = scored(s, a, 1, ({}, 'not submitted'))
        draft.origin = Origin.SKIPPED
        draft.created_at = utcnow() - timedelta(hours=1)
        retry = Run(
            campaign_id=campaign.id, base_candidate_id=1, harness='h', model='m', claims=[RunClaim(idea=a, attempt=2)]
        )
        retry.status, retry.end = RunStatus.SUCCEEDED, RunEnd.FINISHED
        s.add(retry)
        s.flush()
        crashed = Candidate(campaign_id=campaign.id, run_id=retry.id, idea_id=a.id, tree_sha='c', revision='c0ffee00')
        s.add(crashed)
        s.flush()
        evaluator_id = s.get_one(Campaign, campaign.id).evaluator_id
        assert evaluator_id is not None
        s.add(Measurement(candidate_id=crashed.id, evaluator_id=evaluator_id, metrics={}, error='boom'))
        submit(s, retry, crashed, a)
        s.commit()
    assert '| 1 | done | t | — | 2 | a | crash (candidate#3) |' in render(db, campaign)
