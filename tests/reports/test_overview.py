"""Tests for the overview that ``piloop status`` prints and that each sweep reads in its brief."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from inline_snapshot import snapshot
from sqlalchemy.orm import Session

from piloop.agents.results import StartupAnomaly
from piloop.config import CampaignConfig, load_campaign_config
from piloop.db.models import AgentSession, AgentSessionState, Campaign, Comment, Event
from piloop.db.session import Database
from piloop.ids import CampaignStatus, CommentKind, RoleName, TargetKind, TargetRef
from piloop.plugins.campaign_type.kit.backlog.ids import IDEA, IdeaStatus, LinkKind
from piloop.plugins.campaign_type.kit.backlog.section.queue import Queue
from piloop.plugins.campaign_type.kit.backlog.tables import Idea, IdeaLink
from piloop.plugins.campaign_type.kit.candidates.ids import Cause, Outcome
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.evaluation.ids import RungName
from piloop.plugins.campaign_type.kit.evaluation.tables import Measurement
from piloop.plugins.campaign_type.kit.runs.ids import IMPLEMENTER, RUN, RunId, RunStatus
from piloop.plugins.campaign_type.kit.runs.tables import Run
from piloop.plugins.section.contract import SECTIONS, Rendering, Surface, defaults, markdown
from piloop.reports import overview as overview_module
from piloop.text import LINKED_CHARS, age, block
from tests.conftest import RESEARCH, WATCHDOG, cas_of, end_of, has_lines, home_cas, reconfigured, spent

T = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
NOW = T + timedelta(hours=2)


def at(minutes: float) -> datetime:
    return T + timedelta(minutes=minutes)


def idea(
    s: Session, campaign: Campaign, title: str, status: IdeaStatus, *, after: tuple[int, ...] = (), **kw: object
) -> Idea:
    row = Idea(
        track='score',
        campaign_id=campaign.id,
        title=title,
        hypothesis=title,
        status=status,
        created_by='research#4',
        links=[IdeaLink(target_id=t, kind=LinkKind.AFTER) for t in after],
        **kw,
    )
    s.add(row)
    s.flush()
    return row


def run(s: Session, i: Idea, status: RunStatus, base: int, start: float, end: float | None, **kw: object) -> Run:
    row = Run(
        campaign_id=i.campaign_id,
        idea_id=i.id,
        attempt=1,
        base_candidate_id=base,
        harness='script',
        model='m',
        status=status,
        end=end_of(status),
        created_at=at(start),
        started_at=at(start),
        finished_at=at(end) if end is not None else None,
        **kw,
    )
    s.add(row)
    s.flush()
    return row


def agent(
    s: Session,
    campaign: Campaign,
    kind: RoleName,
    start: float,
    lived: float,
    *,
    run_id: RunId | None = None,
    **kw: object,
) -> AgentSession:
    row = AgentSession(
        kind=kind,
        campaign_id=campaign.id,
        subject=None if run_id is None else TargetRef(RUN, run_id),
        harness='script',
        runtime='subprocess',
        started_at=at(start),
        ended_at=at(start) + timedelta(seconds=lived),
        **{'state': AgentSessionState.EXITED, **kw},
    )
    s.add(row)
    s.flush()
    return row


def candidate(
    s: Session, campaign: Campaign, parent: int, verdict: Outcome, value: float, when: float, **kw: object
) -> Candidate:
    row = Candidate(
        campaign_id=campaign.id,
        parent_id=parent,
        tree_sha=f'tree{when}',
        revision=f'{when:07.0f}abcdef',
        summary=f'score {value:g}',
        verdict=verdict,
        **kw,
    )
    s.add(row)
    s.flush()
    s.add(
        Measurement(
            candidate_id=row.id,
            evaluator_id=campaign.evaluator_id,
            metrics={'score': value, 'size': 10.0},
            seconds=60 * value,
            created_at=at(when),
        )
    )
    return row


def live_campaign(db: Database, campaign: Campaign) -> None:
    """Add two hours of a small campaign that has one example of each anomaly."""
    with db.session() as s:
        p = s.get_one(Campaign, campaign.id)
        p.created_at = T
        baseline = s.get_one(Candidate, 1)
        baseline.measurements[0].created_at = T
        good = idea(s, p, 'longer lines', IdeaStatus.DONE)
        first = run(s, good, RunStatus.SUCCEEDED, 1, 5, 20)
        agent(s, p, IMPLEMENTER, 5, 600, run_id=first.id, usage=spent(1000))
        kept = candidate(s, p, 1, Outcome.KEEP, 3, 18, run_id=first.id)
        candidate(s, p, kept.id, Outcome.DISCARD, 3, 19, run_id=first.id)
        bad = idea(s, p, 'crash it', IdeaStatus.DROPPED)
        failed = run(s, bad, RunStatus.FAILED, 1, 30, 40, error='boom\nand the rest')
        dead = agent(s, p, IMPLEMENTER, 30, 5, run_id=failed.id, state=AgentSessionState.KILLED, error='dialog')
        startup = StartupAnomaly(
            role=IMPLEMENTER, harness='script', subject=TargetRef(RUN, failed.id), seconds=5.0, error='dialog'
        )
        s.add(
            Event(
                actor='daemon',
                target=TargetRef(TargetKind.AGENT_SESSION, dead.id),
                payload=startup,
                at=at(30) + timedelta(seconds=5),
            )
        )
        pinned = idea(s, p, 'build on longer lines', IdeaStatus.CLAIMED, after=(good.id,))
        stuck = run(s, pinned, RunStatus.RUNNING, 1, 50, None)
        agent(s, p, IMPLEMENTER, 50, 300, run_id=stuck.id, usage=spent(2000))
        idea(s, p, 'after the crash', IdeaStatus.OPEN, after=(bad.id,))
        agent(s, p, RESEARCH, 60, 120, usage=spent(500))
        s.add_all(
            [
                Comment(
                    target=TargetRef(RUN, failed.id),
                    author='watchdog#9',
                    kind=CommentKind.ESCALATION,
                    priority=1,
                    body='run#2 died in a dialog\nevidence below',
                    created_at=at(45),
                ),
                Comment(
                    target=TargetRef(TargetKind.CAMPAIGN, p.id),
                    author='pi',
                    kind=CommentKind.DECISION,
                    body='keep going',
                    created_at=at(46),
                ),
                Comment(
                    target=TargetRef(IDEA, good.id),
                    author='implementer#1',
                    body='a note',
                    created_at=at(47),
                ),
            ]
        )
        s.commit()


SWEEP = [SECTIONS.validated({'provider': name}) for name in [*defaults(Surface.STATUS), 'tools']]
"""A sweep's sections without the lessons and the pack."""


