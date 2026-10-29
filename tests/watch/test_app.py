"""``piloop watch``: the Textual app driven key by key against a real API on a fixture database, and its screens as text."""

import asyncio
import contextlib
import io
import os
import pty
import re
import subprocess
import sys
import threading
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from pathlib import Path

import httpx2 as httpx
import pytest
from inline_snapshot import snapshot
from rich.console import Console
from rich.text import Text
from sqlalchemy import func, select
from textual.pilot import Pilot
from textual.widgets import DataTable, HelpPanel
from textual.worker import Worker, WorkerCancelled, WorkerState

from piloop.actor import Actor, Agent, System
from piloop.api.client import Client, Refused, socket_http
from piloop.api.contract import Access
from piloop.api.views import Batch, Envelope, Subject
from piloop.campaign import ops
from piloop.db.events import emit
from piloop.db.models import AgentSession, AgentSessionState, Campaign, Comment, Event, UnknownEvent, utcnow
from piloop.db.session import Database
from piloop.ids import AgentSessionId, CampaignId, CampaignStatus, CommentId, CommentKind, TargetKind, TargetRef
from piloop.net import tokens
from piloop.plugins.campaign_type.kit.backlog import ops as backlog_ops
from piloop.plugins.campaign_type.kit.backlog.ids import IdeaId, IdeaStatus
from piloop.plugins.campaign_type.kit.backlog.tables import Idea
from piloop.plugins.campaign_type.kit.candidates.ids import Origin, Outcome
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.evaluation.tables import Measurement
from piloop.plugins.campaign_type.kit.runs.ids import IMPLEMENTER, RunEnd, RunStatus
from piloop.plugins.campaign_type.kit.runs.tables import Run
from piloop.settings import Settings, WatchSettings
from piloop.watch.app import CampaignScreen, CampaignsScreen, CandidatesTable, Follower, Lost, Shown, Started, Watch
from piloop.watch.panels import Api, Part, read_frame, sparkline, touched
from tests.campaign.test_inbox import news_lines
from tests.conftest import RESEARCH, WATCHDOG, reconfigured, register, serving_api, spent, store_config
from tests.reports.test_history import idea


def ago(**delta: float) -> datetime:
    return utcnow() - timedelta(**delta)


STOP = {'max_runs': 50, 'max_hours': 12, 'max_tokens': 5_000_000, 'plateau': 10, 'target': 10}
SIZE = (160, 44)
DEFAULTS = WatchSettings(notice=timedelta(hours=1))
"""The defaults, but a note stays: a test reads the notes after a slow run too."""
OPEN = frozenset({WorkerState.PENDING, WorkerState.RUNNING})
"""The states of a worker that has not ended."""


def screen_text(app: Watch) -> str:
    """The screen as plain text, as a terminal would show it."""
    width, height = app.size
    console = Console(
        width=width, height=height, file=io.StringIO(), record=True, color_system=None, legacy_windows=False
    )
    console.print(app.screen._compositor.render_update(full=True, screen_stack=app._background_screens))
    return '\n'.join(line.rstrip() for line in console.export_text().splitlines()) + '\n'


async def settle(pilot: Pilot[None]) -> None:
    """Wait until every read that the app started answered, and its answers are drawn.

    A worker hands its answer to its screen before it ends, so once the workers ended a pause draws the answers. A
    drawn answer can start a read of its own, such as the detail of the row that the cursor lands on, and a pushed
    screen starts its read when it mounts: then wait again. A read that failed goes again with the next batch of the
    feed: then wait for that batch.
    """
    app = pilot.app
    assert isinstance(app, Watch)
    while True:
        await pilot.pause()
        if any(not screen.is_mounted for screen in app.screen_stack):
            continue
        if live := [w for w in app.workers if w.state in OPEN]:
            await asyncio.wait([asyncio.ensure_future(ended(w)) for w in live])
        elif retries(app):
            await asyncio.to_thread(app.follower.read_after, app.follower.batches)
        elif not any(table._require_update_dimensions for table in app.screen.query(DataTable)):
            await pilot.pause()  # the repaint of what the last round drew and sized
            return  # else a table sizes its columns to its new rows when it is idle: pause again


def retries(app: Watch) -> bool:
    """Whether the app waits for the next batch of the feed to read again what failed: its start, or a screen's read."""
    match app.screen:
        case CampaignsScreen(failed=Exception()):
            return True
        case CampaignScreen() as screen if screen.unread or screen.undetailed is not None:
            return True
        case _:
            return not app.started and not app.follower.ended


async def ended(worker: Worker[object]) -> None:
    """Wait until ``worker`` ends. One that a newer read of its group cancelled ended too."""
    with contextlib.suppress(WorkerCancelled):
        await worker.wait()


async def fed(pilot: Pilot[None], app: Watch, db: Database) -> None:
    """Wait until the app's feed read past the newest event of ``db``, and the app read and drew what it touched."""
    with db.session() as s:
        newest = s.scalar(select(func.max(Event.id))) or 0
    await asyncio.to_thread(app.follower.past, newest)
    await settle(pilot)


def drive(
    db: Database,
    ref: str | None,
    scenario: Callable[[Pilot[None], Watch], Awaitable[None]],
    actor: Actor = System.HUMAN,
    settings: WatchSettings = DEFAULTS,
) -> Watch:
    """Serve the API of the fixture database, run the app headless against it, and play ``scenario``."""
    cfg = Settings(home=Path(str(db.engine.url.database)).parent)
    with serving_api(cfg) as servers, socket_http(cfg.socket_file, actor) as http:
        app = Watch(Api.over(http), ref, actor.signature, settings)

        async def run() -> None:
            async with app.run_test(size=SIZE) as pilot:
                await settle(pilot)
                await scenario(pilot, app)

        asyncio.run(run())
        # The follower's long poll answers once the server closes, and it must end before its client closes: on macOS a
        # read stays blocked for good when another thread closes its socket.
        for server in servers:
            server.close()
        app.follower.join()
    return app


