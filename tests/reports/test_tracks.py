"""Tests for how status, watch, the history and the research pack show a campaign with two tracks."""

from pathlib import Path

from inline_snapshot import snapshot

from piloop.actor import System
from piloop.api.client import socket_http
from piloop.api.contract import Selection
from piloop.config import CampaignConfig
from piloop.db.models import Campaign
from piloop.db.session import Database
from piloop.plugins.campaign_type.kit.backlog.ids import IdeaStatus
from piloop.plugins.campaign_type.kit.backlog.tables import Idea
from piloop.plugins.campaign_type.kit.candidates.ids import Outcome
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.evaluation.ids import TrackName
from piloop.plugins.campaign_type.kit.runs.tables import Run
from piloop.plugins.campaign_type.optimization.pack import pack
from piloop.plugins.campaign_type.optimization.summary import summary
from piloop.settings import Settings, WatchSettings
from piloop.watch.panels import Api, CandidatesPanel, Frame, campaigns, read_frame
from tests.conftest import has_lines, reconfigured, serving_api
from tests.reports.test_history import SHOWN, full
from tests.reports.test_overview import NOW, candidate, live_campaign, render

TRACKS = {
    'metrics': [
        {'name': 'score', 'direction': 'higher', 'role': 'primary', 'margin': 1, 'floor': 0.5},
        {'name': 'size', 'direction': 'lower', 'role': 'primary', 'margin': 2, 'floor': 20},
    ],
    'tracks': {'score': {'share': 0.75}, 'size': {'share': 0.25}},
}
"""Two tracks named after the metrics of the test campaign, so the ideas of ``campaign`` (on ``score``) still fit."""


def two_tracks(db: Database, campaign: Campaign) -> CampaignConfig:
    """Add ``campaign``, then a trunk keep on ``size``, and put the campaign on two tracks."""
    live_campaign(db, campaign)
    with db.session() as s:
        p = s.get_one(Campaign, campaign.id)
        cfg = reconfigured(p.config, **TRACKS)
        p.config = cfg
        lighter = Run(idea_id=3, attempt=2, base_candidate_id=2, harness='script', model='m')
        s.add(lighter)
        s.flush()
        s.get_one(Candidate, 1).measurements[0].metrics = {'score': 1.0, 'size': 12.0}
        kept = candidate(s, p, 2, Outcome.KEEP, 3, 60, run_id=lighter.id)
        kept.measurements[0].metrics = {'score': 3.0, 'size': 8.0}
        lighter.idea.track = TrackName('size')
        s.commit()
    return cfg


def test_status_names_every_track_its_head_its_floor_and_what_works_on_it(db: Database, campaign: Campaign) -> None:
    two_tracks(db, campaign)
    with db.session() as s:
        s.add(
            Idea(
                campaign_id=campaign.id,
                track=TrackName('size'),
                title='review me',
                hypothesis='h',
                status=IdeaStatus.PROPOSED,
                created_by='research#9',
            )
        )
        s.commit()
    text = render(db, campaign)
    has_lines(
        text,
        '## Goal',
        '- Tracks, each a primary its keeps improve while holding the others:'
        ' score (score, higher, margin 1, floor 0.5, share 0.75); size (size, lower, margin 2, floor 20, share 0.25)',
        '## Tracks',
        '- score: score 3 at the head (+2 on the start); floor 0.5, 2.5 to spare; 1 trunk keeps,'
        ' the latest candidate#2 1h ago; share 0.75 of 1 slots, 0 running; 0 runnable ideas, 0 proposed.',
        '- size: size 8 at the head (-4 on the start); floor 20, 12 to spare; 1 trunk keeps,'
        ' the latest candidate#4 1h ago; share 0.25 of 1 slots, 2 running; 0 runnable ideas, 1 proposed.',
        '## Trajectory',
        '- Start candidate#1, head candidate#4: score 1 → 3 (+2, +200.0%); size 12 → 8 (-4, -33.3%); 2 trunk keeps.',
        '  - 10-01 12:18 candidate#2 [score] score 3 (+2) idea#1: score 3',
        '  - 10-01 13:00 candidate#4 [size] size 8 (-2) idea#3: score 3',
        "## Recent candidates (4 of 4; each on its track's primary)",
        '| candidate#2 | keep | score | 3 | 2 | run#1 | 0s | score 3 |',
        '| candidate#4 | keep | size | 8 | -2 | run#4 | 0s | score 3 |',
    )
    assert 'head: candidate#4 · score: 3 · score vs baseline: 2 · size: 8 · size vs baseline: -4' in text