def overview(s: Session, campaign: Campaign, now: datetime = NOW) -> str:
    """The campaign's overview and tools as a sweep reads them."""
    return markdown(SWEEP, Rendering(s, now, campaign, Path('/no/workspace'), cas_of(s)))


def render(db: Database, campaign: Campaign, cfg: CampaignConfig | None = None, now: datetime = NOW) -> str:
    with db.session() as s:
        p = s.get_one(Campaign, campaign.id)
        if cfg is not None:
            p.config = cfg  # rolled back with the session
        return overview(s, p, now)


def test_the_overview_of_a_campaign(db: Database, campaign: Campaign) -> None:
    live_campaign(db, campaign)
    text = render(db, campaign)
    lay, tools = text.split('## Tools\n\n')
    has_lines(
        lay,
        '## Campaign',
        '## Goal',
        '- Primary metric: score, higher is better; also measured: size (lower, watch)',
        '- Campaign: active. Progress: run 2 · 1.9h · no limit stops it: open-ended.',
        '## Trajectory',
        '- Start candidate#1, head candidate#2: score 1 → 3 (+2, +200.0%); 1 trunk keeps.',
        '  - 10-01 12:18 candidate#2 3 (+2) idea#1: score 3',
        '## Runs',
        '  - run#3 idea#3 "build on longer lines" (script): running 1h, agent_session#3 ended 1h ago',
        '## Queue',
        '## Agents',
        '- Agents live: none',
        '## Evaluations',
        '## Funnel',
        'ideas: 4 · attempts: 2 · reached the evaluator: 1 · measured: 1 · keeps: 1',
        '| failed | 1 |',
        '## Yield',
        '- 1 of the last 2 runs scored, 1 kept; the others: 1 crashed.',
        '## Recent candidates (3 of 3; score)',
        '| candidate#2 | keep | 3 | 2 | run#1 | 0s | score 3 |',
        '| candidate#3 | discard | 3 | 0 | run#1 | 0s | score 3 |',
        '## Candidates that scored exactly their parent',
        '- candidate#3, 1h ago: scored what candidate#2 did on score and on 1 of 1 other metrics; 3.0 min of evaluation',
        '## Incidents given to agents',
        '## Inbox (1 unacknowledged; 1 escalation, the oldest comment#1 1h old), decisions and escalations',
        '- comment#1 escalation (p1, unacknowledged) on run#2 by watchdog#9, 1h ago: run#2 died in a dialog',
        '## Recent events',
        '- comment.note comment#3 0s ago',
    )
    assert 'head: candidate#2 · score: 3 · score vs baseline: 2 · kept: 2 · candidates: 3 · tokens: 3500' in lay
    assert '- Ideas: 1 open, 0 held; ' in lay, 'idea#4 is after idea#2, which is dropped: it is free'
    assert '- Agents so far: 3 implementer (1 killed),' in lay
    assert 'evaluations: 3 · crashed: 0 · median: 3 min' in lay
    assert tools.startswith('- `piloop sql "<one statement>"`')
    assert '  - agent_sessions(id, kind, campaign_id, subject_kind, subject_id, harness,' in tools, (
        'the schema is listed, so an agent does not need PRAGMA table_info'
    )
    assert '  - leases(' not in tools and '  - evaluators(' not in tools, 'not the tables that only piloop reads'