def campaign_screen(app: Watch) -> CampaignScreen:
    screen = app.screen
    assert isinstance(screen, CampaignScreen)
    return screen


@pytest.fixture
def live_campaign(db: Database, campaign: Campaign, tmp_path: Path) -> Campaign:
    """A running campaign: kept candidates, two live agents, a queue, an inbox, a stopped twin."""
    log_dir = tmp_path / 'research-log'
    log_dir.mkdir()
    (log_dir / 'transcript.txt').write_text('reading the history\nproposing idea B\n')
    implementer_log = tmp_path / 'implementer-log'
    implementer_log.mkdir()
    (implementer_log / 'agent_session.stdout').write_text('hello from the agent\n')
    with db.session() as s:
        p = s.get_one(Campaign, campaign.id)
        p.config = reconfigured(p.config, plugins={'stop_rule': STOP})
        s.get_one(Candidate, 1).created_at = ago(hours=3)
        done = idea(s, p, 'more lines', IdeaStatus.DONE, created_at=ago(hours=3))
        run = Run(
            idea=done,
            attempt=1,
            base_candidate_id=1,
            harness='pi',
            model='m',
            status=RunStatus.SUCCEEDED,
            end=RunEnd.FINISHED,
            started_at=ago(hours=2),
            finished_at=ago(hours=1),
        )
        s.add(run)
        s.flush()
        for n, (verdict, value, minutes) in enumerate(
            [(Outcome.KEEP, 3, 90), (Outcome.DISCARD, 2, 70), (Outcome.KEEP, 4, 60)], start=2
        ):
            s.add(
                Candidate(
                    id=n,
                    campaign_id=p.id,
                    run_id=run.id,
                    parent_id=n - 1,
                    tree_sha=f't{n}',
                    revision='x',
                    summary=f'[wip] step {n}',
                    verdict=verdict,
                    created_at=ago(minutes=minutes),
                )
            )
            s.flush()
            s.add(Measurement(candidate_id=n, evaluator_id=1, metrics={'score': value, 'size': 1}, seconds=2))
        live = idea(s, p, 'longer lines', IdeaStatus.SCHEDULED, created_at=ago(minutes=20))
        live_run = Run(idea=live, attempt=1, base_candidate_id=4, harness='pi', model='m', status=RunStatus.RUNNING)
        s.add(live_run)
        s.flush()
        idea(s, p, 'shorter lines', IdeaStatus.READY, priority=1, created_at=ago(minutes=5))
        idea(s, p, 'soft voting', IdeaStatus.PROPOSED, priority=2, created_at=ago(minutes=4))
        s.add_all(
            [
                AgentSession(
                    kind=IMPLEMENTER,
                    campaign_id=p.id,
                    run_id=live_run.id,
                    harness='pi',
                    model='m',
                    runtime='subprocess',
                    handle={'pid': 1, 'start_time': 0, 'log_dir': str(implementer_log)},
                    state=AgentSessionState.RUNNING,
                    started_at=ago(minutes=3),
                ),
                AgentSession(
                    kind=RESEARCH,
                    campaign_id=p.id,
                    harness='claude',
                    model='opus',
                    state=AgentSessionState.STARTING,
                    reason='low_water: 1 ready < 10',
                    log_dir=str(log_dir),
                    started_at=ago(minutes=10, seconds=30),  # "10m" for 30 s: "40s" became "41s" on a loaded host
                ),
                AgentSession(
                    kind=WATCHDOG,
                    campaign_id=p.id,
                    state=AgentSessionState.EXITED,
                    usage=spent(1_234_567, 9),
                ),
            ]
        )
        comment = dict(author='watchdog#3', created_at=ago(minutes=30))
        s.add_all(
            [
                Comment(
                    target=TargetRef(TargetKind.RUN, run.id),
                    kind=CommentKind.ESCALATION,
                    priority=1,
                    acked_at=ago(minutes=20),
                    acked_by='pi',
                    body='keep #2?',
                    **comment,
                ),
                Comment(
                    target=TargetRef(TargetKind.RUN, run.id),
                    author='pi',
                    kind=CommentKind.DECISION,
                    priority=2,
                    body='Keep #2: the floor holds.\nDetails follow.',
                    created_at=ago(minutes=20),
                ),
                Comment(
                    target=TargetRef(TargetKind.CAMPAIGN, p.id),
                    kind=CommentKind.ESCALATION,
                    priority=1,
                    body='Codex cannot start: trust dialog. Environment or harness?\nSecond line.',
                    **comment,
                ),
                Comment(target=TargetRef(TargetKind.CAMPAIGN, p.id), kind=CommentKind.NOTE, body='a note', **comment),
                Comment(
                    target=TargetRef(TargetKind.RUN, run.id),
                    author='human',
                    priority=2,
                    body='Why keep it?\nRe: comment#2',
                    created_at=ago(minutes=10),
                ),
            ]
        )
        other = Campaign(
            name='other',
            path=str(tmp_path / 'other'),
            config=p.config,
            status=CampaignStatus.STOPPED,
            stopped_reason='max_runs: 50 runs, and more words than the column holds',
        )
        s.add(other)
        s.flush()
        s.add(Candidate(campaign_id=other.id, tree_sha='o1', revision='o'))
        s.commit()
        for e in s.scalars(select(Event)):  # trigger-written events are stamped to the second
            e.at = ago(minutes=1)
        s.commit()
    return campaign


