"""Tests for the history that an agent reads in its brief and that ``piloop history`` prints."""

import re
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from piloop.actor import Agent
from piloop.config import CampaignConfig
from piloop.db.models import AgentSession, Campaign, Comment, utcnow
from piloop.db.session import Database
from piloop.ids import Revision, TargetKind, TargetRef
from piloop.plugins.campaign_type.kit.backlog.ids import IDEA, IdeaStatus, LinkKind
from piloop.plugins.campaign_type.kit.backlog.tables import Idea, IdeaLink
from piloop.plugins.campaign_type.kit.candidates.ids import Outcome
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.evaluation.ids import RungName
from piloop.plugins.campaign_type.kit.evaluation.tables import Measurement
from piloop.plugins.campaign_type.kit.runs.ids import FINDING, IMPLEMENTER, RUN, RunEnd, RunStatus
from piloop.plugins.campaign_type.kit.runs.tables import Run, working_on
from piloop.plugins.campaign_type.optimization.history import FIRST_LINE_CHARS, comment_line, history, newest
from piloop.plugins.campaign_type.optimization.section.history import History
from piloop.plugins.campaign_type.optimization.section.pack import Pack
from piloop.plugins.section.contract import Briefing, Rendering, markdown
from piloop.reports.wake import cost
from piloop.text import CELL_CHARS, cell
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
    *outcomes: tuple[Outcome, dict[str, float], str | None],
    tokens: int | None = None,
) -> Candidate:
    """Add one run of ``row`` from candidate ``base`` with one measured candidate per outcome. Return the last."""
    run = Run(idea=row, attempt=len(row.runs) + 1, base_candidate_id=base, harness='h', model='m')
    run.status, run.end = RunStatus.SUCCEEDED, RunEnd.FINISHED
    s.add(run)
    s.flush()
    usage = None if tokens is None else spent(tokens, 3)
    s.add(AgentSession(kind=IMPLEMENTER, campaign_id=row.campaign_id, subject=TargetRef(RUN, run.id), usage=usage))
    evaluator_id = row.campaign.evaluator_id
    assert evaluator_id is not None
    parent = base
    for n, (verdict, metrics, error) in enumerate(outcomes):
        c = Candidate(
            campaign_id=row.campaign_id,
            run_id=run.id,
            parent_id=parent,
            tree_sha=f'{row.id}-{run.id}-{n}',
            revision=f'{row.id}{run.id}{n}0000deadbeef',
            summary=f'{row.title} commit' if error is None else None,
            verdict=verdict,
        )
        s.add(c)
        s.flush()
        s.add(Measurement(candidate_id=c.id, evaluator_id=evaluator_id, metrics=metrics, error=error))
        parent = c.id
    return c


def comment(s: Session, kind: TargetKind, target: int, body: str, author: str = 'implementer#1') -> None:
    s.add(Comment(target=TargetRef(kind, target), author=author, kind=FINDING, body=body))


def render(db: Database, campaign: Campaign) -> str:
    """Render the history with the Rich column padding collapsed, so that assertions match the rows."""
    return '\n'.join(compact(line) for line in raw(db, campaign).splitlines()) + '\n'