def test_stop_conditions_say_how_far_each_one_is(db: Database, campaign: Campaign) -> None:
    live_campaign(db, campaign)
    stop = {'target': 5, 'max_runs': 10, 'max_hours': 4, 'max_tokens': 1_000_000, 'plateau': 3}
    cfg = load_campaign_config(Path(campaign.path), plugins={'stop_rule': stop})
    [line] = [x for x in render(db, campaign, cfg).splitlines() if x.startswith('- Campaign:')]
    assert line == snapshot(
        '- Campaign: active. Progress: run 2/10 · 1.9h/4h · 4k/1.0M tok · 1/3 runs without a keep · target ≥ 5 (head 3).'
    ), 'run#2 failed before it committed: plateau counts it, max_runs does not'
    with db.session() as s:
        s.get_one(Campaign, campaign.id).status = CampaignStatus.STOPPED
        s.get_one(Campaign, campaign.id).stopped_reason = 'plateau: 3 runs without a keep'
        s.commit()
    text = render(db, campaign, cfg)
    assert text.count('plateau: 3 runs without a keep') == 1, 'the reason is in the headline only'
    assert '- Campaign: stopped. Progress:' in text


def test_a_fresh_campaign_has_little_to_say(db: Database, campaign: Campaign) -> None:
    """A campaign with no run, no comment and no trunk keep shows no trajectory and reports an idle queue."""
    stop = {'max_hours': 4, 'plateau': 2}
    cfg = load_campaign_config(Path(campaign.path), plugins={'stop_rule': stop})
    with db.session() as s:
        s.get_one(Campaign, campaign.id).created_at = T
        s.get_one(Candidate, 1).verdict = None
        s.commit()
    text = render(db, campaign, cfg)
    assert '- Campaign: active. Progress: run 0 · 0.0h/4h · 0/2 runs without a keep.' in text
    assert "- No trunk keep: the baseline's verdict was overridden and nothing was kept since." in text
    assert 'median:' not in text, 'the baseline has no recorded seconds'
    assert 'evaluations: 1 · crashed: 0 · waiting for a verdict: 1 candidates' in text
    assert '- 1 free implementer slots and no runnable idea since 2h ago; no research sweep pending' in text
    assert '## Inbox (0 unacknowledged), decisions and escalations\n\n- None.' in text