def test_the_campaigns_screen_opens_a_campaign_and_esc_comes_back(db: Database, live_campaign: Campaign) -> None:
    shots: list[str] = []

    async def scenario(pilot: Pilot[None], app: Watch) -> None:
        shots.append(screen_text(app))
        await pilot.press('enter')
        await settle(pilot)
        shots.append(screen_text(app))
        await pilot.press('escape', 'down', 'enter')
        await settle(pilot)
        shots.append(screen_text(app))

    drive(db, None, scenario)
    assert shots[0].splitlines()[:5] == snapshot(
        [
            ' piloop: 2 campaigns',
            '',
            ' campaign  status                                    head     vs baseline  agents    ready    inbox    tokens    last event',
            ' demo      active                                    4 score  +3           2 agents  1 ready  2 inbox  1.2M tok  comment.note 1m ago',
            ' other     stopped: max_runs: 50 runs, and more wo…  —        —            0 agents  0 ready  0 inbox  —         —',
        ]
    )
    header, rest = shots[1].split('\n', 2)[:2], shots[1].split('\n', 2)[2]
    assert header[0] == ' demo · active'
    assert header[1].startswith(
        ' head: candidate#4 · score: 4 · score vs baseline: 3 · kept: 3 · candidates: 4 · tokens:'
    )
    assert rest == snapshot("""\
╭─ [1] Inbox 2 ────────────────────────────────────────────────────────────────────────╮╭─ [0] inbox ──────────────────────────────────────────────────────────╮
│ p1  escalation  watchdog#3  30m  Codex cannot start: trust dialog. Environment or har││ comment#3 escalation on campaign#1 by watchdog#3, 30m ago · p1,      │
│ p2  decision    pi          20m  Keep #2: the floor holds.                           ││ unacknowledged                                                       │
╰──────────────────────────────────────────────────────────────────────────────────────╯│                                                                      │
╭─ [2] Agents 2 ───────────────────────────────────────────────────────────────────────╮│ Codex cannot start: trust dialog. Environment or harness?            │
│ #1  implementer  running   pi/m         3m   idea#2 longer lines                     ││ Second line.                                                         │
│ #2  research     starting  claude/opus  10m  low_water: 1 ready < 10                 ││                                                                      │
╰──────────────────────────────────────────────────────────────────────────────────────╯│                                                                      │
╭─ [3] Ideas 1 proposed · 1 ready · 1 scheduled ───────────────────────────────────────╮│                                                                      │
│ P2  proposed   #4  soft voting    —                                                  ││                                                                      │
│ P1  ready      #3  shorter lines  —                                                  ││                                                                      │
│ P2  scheduled  #2  longer lines   —                                                  ││                                                                      │
╰──────────────────────────────────────────────────────────────────────────────────────╯│                                                                      │
╭─ [4] Candidates ▁▆█ 1 → 4 ───────────────────────────────────────────────────────────╮│                                                                      │
│ candidate#4  Kept             4  +2  1h  [wip] step 4                                ││                                                                      │
│ candidate#3  Discarded        2  -1  1h  [wip] step 3                                ││                                                                      │
│ candidate#2  Kept             3  +2  1h  [wip] step 2                                ││                                                                      │
│ candidate#1  Baseline · Kept  1  —   3h  baseline                                    ││                                                                      │
│                                                                                      ││                                                                      │
│                                                                                      ││                                                                      │
│                                                                                      ││                                                                      │
│                                                                                      ││                                                                      │
│                                                                                      ││                                                                      │
│                                                                                      ││                                                                      │
│                                                                                      ││                                                                      │
│                                                                                      ││                                                                      │
│                                                                                      ││                                                                      │
│                                                                                      ││                                                                      │
╰──────────────────────────────────────────────────────────────────────────────────────╯│                                                                      │
╭─ [5] Status 13 ──────────────────────────────────────────────────────────────────────╮│                                                                      │
│ Campaign                                                                             ││                                                                      │
│ Goal                                                                                 ││                                                                      │
│ Trajectory                                                                           ││                                                                      │
│ Runs                                                                                 ││                                                                      │
│ Queue                                                                                ││                                                                      │
│ Agents                                                                               ││                                                                      │
│ Evaluations                                                                          ││                                                                      │
│ Funnel                                                                             ▂▂││                                                                      │
│ Yield                                                                                ││                                                                      │
│ Recent candidates (4 of 4; score)                                                    ││                                                                      │
╰──────────────────────────────────────────────────────────────────────────────────────╯╰──────────────────────────────────────────────────────────────────────╯
 a ack  A ack all shown  r reply  / filter  esc campaigns  q quit  ? keys
""")
    assert shots[2].splitlines()[:2] == snapshot(
        [
            ' other · stopped: max_runs: 50 runs, and more words than the column holds',
            ' head: no trunk keep · kept: 0 · candidates: 1 · stopped because: max_runs: 50 runs, and more words than the column holds',
        ]
    )


def test_a_campaign_not_registered_yet_is_waited_for_and_shown_once_it_is(db: Database, campaign_path: Path) -> None:
    shots: list[str] = []

    async def scenario(pilot: Pilot[None], app: Watch) -> None:
        shots.append(screen_text(app).splitlines()[0])
        await pilot.press('slash', 'enter')
        await settle(pilot)
        registered = register(db, campaign_path)
        with db.session() as s:  # what the daemon's registration emits; the fixture's insert emits nothing
            target = TargetRef(TargetKind.CAMPAIGN, registered.id)
            emit(s, UnknownEvent(kind='campaign.registered', payload={}), target, System.DAEMON)
            s.commit()
        await fed(pilot, app, db)
        assert screen_text(app).startswith(' demo · active')

    drive(db, 'demo', scenario, settings=DEFAULTS.model_copy(update={'tail': 0}))
    assert shots == [" no campaign 'demo' yet: `piloop init` registers it, and this view follows"]