def full(s: Session, campaign: Campaign, cfg: CampaignConfig) -> str:
    """The history as the research pack shows it, with its default bounds."""
    return history(s, campaign, cfg, ideas=newest(s, campaign, SHOWN.ideas), findings=SHOWN.campaign_findings)


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
            (Outcome.KEEP, {'score': 3, 'size': 12}, None),
            tokens=234_444,
        )
        comment(s, IDEA, kept.id, 'appending works | keep appending\nsecond line is not shown')
        worse = idea(s, campaign, 'fewer lines', created_by='implementer#4')
        scored(s, worse, e2.id, (Outcome.DISCARD, {'score': 2, 'size': 8}, None))
        crashed = idea(s, campaign, 'delete solve.txt', IdeaStatus.DROPPED, priority=3)
        scored(s, crashed, e2.id, (Outcome.CRASH, {}, 'no such file'))
        comment(s, IDEA, crashed.id, 'first attempt: evaluate needs the file')
        comment(s, IDEA, crashed.id, 'second attempt agrees; rejected', author='watchdog#2')
        mixed = idea(s, campaign, 'two commits', after=(kept.id,))
        scored(s, mixed, e2.id, (Outcome.CRASH, {}, 'syntax'), (Outcome.KEEP, {'score': 4, 'size': 16}, None))
        idea(s, campaign, 'a' * 200, IdeaStatus.OPEN, 'h' * 200, priority=0, after=(crashed.id, kept.id))
        idea(s, campaign, 'human should look', IdeaStatus.OPEN, 'the metric | is wrong\nsecond line')
        idea(s, campaign, 'fewer but longer lines', IdeaStatus.OPEN, after=(mixed.id, kept.id))
        idea(s, campaign, 'no lines at all', IdeaStatus.OPEN)
        comment(
            s, TargetKind.CAMPAIGN, campaign.id, 'batch 1: extend appending\nrejected: deleting', author='research#1'
        )
        comment(s, TargetKind.CAMPAIGN, campaign.id, 'idle: best is #2; try longer lines', author='watchdog#2')
        comment(s, TargetKind.CAMPAIGN, campaign.id, 'batch 2: nothing new worth proposing', author='research#3')
        comment(s, TargetKind.CAMPAIGN, campaign.id, 'the cache keep holds on fresh data', author='reviewer#7')
        comment(s, TargetKind.CAMPAIGN, campaign.id, 'digest: on track', author='orchestrator')
        s.commit()
    text = render(db, campaign)
    has_lines(
        text,
        '### History: ideas (8 of 8)',
        '| 1 | done | research#9 | — | 1 | more lines | keep 3 (+2 vs candidate#1) (candidate#2) |',
        '| 2 | done | implementer#4 | — | 2 | fewer lines | discard 2 (-1 vs candidate#2) (candidate#3) |',
        '| 3 | dropped | t | — | 3 | delete solve.txt | crash (candidate#4) |',
        '| 4 | done | t | after idea#1 | 2 | two commits | keep 4 (+1 vs candidate#2) (candidate#6) |',
        f'| 5 | open | t | after idea#1, idea#3 | 0 | {"a" * 99}… | — |',
        '| 6 | open | t | — | 2 | human should look | — |',
        '| 7 | open | t | after idea#1, idea#4 | 2 | fewer but longer lines | — |',
        '| 8 | open | t | — | 2 | no lines at all | — |',
        '### History: kept lineage',
        '| #1 | — | 1 | — | — | ba5e11a | — | baseline |',
        '| #2 | #1 | 3 | +2 | idea#1 (research#9) | 1100000 | 234k | more lines commit |',
        '| #6 | #5 | 4 | — | idea#4 (t) | 4410000 | — | two commits commit |',
        '### History: findings on the campaign (last 10)',
        '- comment#4 research#1: batch 1: extend appending (+1 lines)',
        '- comment#5 watchdog#2: idle: best is #2; try longer lines',
        '- comment#6 research#3: batch 2: nothing new worth proposing',
        '- comment#7 reviewer#7: the cache keep holds on fresh data',
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
    findings = [line for line in rendered.splitlines() if ' research#1: finding' in line]
    assert findings[0].endswith(' research#1: finding 2') and len(findings) == SHOWN.campaign_findings


def test_history_worst_case_stays_under_the_documented_size(db: Database, campaign: Campaign) -> None:
    with db.session() as s:
        previous: Idea | None = None
        for n in range(SHOWN.ideas):
            links = (previous.id,) if previous is not None else ()
            row = idea(s, campaign, f'{n} ' + 't' * 200, IdeaStatus.DROPPED, 'h' * 200, after=links)
            scored(s, row, 1, (Outcome.DISCARD, {'score': 0.123456, 'size': 1}, None))
            comment(s, IDEA, row.id, 'f' * 200)
            previous = row
        for _ in range(SHOWN.campaign_findings):
            comment(s, TargetKind.CAMPAIGN, campaign.id, '\n'.join(['r' * 79] * 30), author='research#1')
        s.commit()
    rendered = render(db, campaign)
    assert 'after idea#58' in rendered, 'every link: a dropped prerequisite frees its dependent'
    assert len(raw(db, campaign)) < 64_000
    assert len(rendered) < 18_000


def test_history_when_a_human_kept_an_unscored_commit_and_an_idea_builds_on_it(
    db: Database, campaign: Campaign
) -> None:
    with db.session() as s:
        unscored = Candidate(
            campaign_id=campaign.id, tree_sha='y', revision='y', verdict=Outcome.KEEP, verdict_by='human:t'
        )
        s.add(unscored)
        s.flush()
        row = idea(s, campaign, 'from an unscored base')
        scored(s, row, unscored.id, (Outcome.KEEP, {'score': 5, 'size': 1}, None))
        s.commit()
    rendered = render(db, campaign)
    assert '| 1 | done | t | — | 2 | from an unscored base | keep 5 (candidate#3) |' in rendered, (
        'no delta without a base score'
    )
    assert '| #2 | — | — | — | — | y | — | — |' in rendered, 'the unscored base is in the lineage without a value'
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


def test_an_implementers_history_shows_the_keeps_then_the_newest_within_its_budget(
    db: Database, campaign: Campaign
) -> None:
    with db.session() as s:
        idea(s, campaign, 'older')
        kept = idea(s, campaign, 'kept')
        scored(s, kept, 1, (Outcome.KEEP, {'score': 0.9, 'size': 1}, None))
        for n in range(30):
            idea(s, campaign, f'filler {n}')
        mine = idea(s, campaign, 'mine', IdeaStatus.CLAIMED)
        scored(s, mine, 1, (Outcome.DISCARD, {'score': 0.1, 'size': 1}, None))
        reader = s.scalars(select(AgentSession.id).where(working_on(mine.runs[0].id))).one()
        s.commit()
        p = s.get_one(Campaign, campaign.id)
        section = History(budget=900, findings=0)
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
    assert shown[:1] == [kept.id] and len(shown) < 31, 'the keeps first, then what fits'
    assert shown[1:] == list(range(mine.id - len(shown) + 1, mine.id)), 'then the newest, oldest first'
    assert f'of 33; besides idea#{mine.id}, the keeps, then the newest)' in text
    assert cost(text.split('\n', 2)[2].splitlines()) <= section.budget
    assert '| mine ' in everything and '(33 of 33)' in everything, 'a reader without a run sees every idea'


def test_an_idea_whose_candidate_stopped_at_a_rung_says_where_and_why(db: Database, campaign: Campaign) -> None:
    with db.session() as s:
        c = scored(s, idea(s, campaign, 'a cheaper screen'), 1, (Outcome.DISCARD, {'score': 0.5}, None))
        [m] = c.measurements
        m.rung, m.stopped = RungName('screen'), 'score 0.5 vs 1 (-0.5; margin 0.1)'
        s.commit()
    rendered = render(db, campaign)
    assert '| a cheaper screen | discard, stopped at screen: score 0.5 vs 1 (-0.5; margin 0.1) (candidate#2) |' in (
        rendered
    ), rendered