def test_recent_candidates_say_how_far_each_climbed_the_ladder(db: Database, campaign: Campaign) -> None:
    cfg = load_campaign_config(Path(campaign.path), ladder=[{'name': 'screen', 'stop': {'score': {'margin': 0.1}}}])
    with db.session() as s:
        p = s.get_one(Campaign, campaign.id)
        stopped = candidate(s, p, 1, Outcome.DISCARD, 0.5, 18)
        stopped.measurements[0].rung = RungName('screen')
        stopped.measurements[0].stopped = 'score 0.5 vs 1 (-0.5; margin 0.1)'
        climbing = candidate(s, p, 1, Outcome.KEEP, 2, 19)
        climbing.verdict = None
        climbing.measurements[0].rung = RungName('screen')
        s.commit()
    text = render(db, campaign, cfg)
    table = text[text.index('## Recent candidates') :].split('\n\n')[1]
    assert table == snapshot("""\
| candidate | verdict | score | vs parent | ladder | run | age | summary |
|---|---|---|---|---|---|---|---|
| candidate#1 | keep | 1 | — | — | no run | 0s | baseline |
| candidate#2 | discard | — | — | stopped at screen: score 0.5 vs 1 (-0.5; margin 0.1) | no run | 0s | score 0.5 |
| candidate#3 | pending | — | — | reached screen | no run | 0s | score 2 |\
""")


def test_held_ideas_beyond_the_shown_few_are_counted(db: Database, campaign: Campaign) -> None:
    with db.session() as s:
        p = s.get_one(Campaign, campaign.id)
        gate = idea(s, p, 'gate', IdeaStatus.CLAIMED)
        for n in range(Queue().held + 1):
            idea(s, p, f'waits {n}', IdeaStatus.OPEN, after=(gate.id,))
        s.commit()
    assert ', 1 more); 1 claimed, 0 done, 0 dropped' in render(db, campaign)


def test_open_runs_say_how_far_they_got(db: Database, campaign: Campaign) -> None:
    with db.session() as s:
        p = s.get_one(Campaign, campaign.id)
        waiting = run(s, idea(s, p, 'waiting', IdeaStatus.CLAIMED), RunStatus.QUEUED, 1, 100, None)
        waiting.started_at = None
        going = run(s, idea(s, p, 'going', IdeaStatus.CLAIMED), RunStatus.RUNNING, 1, 110, None)
        s.add(
            AgentSession(
                kind=IMPLEMENTER, campaign_id=p.id, subject=TargetRef(RUN, going.id), state=AgentSessionState.RUNNING
            )
        )
        s.commit()
    cfg = load_campaign_config(Path(campaign.path), plugins={'role': {'implementer': {'max_concurrent': 2}}})
    lines = render(db, campaign, cfg).splitlines()
    start = lines.index('- Implementer slots: 2 of 2 busy.')
    assert lines[start + 1 : start + 3] == [
        '  - run#1 idea#1 "waiting" (script): queued',
        '  - run#2 idea#2 "going" (script): running 10m',
    ]


def test_branches_and_notes_say_where_else_a_run_may_start(db: Database, campaign: Campaign) -> None:
    """A branch keep starts its own lineage, and a note shows a candidate that regressed.

    A keep that the current evaluator has not measured yet is in no lineage.
    """
    live_campaign(db, campaign)
    with db.session() as s:
        p = s.get_one(Campaign, campaign.id)
        explore = idea(s, p, 'from the baseline', IdeaStatus.DONE)
        branch = run(s, explore, RunStatus.SUCCEEDED, 1, 60, 70, branch=True)
        candidate(s, p, 1, Outcome.KEEP, 2, 65, run_id=branch.id)
        noted = candidate(s, p, 1, Outcome.INTERESTING, 0.5, 66, run_id=branch.id, verdict_by=f'run#{branch.id}')
        noted.cause, noted.verdict_reason = Cause.NOTE, 'halves the eval time; against candidate#1: score 0.5 vs 1'
        s.add(Candidate(campaign_id=p.id, tree_sha='unmeasured', revision='u', verdict=Outcome.KEEP, run_id=branch.id))
        s.commit()
    trajectory = render(db, campaign).split('## Trajectory\n\n')[1].split('## Runs')[0]
    assert trajectory == snapshot("""\
- Start candidate#1, head candidate#2: score 1 → 3 (+2, +200.0%); 1 trunk keeps.
- Last trunk keep 1h ago; 3 runs finished since.
- Trunk keeps (latest 8; when first measured, the gain on its track over the head before it):
  - 10-01 12:00 candidate#1 1 (start): baseline
  - 10-01 12:18 candidate#2 3 (+2) idea#1: score 3
- Lineages (latest keep of each; a branch keep never moves the head):
  - trunk from candidate#1 (keep): latest candidate#2 3, 2 kept
  - branch from candidate#1 (keep): latest candidate#6 —, 2 kept
- Notes (regressed, kept in view as leads; a worker takes the change of one):
  - candidate#5 0.5 by run#4: halves the eval time; against candidate#1: score 0.5 vs 1

""")