def test_a_campaign_whose_config_no_longer_validates_says_how_to_fix_it(db: Database, campaign: Campaign) -> None:
    store_config(db, campaign.id, {'name': campaign.name, 'metrics': 'no list'})
    shots: list[str] = []

    async def scenario(pilot: Pilot[None], app: Watch) -> None:
        shots.append(screen_text(app).splitlines()[3])
        await pilot.press('enter')
        await settle(pilot)
        shots.append(screen_text(app).splitlines()[0])

    drive(db, None, scenario)
    assert shots[0].startswith(' demo      active  —')
    assert shots[1].startswith(' demo: the stored config is not a valid piloop config')


def test_a_caller_that_may_not_act_as_its_actor_is_told_why(db: Database, campaign: Campaign) -> None:
    async def scenario(pilot: Pilot[None], app: Watch) -> None:
        assert [n.message for n in app._notifications] == [
            snapshot('the event feed: owner may not act as implementer#99: only the token of implementer#99 acts as it')
        ]

    app = drive(db, None, scenario, actor=Agent(role='implementer', session=AgentSessionId(99)))
    assert (app.started, app.follower.ended) == (False, True), 'a refusal ends the feed: reading again changes nothing'


def test_inbox_keys_acknowledge_one_item_or_every_item_shown(db: Database, live_campaign: Campaign) -> None:
    notes: list[str] = []

    async def scenario(pilot: Pilot[None], app: Watch) -> None:
        await pilot.press('down')
        await settle(pilot)
        assert 'Keep #2: the floor holds.' in screen_text(app).split('[0] inbox')[1], 'the detail follows the cursor'
        await pilot.press('a')
        await fed(pilot, app, db)
        assert '[1] Inbox 1 ' in screen_text(app), 'the ack came back on the feed'
        notes.append(campaign_screen(app).note)
        await pilot.press('A')
        await fed(pilot, app, db)
        assert '[1] Inbox 0 ' in screen_text(app)
        notes.append(campaign_screen(app).note)
        await pilot.press('a', 'A')

    drive(db, 'demo', scenario)
    assert notes == ['comment#2 acknowledged', 'comment#3 acknowledged']
    with db.session() as s:
        assert [(c.id, c.acked_by) for c in s.scalars(select(Comment).where(Comment.acked_at.is_not(None)))] == [
            (1, 'pi'),
            (2, 'human'),
            (3, 'human'),
        ]


def test_r_replies_to_the_thread_root_with_a_priority_that_wakes_the_orchestrator(
    db: Database, live_campaign: Campaign
) -> None:
    notes: list[str] = []

    async def scenario(pilot: Pilot[None], app: Watch) -> None:
        await pilot.press('down', 'r')
        await pilot.pause()
        await pilot.press(*'Revert it', 'tab', 'enter', 'down', 'enter', 'shift+tab', 'enter')
        await pilot.pause()
        notes.append(campaign_screen(app).note)
        await pilot.press('r', 'escape')
        await pilot.pause()
        notes.append(campaign_screen(app).note)
        await pilot.press('r', 'enter')
        await pilot.pause()
        notes.append(campaign_screen(app).note)

    with db.session() as s:
        cursor = s.scalar(select(func.max(Event.id))) or 0
    drive(db, 'demo', scenario)
    assert notes == ['sent comment#6 (p3) in reply to comment#2', 'reply dropped', 'reply dropped']
    with db.session() as s:
        sent = s.get_one(Comment, CommentId(6))
        assert (sent.target_kind, sent.target_id, sent.author, sent.kind, sent.priority, sent.body) == (
            TargetKind.COMMENT,
            2,
            'human',
            CommentKind.DECISION,
            3,
            'Revert it',
        )
        assert news_lines(s, cursor, campaign_id=live_campaign.id, me=System.ORCHESTRATOR)[1] == [
            'comment#6 decision p3 on comment#2 (demo) by human: Revert it'
        ]


def test_slash_filters_the_focused_panel_and_esc_clears_it(db: Database, live_campaign: Campaign) -> None:
    shots: list[str] = []

    async def scenario(pilot: Pilot[None], app: Watch) -> None:
        await pilot.press('slash', *'keep', 'enter')
        await settle(pilot)
        shots.append(screen_text(app))
        await pilot.press('slash', 'escape')
        await settle(pilot)
        shots.append(screen_text(app))

    drive(db, 'demo', scenario)
    assert '[1] Inbox 1 /keep' in shots[0] and 'Codex cannot start' not in shots[0].split('[2] Agents')[0]
    assert '[1] Inbox 2 ─' in shots[1]


def test_the_agents_panel_shows_the_selected_agents_output_and_its_state(db: Database, live_campaign: Campaign) -> None:
    shots: list[str] = []

    async def scenario(pilot: Pilot[None], app: Watch) -> None:
        await pilot.press('2')
        await settle(pilot)
        shots.append(screen_text(app).split('[0] agents')[1])
        await pilot.press('down')
        await settle(pilot)
        shots.append(screen_text(app).split('[0] agents')[1])

    drive(db, 'demo', scenario)
    assert [line.split('│')[-2].rstrip() for line in shots[0].splitlines()[1:6]] == snapshot(
        [
            ' implementer-1 · pi/m · running 3m · subprocess',
            ' on: idea#2 longer lines',
            '',
            ' the last 1 lines it wrote',
            ' hello from the agent',
        ]
    )
    assert 'proposing idea B' in shots[1], "a headless agent's saved transcript"