def test_status_of_a_campaign_with_no_candidate_says_there_is_no_baseline_yet(db: Database, campaign: Campaign) -> None:
    """A campaign added by hand, with SQL, before its baseline."""
    with db.session() as s:
        bare = Campaign(name='bare', path='/elsewhere', config=reconfigured(campaign.config, **TRACKS))
        s.add(bare)
        s.commit()
    shown = render(db, bare).split('## Tracks\n\n')[1].split('## Runs')[0]
    assert shown == snapshot("""\
- score: no baseline yet; floor 0.5; 0 trunk keeps; share 0.75 of 1 slots, 0 running; 0 runnable ideas, 0 proposed.
- size: no baseline yet; floor 20; 0 trunk keeps; share 0.25 of 1 slots, 0 running; 0 runnable ideas, 0 proposed.

## Trajectory

- No baseline yet.

""")


def test_history_and_pack_show_each_track(db: Database, campaign: Campaign) -> None:
    cfg = two_tracks(db, campaign)
    with db.session() as s:
        p = s.get_one(Campaign, campaign.id)
        text = full(s, p, cfg)
        packed = pack(s, p, cfg, workspace=None, shown=SHOWN)
    assert text.splitlines()[2] == snapshot(
        "| id | status    | track | by         | family | links                        | prio | title                 | best candidate (its track's primary)     |"
    )
    assert [line for line in text.splitlines() if line.startswith(('| candidate', '| #4'))] == snapshot(
        [
            "| candidate | parent | track | score | size | vs parent (its track's primary) | idea (by)           | revision | tokens | summary  |",
            '| #4        | #2     | size  | 3     | 8    | -2                              | idea#3 (research#4) | 0000060  | —      | score 3  |',
        ]
    )
    assert [line for line in packed.splitlines() if line.startswith('| metric')] == snapshot(
        ['| metric | role    | track | better | margin | floor | start | head | gain |']
    )
    assert [line for line in packed.splitlines() if line.startswith(('| score', '| size'))] == snapshot(
        [
            '| score  | primary | score | higher | 1      | 0.5   | 1     | 3    | +2   |',
            '| size   | primary | size  | lower  | 2      | 20    | 12    | 8    | +4   |',
        ]
    )


def test_watch_shows_the_head_per_track(db: Database, campaign: Campaign) -> None:
    two_tracks(db, campaign)
    cfg = Settings(home=Path(str(db.engine.url.database)).parent)
    with serving_api(cfg), socket_http(cfg.socket_file, System.ORCHESTRATOR) as http:
        api = Api.over(http)
        frame = read_frame(api, 'demo', NOW)
        states = api.core.status(Selection()).campaigns
    assert isinstance(frame, Frame)
    [row] = campaigns(states, {}, NOW)
    assert row.cells[2:4] == snapshot(('3 score, 8 size', '+2, -4'))
    with db.session() as s:
        p = s.get_one(Campaign, campaign.id)
        assert summary(s, p, p.config) == snapshot(
            'demo · head candidate#4: score 3 (+2), size 8 (-4) · 3 kept / 4 candidates · 4k tok'
        ), 'piloop wake shows it'
    panel = CandidatesPanel(WatchSettings())
    assert panel.title(frame, panel.items(frame)) == snapshot('Candidates score ▁██ 1 → 3 · size █▅▁ 12 → 8')


def test_lineages_show_every_track_primary_of_their_latest_keep(db: Database, campaign: Campaign) -> None:
    two_tracks(db, campaign)
    with db.session() as s:
        branch = Run(idea_id=1, attempt=2, base_candidate_id=1, harness='script', model='m', branch=True)
        s.add(branch)
        s.flush()
        candidate(s, s.get_one(Campaign, campaign.id), 1, Outcome.KEEP, 2, 70, run_id=branch.id)
        s.commit()
    lines = [
        line for line in render(db, campaign).splitlines() if line.startswith('  - ') and 'from candidate#' in line
    ]
    assert lines == snapshot(
        [
            '  - trunk from candidate#1 (keep): latest candidate#4 score 3, size 8, 3 kept',
            '  - branch from candidate#1 (keep): latest candidate#5 score 2, size 10, 1 kept',
        ]
    )


def test_a_keep_counts_on_the_track_of_its_verdict_not_of_its_run(db: Database, campaign: Campaign) -> None:
    """The daemon judges a candidate on every track: a score run's candidate kept on size is a size keep."""
    two_tracks(db, campaign)
    with db.session() as s:
        s.get_one(Candidate, 2).track = TrackName('size')
        s.commit()
    tracks = render(db, campaign).split('## Tracks')[1].split('## Trajectory')[0]
    assert '- score: score 3 at the head (+2 on the start); floor 0.5, 2.5 to spare; 0 trunk keeps;' in tracks
    assert '1h ago; share 0.75' not in tracks and '2 trunk keeps, the latest candidate#4 1h ago' in tracks