def test_the_queue_the_live_agents_the_inbox_and_the_events(db: Database, campaign: Campaign, tmp_path: Path) -> None:
    """The runs, queue, agents, inbox and recent events sections show every row with its age."""
    live_campaign(db, campaign)
    log_dir = tmp_path / 'research-6'
    log_dir.mkdir()
    (log_dir / 'transcript.txt').write_text('thinking\n\nran evaluate.py: METRIC score=3\n\n')
    with db.session() as s:
        p = s.get_one(Campaign, campaign.id)
        idea(s, p, 'a' * 120, IdeaStatus.OPEN, priority=0, created_at=at(100))
        idea(s, p, 'even longer lines', IdeaStatus.OPEN, priority=1, created_at=at(110))
        s.add_all(
            [
                AgentSession(
                    kind=RESEARCH,
                    campaign_id=p.id,
                    harness='script',
                    model='m',
                    runtime='subprocess',
                    state=AgentSessionState.RUNNING,
                    started_at=at(115),
                    reason='low_water: 2 ready < 10',
                    log_dir=str(log_dir),
                ),
                AgentSession(kind=WATCHDOG, campaign_id=p.id, state=AgentSessionState.REQUESTED, reason='cron'),
            ]
        )
        s.commit()
    text = render(db, campaign)
    has_lines(
        text,
        '## Runs',
        '  - run#3 idea#3 "build on longer lines" (script): running 1h, agent_session#3 ended 1h ago',
        '## Queue',
        '  - idea#5 p0 "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa…" by research#4, 20m ago',
        '  - idea#6 p1 "even longer lines" by research#4, 10m ago',
        '  - idea#4 p2 "after the crash" by research#4, 0s ago; after idea#2',
        '## Agents',
        '- Agents live: 1; requested: watchdog#6',
        '  - research#5 script/m 5m, on low_water: 2 ready < 10: ran evaluate.py: METRIC score=3',
        '## Inbox (1 unacknowledged; 1 escalation, the oldest comment#1 1h old), decisions and escalations',
        '- comment#2 decision on campaign#1 by pi, 1h ago: keep going',
        '- comment#1 escalation (p1, unacknowledged) on run#2 by watchdog#9, 1h ago: run#2 died in a dialog',
        '## Recent events',
        '- agent_session.requested agent_session#6 0s ago',
        '- idea.created idea#6 0s ago',
        '- idea.created idea#5 0s ago',
    )
    assert '- Ideas: 3 open, 0 held;' in text


def test_age_floors_to_the_largest_unit() -> None:
    assert age(NOW, NOW) == '0s'
    assert age(NOW - timedelta(seconds=59), NOW) == '59s'
    assert age(NOW - timedelta(minutes=3, seconds=59), NOW) == '3m'
    assert age(NOW - timedelta(hours=23, minutes=59), NOW) == '23h'
    assert age(NOW - timedelta(days=40), NOW) == '40d'
    assert age(NOW + timedelta(hours=1), NOW) == '0s', 'a clock skew is not a negative age'


SESSION_ID = 'd96a2121-355c-4608-b2f7-8bfc85bba1b5'