def test_s_steers_a_running_agent(db: Database, live_campaign: Campaign) -> None:
    notes: list[str] = []

    async def scenario(pilot: Pilot[None], app: Watch) -> None:
        async def note(*keys: str) -> None:
            await pilot.press(*keys)
            await settle(pilot)
            notes.append(campaign_screen(app).note)

        await note('2', 's', *'try the cheaper model', 'enter')
        await note('s', 'escape', 'down', 's', *'go', 'enter')

    drive(db, 'demo', scenario)
    with db.session() as s:
        steered = s.scalars(select(Comment).where(Comment.target_kind == TargetKind.AGENT_SESSION)).one()
        assert (steered.target_id, steered.kind, steered.body) == (1, CommentKind.NOTE, 'try the cheaper model')
    assert notes == [
        f'comment#{steered.id}: queued for agent_session#1; the daemon delivers it',
        'agent_session#2 is not running',
    ], 'the daemon delivers the note'


def test_a_paused_campaign_says_so_in_its_row_and_its_header(db: Database, live_campaign: Campaign) -> None:
    with db.session() as s:
        ops.pause(s, live_campaign.id, evals=False, reason='evaluator v2')
        s.commit()
    shots: list[str] = []

    async def scenario(pilot: Pilot[None], app: Watch) -> None:
        shots.append(screen_text(app))
        await pilot.press('enter')
        await settle(pilot)
        shots.append(screen_text(app))

    drive(db, None, scenario)
    assert shots[0].splitlines()[3].startswith(' demo      paused (launches): evaluator v2  ')
    assert shots[1].splitlines()[0] == ' demo · paused (launches): evaluator v2'


def test_the_candidates_are_the_lineage_s_nodes_with_their_detail(db: Database, live_campaign: Campaign) -> None:
    with db.session() as s:
        s.add(
            Candidate(
                campaign_id=live_campaign.id,
                tree_sha='r1',
                revision='r',
                summary='paper repro',
                origin=Origin.REFERENCE,
                created_at=utcnow() - timedelta(hours=2),  # a fixed age: "0s" became "1s" on a slow machine
            )
        )
        s.commit()
    shots: list[str] = []

    async def scenario(pilot: Pilot[None], app: Watch) -> None:
        await pilot.press('4')
        await settle(pilot)
        shots.append(screen_text(app))

    drive(db, 'demo', scenario)
    candidates = shots[0].split('[4] ')[1].splitlines()
    assert candidates[0].startswith('Candidates ▁▆█ 1 → 4 ─')
    assert [line.split('│')[1].rstrip() for line in candidates[1:6]] == snapshot(
        [
            ' candidate#4  Kept                    4  +2  1h  [wip] step 4',
            ' candidate#3  Discarded               2  -1  1h  [wip] step 3',
            ' candidate#2  Kept                    3  +2  1h  [wip] step 2',
            ' candidate#6  Reference · Not judged  —  —   2h  paper repro',
            ' candidate#1  Baseline · Kept         1  —   3h  baseline',
        ]
    )
    assert [
        line.split('│')[-2].rstrip() for line in shots[0].split('[0] candidates')[1].splitlines()[1:21]
    ] == snapshot(
        [
            ' candidate#4 more lines',
            ' Kept —',
            ' parent candidate#3 · 1h ago',
            '',
            ' scores under evaluator#1 (vs parent):',
            '   score (score) 4 (+2)',
            '',
            ' links:',
            '   candidate#4',
            '   idea#1 more lines',
            '   run#1',
            '',
            ' summary:',
            ' [wip] step 4',
            '',
            ' candidate#4 · revision x',
            ' metrics under evaluator#1 (vs parent):',
            '   score 4 (+2)',
            '   size 1 (+0)',
            '',
        ]
    )


def test_ideas_keys_approve_park_reject_with_a_reason_and_move_the_priority(
    db: Database, live_campaign: Campaign
) -> None:
    notes: list[str] = []

    async def scenario(pilot: Pilot[None], app: Watch) -> None:
        async def note(*keys: str, shown: str) -> None:
            await pilot.press(*keys)
            await fed(pilot, app, db)
            assert shown in screen_text(app).split('[4] ')[0]
            notes.append(campaign_screen(app).note)

        await note('3', 'y', *'worth a run', 'enter', shown='P2  ready      #4')
        await note('y', shown='P2  ready      #4')
        await note('p', 'escape', shown='P2  ready      #4')
        await note('p', *'later', 'enter', shown='P2  parked     #4')
        await note('plus', shown='P1  parked     #4')
        for shown in ('P2', 'P3', 'P4', 'P4'):
            await note('minus', shown=f'{shown}  parked     #4')
        await note('x', *'never', 'enter', shown='Ideas 1 ready · 1 scheduled ')

    drive(db, 'demo', scenario)
    assert notes == [
        'idea#4 proposed → ready',
        'idea#4 is ready already',
        'idea#4 left ready: no reason given',
        'idea#4 ready → parked',
        'idea#4 priority 1',
        'idea#4 priority 2',
        'idea#4 priority 3',
        'idea#4 priority 4',
        'idea#4 priority 4',
        'idea#4 parked → rejected',
    ]
    with db.session() as s:
        assert s.get_one(Idea, 4).status is IdeaStatus.REJECTED
        assert [c.body for c in s.scalars(select(Comment).where(Comment.target_kind == TargetKind.IDEA))] == [
            'proposed → ready: worth a run',
            'ready → parked: later',
            'parked → rejected: never',
        ]
        assert {e.actor for e in s.scalars(select(Event).where(Event.kind.in_(('idea.status', 'idea.priority'))))} == {
            'human'
        }


