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
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.evaluation.ids import TrackName
from piloop.plugins.campaign_type.kit.runs.tables import Run
from piloop.plugins.campaign_type.optimization.pack import pack
from piloop.plugins.campaign_type.optimization.summary import summary
from piloop.settings import Settings, WatchSettings
from piloop.watch.panels import Api, CandidatesPanel, Frame, campaigns, read_frame
from tests.conftest import has_lines, reconfigured, serving_api
from tests.reports.test_history import SHOWN, full
from tests.reports.test_overview import NOW, candidate, live_campaign, render, score_at

TRACKS = {
    'metrics': [
        {'name': 'score', 'direction': 'higher', 'role': 'primary', 'margin': 1, 'floor': 0.5},
        {'name': 'size', 'direction': 'lower', 'role': 'primary', 'margin': 2, 'floor': 20},
    ],
}
"""Two tracks named after the metrics of the test campaign, so the ideas of ``campaign`` (on ``score``) still fit."""


def two_tracks(db: Database, campaign: Campaign) -> CampaignConfig:
    """Add ``campaign``, put it on two tracks, then a new best on ``size``."""
    live_campaign(db, campaign)
    with db.session() as s:
        p = s.get_one(Campaign, campaign.id)
        cfg = reconfigured(p.config, **TRACKS)
        p.config = cfg
        lighter = Run(campaign_id=campaign.id, idea_id=3, attempt=2, base_candidate_id=2, harness='script', model='m')
        s.add(lighter)
        s.flush()
        s.get_one(Candidate, 1).measurements[0].metrics = {'score': 1.0, 'size': 12.0}
        s.get_one(Idea, 3).track = TrackName('size')
        lighter_one = candidate(s, p, 2, 3, 60, run_id=lighter.id)
        lighter_one.measurements[0].metrics = {'score': 3.0, 'size': 8.0}
        score_at(s, p, lighter_one, 60)
        s.commit()
    return cfg


def test_status_names_every_track_its_best_its_floor_and_what_works_on_it(db: Database, campaign: Campaign) -> None:
    two_tracks(db, campaign)
    with db.session() as s:
        s.add(
            Idea(
                campaign_id=campaign.id,
                track=TrackName('size'),
                title='try me',
                hypothesis='h',
                status=IdeaStatus.OPEN,
                created_by='research#9',
            )
        )
        s.commit()
    text = render(db, campaign)
    has_lines(
        text,
        '## Goal',
        '- Tracks, each a primary that its best improves while it holds the others:'
        ' score (score, higher, margin 1, floor 0.5); size (size, lower, margin 2, floor 20)',
        '## Tracks',
        '- score: score 3 at candidate#2 (+2 on the baseline); floor 0.5, 2.5 to spare; 1 new bests, the latest 1h ago;'
        ' 0 running; 1 open ideas.',
        '- size: size 8 at candidate#4 (+4 on the baseline); floor 20, 12 to spare; 1 new bests, the latest 1h ago;'
        ' 2 running; 1 open ideas.',
        '## Trajectory',
        '- Baseline candidate#1, best candidate#2: score 1 → 3 (+2, +200.0%); 1 new bests.',
        '  - 10-01 12:20 candidate#2 3 (+2) idea#1: score 3',
        "## Recent candidates (4 of 4; each on its track's primary)",
        '| candidate#2 | best | score | 3 | 2 | run#1 | 0s | score 3 |',
        '| candidate#3 | #2 of 4 | score | 3 | 0 | run#1 | 0s | score 3 |',
        '| candidate#4 | best | size | 8 | 2 | run#4 | 0s | score 3 |',
    )
    assert (
        'best of score: candidate#2 · score: 3 · score vs baseline: 2 · may be best on score: 4 ·'
        ' best of size: candidate#4 · size: 8 · size vs baseline: 4 · may be best on size: 4'
    ) in text, 'a gain is positive when better, a lower size too'
    assert 'candidate#4 3 (+0)' not in text, 'the trajectory follows the first track only'


def test_status_of_a_campaign_with_no_candidate_says_there_is_no_baseline_yet(db: Database, campaign: Campaign) -> None:
    """A campaign added by hand, with SQL, before its baseline."""
    with db.session() as s:
        bare = Campaign(name='bare', path='/elsewhere', config=reconfigured(campaign.config, **TRACKS))
        s.add(bare)
        s.commit()
    shown = render(db, bare).split('## Tracks\n\n')[1].split('## Runs')[0]
    assert shown == snapshot("""\
- score: no baseline yet; floor 0.5; 0 new bests; 0 running; 0 open ideas.
- size: no baseline yet; floor 20; 0 new bests; 0 running; 0 open ideas.

## Trajectory

- No baseline yet.

""")
    floorless = {**TRACKS, 'metrics': [TRACKS['metrics'][0], {**TRACKS['metrics'][1], 'floor': None}]}
    with db.session() as s:
        loose = Campaign(name='loose', path='/loose', config=reconfigured(campaign.config, **floorless))
        s.add(loose)
        s.commit()
    assert '- size: no baseline yet; 0 new bests; 0 running; 0 open ideas.' in render(db, loose), 'no floor to show'


def test_history_and_pack_show_each_track(db: Database, campaign: Campaign) -> None:
    cfg = two_tracks(db, campaign)
    with db.session() as s:
        p = s.get_one(Campaign, campaign.id)
        text = full(s, p, cfg)
        packed = pack(s, p, cfg, workspace=None, shown=SHOWN)
    assert text.splitlines()[2] == snapshot(
        "| id | status  | track | by         | links        | prio | title                 | best candidate (its track's primary) |"
    )
    assert [line for line in text.splitlines() if line.startswith(('| candidate', '| #4'))] == snapshot(
        [
            "| candidate | parent | track | score | size | vs parent (its track's primary) | idea (by)           | revision | tokens | summary  |",
            '| #4        | #2     | size  | 3     | 8    | -2                              | idea#3 (research#4) | 0000060  | —      | score 3  |',
        ]
    )
    assert [line for line in packed.splitlines() if line.startswith('| metric')] == snapshot(
        ['| metric | role    | track | better | margin | floor | start | best | gain |']
    )
    assert [line for line in packed.splitlines() if line.startswith(('| score', '| size'))] == snapshot(
        [
            '| score  | primary | score | higher | 1      | 0.5   | 1     | 3    | +2   |',
            '| size   | primary | size  | lower  | 2      | 20    | 12    | 10   | +2   |',
        ]
    )


def test_watch_shows_the_best_per_track(db: Database, campaign: Campaign) -> None:
    two_tracks(db, campaign)
    cfg = Settings(home=Path(str(db.engine.url.database)).parent)
    with serving_api(cfg), socket_http(cfg.socket_file, System.ORCHESTRATOR) as http:
        api = Api.over(http)
        frame = read_frame(api, 'demo', NOW)
        states = api.core.status(Selection()).campaigns
    assert isinstance(frame, Frame)
    [row] = campaigns(states, {}, NOW)
    assert row.cells[2:4] == snapshot(('3 score, 8 size', '+2, +4'))
    with db.session() as s:
        p = s.get_one(Campaign, campaign.id)
        assert summary(s, p, p.config) == snapshot(
            'demo · best score 3 (candidate#2, +2 vs baseline), size 8 (candidate#4, +4 vs baseline) · 2 new bests / 4 candidates · 4k tok'
        ), 'piloop wake shows it'
    panel = CandidatesPanel(WatchSettings())
    assert panel.title(frame, panel.items(frame)) == snapshot('Candidates score ▁█ 1 → 3 · size █▁ 12 → 10')