def test_live_agents_say_their_last_words_from_the_stored_session_else_their_output_else_their_transcript(
    db: Database, campaign: Campaign, tmp_path: Path
) -> None:
    stored = home_cas(db).put((Path(__file__).parent.parent / 'fixtures' / 'said' / 'claude.jsonl').read_bytes())
    writing, saved, silent = tmp_path / 'writing', tmp_path / 'saved', tmp_path / 'silent'
    for log_dir in (writing, saved, silent):
        log_dir.mkdir()
    (writing / 'agent_session.stdout').write_text('{"type":"turn.started"}\n\n{"type":"item.completed"}\n')
    (saved / 'transcript.txt').write_text('Ran the evaluation; measurement pending.\n')
    with db.session() as s:
        for harness, session_file, log_dir in (
            ('claude', stored, silent),
            ('claude', None, writing),
            ('codex', None, saved),
            ('script', None, silent),
        ):
            s.add(
                AgentSession(
                    kind=RESEARCH,
                    campaign_id=campaign.id,
                    state=AgentSessionState.RUNNING,
                    harness=harness,
                    model='m',
                    session_id=SESSION_ID if session_file is not None else None,
                    session_file=session_file,
                    runtime='subprocess',
                    handle={'pid': 1, 'start_time': 0, 'log_dir': str(log_dir)},
                    log_dir=str(log_dir),
                    reason='cron',
                    started_at=at(100),
                )
            )
        s.commit()
    assert [line for line in render(db, campaign).splitlines() if line.startswith('  - research#')] == snapshot(
        [
            '  - research#1 claude/m 20m, on cron: Starting both training runs (16 processes each).',
            '  - research#2 claude/m 20m, on cron: {"type":"item.completed"}',
            '  - research#3 codex/m 20m, on cron: Ran the evaluation; measurement pending.',
            '  - research#4 script/m 20m, on cron: —',
        ]
    )


def test_a_row_holding_a_blob_renders_as_a_placeholder_with_one_warning_naming_it(
    db: Database, campaign: Campaign, capsys: pytest.CaptureFixture[str]
) -> None:
    """A comment body stored as bytes renders as a placeholder and prints one warning. It does not crash."""
    with db.session() as s:
        s.add(Comment(target=TargetRef(TargetKind.CAMPAIGN, campaign.id), author='w', body='x', priority=1))
        s.commit()
    with db.engine.begin() as con:  # The CHECK constraints reject a blob, so turn them off to insert one.
        con.exec_driver_sql('PRAGMA ignore_check_constraints = ON')
        con.exec_driver_sql("UPDATE comments SET body = x'00ff' WHERE id = 1")
        con.exec_driver_sql('PRAGMA ignore_check_constraints = OFF')
    assert '- comment#1 note (p1, unacknowledged) on campaign#1 by w, 0s ago: <not text: a 2-byte blob>' in render(
        db, campaign
    )
    render(db, campaign)
    assert capsys.readouterr().err == (
        'piloop: warning: comments#1 body is a 2-byte blob, not text; shown as a placeholder. Repair it:'
        ' UPDATE comments SET body = CAST(body AS TEXT) WHERE id = 1\n'
    ), 'the warning prints once per process'
    with db.session() as s:
        s.get_one(Comment, 1).priority = 2
        s.commit()
    with db.engine.connect() as con:
        assert con.exec_driver_sql('SELECT typeof(body) FROM comments').scalar() == 'blob', 'never written back'


def test_progress_lists_only_the_configured_conditions(db: Database, campaign: Campaign) -> None:
    def config(stop: dict[str, object], direction: str = 'higher') -> CampaignConfig:
        metrics = [{'name': 'score', 'direction': direction, 'role': 'primary'}, {'name': 'size', 'direction': 'lower'}]
        return reconfigured(campaign.config, plugins={'stop_rule': stop}, metrics=metrics)

    with db.session() as s:
        p = s.get_one(Campaign, campaign.id)
        assert (
            overview_module.progress(s, p, config({'target': 0.5}, 'lower'), NOW)
            == 'run 0 · 0.0h · target ≤ 0.5 (head 1)'
        )
        assert overview_module.progress(s, p, config({}), NOW) == 'run 0 · 0.0h · no limit stops it: open-ended'
        s.get_one(Candidate, 1).verdict = None
        assert overview_module.progress(s, p, config({'target': 0.5}), NOW) == 'run 0 · 0.0h · target ≥ 0.5 (head —)'


def test_block_keeps_whole_lines_and_counts_the_rest() -> None:
    assert block('  \n') == '—'
    assert block('a\nb\n\nc') == 'a\nb\n\nc'
    assert block('a\n' + 'b' * 10 + '\nc\nd', 8) == 'a\n… (3 more lines)'
    assert block('x' * 500 + '\ny', 8) == 'xxxxxxx…\n… (1 more lines)'
    assert block('\n'.join(['r' * 79] * 30)) == '\n'.join(['r' * 79] * 10) + '\n… (20 more lines)'
    assert len(block('x' * 2000)) == LINKED_CHARS