def test_a_review_that_the_api_refuses_says_why(db: Database, live_campaign: Campaign) -> None:
    with db.session() as s:
        seed = idea(s, s.get_one(Campaign, live_campaign.id), 'a hunch', IdeaStatus.SEED, created_by='watchdog#9')
        s.commit()
        seed_id = seed.id
    notes: list[str] = []

    async def scenario(pilot: Pilot[None], app: Watch) -> None:
        await pilot.press('3', 'y')
        await settle(pilot)
        await pilot.press(*'go', 'enter')
        await settle(pilot)
        notes.append(campaign_screen(app).note)
        await pilot.press('down', 'y')
        await settle(pilot)
        with db.session() as s:  # another client approves it meanwhile
            backlog_ops.set_idea_status(
                s, IdeaId(4), IdeaStatus.READY, reason='from the CLI', actor=System.ORCHESTRATOR
            )
            s.commit()
        await pilot.press(*'worth a run', 'enter')
        await settle(pilot)
        notes.append(campaign_screen(app).note)

    drive(db, 'demo', scenario)
    assert notes == [
        f'idea#{seed_id} is a seed: research defines it first (piloop idea develop {seed_id} …), or park or reject it',
        'idea#4 is ready already',
    ]


def test_the_ideas_detail(db: Database, live_campaign: Campaign) -> None:
    with db.session() as s:
        s.add(Comment(target=TargetRef(TargetKind.IDEA, 2), author='human', body='go on'))
        s.get_one(Idea, 2).plan = 'step one\nstep two'
        s.commit()
    shots: list[str] = []

    async def scenario(pilot: Pilot[None], app: Watch) -> None:
        await pilot.press('3', 'down', 'down')
        await settle(pilot)
        shots.append(screen_text(app).split('[0] ideas')[1])

    drive(db, 'demo', scenario)
    assert [line.split('│')[-2].rstrip() for line in shots[0].splitlines()[1:17]] == snapshot(
        [
            ' idea#2 longer lines',
            ' status scheduled · priority 2 · by t 20m ago · links: —',
            '',
            ' hypothesis:',
            ' longer lines helps',
            '',
            ' plan:',
            ' step one',
            ' step two',
            '',
            ' runs (1):',
            ' - run#2 running pi/m',
            '',
            ' comments (1):',
            ' - comment#6 note by human: go on',
            '',
        ]
    )


def test_the_status_panel_lists_the_overview_s_sections(db: Database, live_campaign: Campaign) -> None:
    shots: list[str] = []

    async def scenario(pilot: Pilot[None], app: Watch) -> None:
        await pilot.press('5', 'down')
        await settle(pilot)
        shots.append(screen_text(app))

    drive(db, 'demo', scenario)
    assert [line.split('│')[1].rstrip() for line in shots[0].split('[5] ')[1].splitlines()[1:4]] == snapshot(
        [' Campaign', ' Goal', ' Trajectory']
    )
    assert [line.split('│')[-2].rstrip() for line in shots[0].split('[0] status')[1].splitlines()[1:6]] == snapshot(
        [
            ' Goal',
            '',
            '  • Primary metric: score, higher is better; also measured: size',
            '    (lower, watch)',
            '  • Campaign: active. Progress: run 2/50 · 2.0h/12h · 1.2M/5.0M tok ·',
        ]
    )


def test_question_mark_shows_the_panels_keys_and_0_focuses_the_detail(db: Database, live_campaign: Campaign) -> None:
    async def scenario(pilot: Pilot[None], app: Watch) -> None:
        await pilot.press('question_mark')
        await pilot.pause()
        assert app.screen.query(HelpPanel)
        await pilot.press('question_mark')
        await pilot.pause()
        assert not app.screen.query(HelpPanel)
        await pilot.press('4', '0')
        await settle(pilot)
        assert app.focused is not None and app.focused.id == 'main'
        assert '[0] candidates' in screen_text(app), 'the detail keeps the last panel focused'
        await pilot.press('q')

    drive(db, 'demo', scenario)


def test_an_empty_campaign_takes_every_key_without_acting(db: Database, live_campaign: Campaign) -> None:
    async def scenario(pilot: Pilot[None], app: Watch) -> None:
        assert isinstance(app.focused, CandidatesTable), 'an empty inbox does not take the focus'
        await pilot.press('1', 'a', 'A', 'r', '2', 'enter', 's', '3', 'y', 'plus')
        await settle(pilot)
        assert campaign_screen(app).note == ''
        assert ' nothing here' in screen_text(app)

    drive(db, 'other', scenario)


def test_the_screen_follows_the_feed_and_a_dialog_holds_it_still(db: Database, live_campaign: Campaign) -> None:
    def write(body: str) -> None:
        with db.session() as s:
            s.add(Comment(target=TargetRef(TargetKind.CAMPAIGN, live_campaign.id), author='w', priority=0, body=body))
            s.commit()

    async def scenario(pilot: Pilot[None], app: Watch) -> None:
        write('new')
        await fed(pilot, app, db)
        assert '[1] Inbox 3 ' in screen_text(app), "another client's write"
        await pilot.press('r')
        await pilot.pause()
        write('newer')
        await fed(pilot, app, db)
        await pilot.press('escape')
        await settle(pilot)
        assert '[1] Inbox 3 ' in screen_text(app), 'under a dialog the screen does not read'
        await pilot.press('escape')
        await settle(pilot)
        assert 'piloop: 2 campaigns' in screen_text(app)
        write('newest')
        await fed(pilot, app, db)
        assert 'comment.note 0s ago' in screen_text(app), 'the campaigns screen follows too'

    drive(db, 'demo', scenario)


def test_an_event_of_another_campaign_draws_again_and_one_of_the_home_reads_again(
    db: Database, live_campaign: Campaign
) -> None:
    async def scenario(pilot: Pilot[None], app: Watch) -> None:
        screen = campaign_screen(app)
        for target in (TargetRef(TargetKind.CAMPAIGN, 2), TargetRef(TargetKind.HOME, 1)):
            with db.session() as s:
                emit(s, UnknownEvent(kind='noise', payload={}), target, System.DAEMON)
                s.commit()
            await fed(pilot, app, db)
        screen.detailed(Shown('inbox', 'stale', None), Text('an answer for a row the cursor left'))
        assert 'an answer for' not in screen_text(app)
        app.post_message(Lost(httpx.ConnectError('the server went away')))
        await settle(pilot)
        assert 'the server went away; reading again soon' in [n.message for n in app._notifications][-1]

    drive(db, 'demo', scenario)


def test_an_event_touches_the_parts_of_its_row_s_kind_on_the_screen_of_its_campaign() -> None:
    def batch(*subjects: tuple[TargetKind, CampaignId | None]) -> Batch:
        events = [
            Envelope(id=n, kind='k', subject=Subject(kind=kind, id=1, campaign=c), actor='a', time=None, payload={})
            for n, (kind, c) in enumerate(subjects, start=1)
        ]
        return Batch(events=events, next=len(events), more=False)

    mine = CampaignId(1)
    assert touched(batch((TargetKind.COMMENT, CampaignId(2))), mine) is None, 'another campaign: draw again'
    assert touched(batch((TargetKind.HOME, None)), mine) == frozenset(), 'the state and the overview'
    assert touched(batch((TargetKind.COMMENT, mine), (TargetKind.MEASUREMENT, mine)), mine) == {
        Part.INBOX,
        Part.LINEAGE,
    }
    assert touched(batch((TargetKind.CAMPAIGN, mine)), mine) == frozenset(Part)


def test_the_follower_says_when_the_feed_fails_and_reads_again_from_its_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[str] = []

    def answer(request: httpx.Request) -> httpx.Response:
        asked.append(request.url.params['after'])
        if len(asked) == 1:
            raise httpx.ConnectError('refused')
        follower.stop.set()  # the app closes while the read is out
        return httpx.Response(200, json={'protocol': 1, 'events': [], 'next': 8, 'more': False})

    http = httpx.Client(transport=httpx.MockTransport(answer), base_url='http://piloop')
    app = Watch(Api.over(http), None, 'human', WatchSettings(retry=timedelta(milliseconds=1)))
    posted: list[object] = []
    monkeypatch.setattr(app, 'post_message', posted.append)
    follower = Follower(app)
    follower.after = 7
    follower.run()
    assert asked == ['7', '7'] and [type(m).__name__ for m in posted] == ['Lost'], 'a batch after the stop is dropped'
    assert follower.after == 8


def test_a_read_that_fails_once_the_app_closed_ends_the_follower_quietly(monkeypatch: pytest.MonkeyPatch) -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        follower.stop.set()
        raise httpx.ReadError('the app closed the client')

    http = httpx.Client(transport=httpx.MockTransport(answer), base_url='http://piloop')
    app = Watch(Api.over(http), None, 'human', DEFAULTS)
    posted: list[object] = []
    monkeypatch.setattr(app, 'post_message', posted.append)
    follower = Follower(app)
    follower.after = 7
    follower.run()
    assert posted == []


def test_the_follower_reads_its_start_again_until_it_answers_and_a_refusal_ends_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list[str] = []
    status = {'protocol': 1, 'cursor': 3, 'daemon': {'pid': 1, 'started': '2026-10-29T00:00:00Z', 'listen': None}}

    def answer(request: httpx.Request) -> httpx.Response:
        asked.append(request.url.path)
        match len(asked):
            case 1:
                raise httpx.ConnectError('the daemon restarts')
            case 2:
                return httpx.Response(200, json={**status, 'campaigns': []})
            case 3:
                return httpx.Response(200, json={'protocol': 1, 'events': [], 'next': 3, 'more': False})
            case _:
                return httpx.Response(409, json={'error': 'cursor_ahead', 'detail': 'cursor 3 is ahead of the feed'})

    http = httpx.Client(transport=httpx.MockTransport(answer), base_url='http://piloop')
    app = Watch(Api.over(http), None, 'human', WatchSettings(retry=timedelta(milliseconds=1), tail=10))
    posted: list[object] = []
    monkeypatch.setattr(app, 'post_message', posted.append)
    app.follower.run()
    assert asked == ['/v1/status', '/v1/status', '/v1/events', '/v1/events']
    assert [type(m).__name__ for m in posted] == ['Lost', 'Started', 'Lost'] and app.follower.ended
    started = posted[1]
    assert isinstance(started, Started) and (started.cursor, app.follower.after, app.follower.batches) == (3, 3, 1)
    app.follower.read_after(5)
    app.follower.past(99)  # an ended follower holds no waiter


def test_a_read_that_fails_is_read_again_with_the_next_batch_of_the_feed(
    db: Database, live_campaign: Campaign, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A server that stalls past the client's timeout fails a read: the screen says so, then reads it again."""
    calls: dict[str, int] = {}

    def once(name: str, nth: int) -> None:
        method = getattr(Client, name)

        def flaky(self: Client, *args: object) -> object:
            calls[name] = calls.get(name, 0) + 1
            if calls[name] == nth:
                raise httpx.ReadTimeout('the server stalled')
            return method(self, *args)

        monkeypatch.setattr(Client, name, flaky)

    once('status', 2)  # the campaigns screen's: the follower's start reads the first
    once('overview', 1)
    once('thread', 1)

    async def scenario(pilot: Pilot[None], app: Watch) -> None:
        assert '[1] Inbox 2 ' in screen_text(app) and 'Codex cannot start' in screen_text(app).split('[0] inbox')[1]
        await pilot.press('escape')
        await settle(pilot)
        assert 'demo      active' in screen_text(app)

    drive(db, 'demo', scenario, settings=DEFAULTS.model_copy(update={'wait': timedelta(milliseconds=200)}))
    assert {name: n > 1 for name, n in calls.items()} == {'status': True, 'overview': True, 'thread': True}, (
        'each read that failed went again'
    )


def test_the_campaigns_screen_without_a_campaign_opens_nothing(db: Database) -> None:
    async def scenario(pilot: Pilot[None], app: Watch) -> None:
        await pilot.press('enter')
        await pilot.pause()
        assert isinstance(app.screen, CampaignsScreen) and 'piloop: 0 campaigns' in screen_text(app)

    drive(db, None, scenario)


@pytest.mark.parametrize('remote', [False, True], ids=['socket', 'PILOOP_SERVER'])
def test_a_reply_typed_in_the_watch_wakes_the_orchestrators_inbox_wait(
    db: Database, campaign: Campaign, tmp_path: Path, remote: bool
) -> None:
    """End to end: `piloop watch` in a terminal, `piloop inbox wait` as the orchestrator.

    On the home's socket, and from another machine: a home of its own with no database, through PILOOP_SERVER.
    """
    with serving_api(Settings(home=tmp_path), listen='127.0.0.1:0') as [_, tcp]:
        env: dict[str, str] = {**os.environ, 'PILOOP_HOME': str(tmp_path), 'HOME': str(tmp_path)}
        if remote:
            with db.session() as s:
                secret = tokens.create(s, 'laptop', Access.ACT)
                s.commit()
            laptop = tmp_path / 'laptop'
            laptop.mkdir(mode=0o700)
            env |= {'PILOOP_HOME': str(laptop), 'PILOOP_SERVER': tcp.url, 'PILOOP_HTTPS_TOKEN': secret}
        out = _reply_wakes_the_wait(db, campaign, env)
    author = 'human:laptop' if remote else 'human'
    assert out.splitlines()[0] == f'comment#2 decision p2 on comment#1 (demo) by {author}: Use the cheaper model'


def _reply_wakes_the_wait(db: Database, campaign: Campaign, env: dict[str, str]) -> str:
    with db.session() as s:
        s.add(
            Comment(
                target=TargetRef(TargetKind.CAMPAIGN, campaign.id),
                author='watchdog#3',
                kind=CommentKind.ESCALATION,
                priority=1,
                body='Codex cannot start: trust dialog.',
            )
        )
        s.commit()
        cursor = s.scalar(select(func.max(Event.id)))
    env = {**env, 'TERM': 'xterm-256color'}
    env.pop('PILOOP_ACTOR', None)
    piloop = [sys.executable, '-m', 'piloop']
    controller, terminal = pty.openpty()
    watching = subprocess.Popen([*piloop, 'watch', 'demo'], env=env, stdin=terminal, stdout=terminal, stderr=terminal)
    # The child has the terminal now. On macOS a close of the terminal side blocks while its output waits to be read,
    # so the parent closes its copy at once, while the reader thread still reads the controller.
    os.close(terminal)
    screen = Terminal(controller)
    try:
        screen.wait_for('Inbox')
        os.write(controller, b'r')
        screen.wait_for('enter sends it')  # the reply box is open, and takes the keys
        os.write(controller, b'Use the cheaper model\r')
        db.wait_until(lambda s: s.get(Comment, 2) is not None)
        os.write(controller, b'q')
        assert watching.wait() == 0
        waiting = subprocess.run(
            [*piloop, 'inbox', 'wait', 'demo', '--after', str(cursor), '--timeout', '60'],
            env={**env, 'PILOOP_ACTOR': 'orchestrator'},
            capture_output=True,
            text=True,
            check=True,
        )
    finally:
        watching.kill()
        os.close(controller)
    return waiting.stdout


class Terminal:
    """What a program drew on a terminal, read in a thread: ``wait_for`` blocks until a text shows."""

    def __init__(self, controller: int) -> None:
        self._shown, self._changed, self._ended = '', threading.Condition(), False
        threading.Thread(target=self._read, args=(controller,), daemon=True).start()

    def _read(self, fd: int) -> None:
        while True:
            try:
                chunk = os.read(fd, 65536)
            except OSError:  # the terminal closed under the reader: the test is over
                chunk = b''
            with self._changed:
                self._shown += ANSI.sub('', chunk.decode(errors='replace'))
                self._ended = not chunk
                self._changed.notify_all()
            if not chunk:
                return

    def wait_for(self, text: str) -> None:
        """Block until ``text`` shows, or the program closed the terminal without it, which fails."""
        with self._changed:
            self._changed.wait_for(lambda: text in self._shown or self._ended)
            assert text in self._shown, f'the program closed the terminal without showing {text!r}'


ANSI = re.compile(r'\x1b(\[[0-9;?]*[ -/]*[@-~]|\][^\x07]*\x07|[()][0-9A-Za-z])')
"""An escape sequence of a terminal: what a program sends to move the cursor or style the text, not text."""


def test_the_sparkline_draws_the_spine_s_steps() -> None:
    assert (sparkline([1.0, 3.0, 4.0]), sparkline([])) == ('▁▆█ 1 → 4', '—')


def test_a_refusal_other_than_an_unknown_campaign_is_not_taken_for_one(db: Database, campaign: Campaign) -> None:
    cfg = Settings(home=Path(str(db.engine.url.database)).parent)
    posing = Agent(role='implementer', session=AgentSessionId(99))
    with serving_api(cfg), socket_http(cfg.socket_file, posing) as http, pytest.raises(Refused, match='may not act'):
        read_frame(Api.over(http), 'demo', utcnow())
