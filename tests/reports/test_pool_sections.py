"""Tests for the sections of the pool as ``piloop status`` shows them: the headline, the tracks, the trajectory and the
recent candidates."""

from pathlib import Path

from inline_snapshot import snapshot

from piloop.config import CampaignConfig
from piloop.db.models import Campaign, Pause
from piloop.db.session import Database
from piloop.plugins.campaign_type.kit.candidates.ids import Origin
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.evaluation.tables import Measurement
from piloop.plugins.campaign_type.kit.pool.section.trajectory import STEPS_SHOWN
from piloop.plugins.section.contract import SECTIONS, Rendering, markdown
from tests.conftest import cas_of, reconfigured
from tests.reports.test_overview import NOW, candidate, score_at


def shown(db: Database, campaign: Campaign, name: str, cfg: CampaignConfig | None = None) -> str:
    """The section ``name`` as ``piloop status`` shows it, under ``cfg`` if set."""
    with db.session() as s:
        p = s.get_one(Campaign, campaign.id)
        if cfg is not None:
            p.config = cfg  # rolled back with the session
        return markdown(
            [SECTIONS.validated({'provider': name})], Rendering(s, NOW, p, Path('/no/workspace'), cas_of(s))
        )


def test_each_recent_candidate_says_where_it_stands_in_the_pool(db: Database, campaign: Campaign) -> None:
    """Best, ranked, refused by the best rule, excluded, unconfirmed, crashed, and one that is never scored."""
    metrics = [
        {'name': 'score', 'direction': 'higher', 'role': 'primary'},
        {'name': 'size', 'direction': 'lower', 'role': 'guardrail', 'margin': 6},
    ]
    cfg = reconfigured(campaign.config, metrics=metrics, evaluator={'noise_runs': 2})
    with db.session() as s:
        p = s.get_one(Campaign, campaign.id)
        p.config = cfg
        for value, when in ((3, 9), (2, 10)):
            c = candidate(s, p, 1, value, when)
            s.add(Measurement(candidate_id=c.id, evaluator_id=p.evaluator_id, metrics={'score': value, 'size': 10.0}))
        refused = candidate(s, p, 1, 4, 11)
        s.add(Measurement(candidate_id=refused.id, evaluator_id=p.evaluator_id, metrics={'score': 4, 'size': 14.0}))
        candidate(s, p, 2, 5, 12, excluded_reason='read the answers', excluded_by='human:t')
        candidate(s, p, 2, 9, 13)
        crashed = Candidate(campaign_id=p.id, parent_id=2, tree_sha='c', revision='c', summary='crashed')
        s.add(crashed)
        s.flush()
        s.add(Measurement(candidate_id=crashed.id, evaluator_id=p.evaluator_id, metrics={}, error='died'))
        s.add(Candidate(campaign_id=p.id, parent_id=2, tree_sha='k', revision='k', origin=Origin.SKIPPED))
        s.commit()
    assert shown(db, campaign, 'candidates') == snapshot("""\
## Recent candidates (8 of 8; score)

| candidate | standing | score | vs parent | run | age | summary |
|---|---|---|---|---|---|---|
| candidate#1 | #3 of 3 | 1 | — | no run | 0s | baseline |
| candidate#2 | best | 3 | 2 | no run | 0s | score 3 |
| candidate#3 | #2 of 3 | 2 | 1 | no run | 0s | score 2 |
| candidate#4 | refused | 4 | 3 | no run | 0s | score 4 |
| candidate#5 | excluded | 5 | 2 | no run | 0s | score 5 |
| candidate#6 | 1 eval | 9 | 6 | no run | 0s | score 9 |
| candidate#7 | crash | — | — | no run | 0s | crashed |
| candidate#8 | skipped | — | — | no run | 0s | — |\
""")


def test_the_ladder_and_the_track_of_each_candidate_have_columns(db: Database, campaign: Campaign) -> None:
    metrics = [
        {'name': 'score', 'direction': 'higher', 'role': 'primary', 'margin': 1},
        {'name': 'size', 'direction': 'lower', 'role': 'primary', 'margin': 2},
    ]
    ladder = [{'name': 'screen', 'stop': {'score': {'margin': 0.1}}}]
    cfg = reconfigured(campaign.config, metrics=metrics, ladder=ladder)
    with db.session() as s:
        candidate(s, s.get_one(Campaign, campaign.id), 1, 2, 10)
        s.commit()
    assert shown(db, campaign, 'candidates', cfg) == snapshot("""\
## Recent candidates (2 of 2; each on its track's primary)

| candidate | standing | track | score | vs parent | ladder | run | age | summary |
|---|---|---|---|---|---|---|---|---|
| candidate#1 | best | score | 1 | — | — | no run | 0s | baseline |
| candidate#2 | refused | score | 2 | 1 | — | no run | 0s | score 2 |\
""")


def test_the_trajectory_lists_the_latest_new_bests_with_the_gain_over_the_one_before(
    db: Database, campaign: Campaign
) -> None:
    with db.session() as s:
        p = s.get_one(Campaign, campaign.id)
        parent = 1
        for n in range(STEPS_SHOWN + 2):
            c = candidate(s, p, parent, 2 + n, 10 + n)
            score_at(s, p, c, 10 + n)
            parent = c.id
        reference = candidate(s, p, 1, 1.5, 30, origin=Origin.REFERENCE)
        score_at(s, p, reference, 30)
        s.commit()
    text = shown(db, campaign, 'trajectory')
    assert text == snapshot("""\
## Trajectory

- Baseline candidate#1, best candidate#11: score 1 → 11 (+10, +1000.0%); 10 new bests.
- Last new best 1h ago; 0 runs finished since.
- New bests (latest 8; the gain over the one before):
  - 10-01 12:12 candidate#4 4 (+1): score 4
  - 10-01 12:13 candidate#5 5 (+1): score 5
  - 10-01 12:14 candidate#6 6 (+1): score 6
  - 10-01 12:15 candidate#7 7 (+1): score 7
  - 10-01 12:16 candidate#8 8 (+1): score 8
  - 10-01 12:17 candidate#9 9 (+1): score 9
  - 10-01 12:18 candidate#10 10 (+1): score 10
  - 10-01 12:19 candidate#11 11 (+1): score 11
- Other lines (their best, against the best):
  - candidate#12 1.5 (-9.5): score 1.5\
""")


def test_the_trajectory_of_a_baseline_without_a_score(db: Database, campaign: Campaign) -> None:
    with db.session() as s:
        s.delete(s.get_one(Measurement, 1))
        s.commit()
    assert shown(db, campaign, 'trajectory') == snapshot("""\
## Trajectory

- Baseline candidate#1, best candidate#1: score unmeasured; 0 new bests.\
""")
    with db.session() as s:
        p = s.get_one(Campaign, campaign.id)
        score_at(s, p, candidate(s, p, 1, 2, 10, origin=Origin.REFERENCE), 10)
        s.commit()
    assert shown(db, campaign, 'trajectory') == snapshot("""\
## Trajectory

- Baseline candidate#1, best candidate#2: score unmeasured; 1 new bests.
- Last new best 1h ago; 0 runs finished since.
- New bests (latest 8; the gain over the one before):
  - 10-01 12:10 candidate#2 2: score 2\
"""), 'no gain over a baseline without a score'


def test_a_baseline_that_scored_zero_has_no_relative_gain(db: Database, campaign: Campaign) -> None:
    with db.session() as s:
        s.get_one(Measurement, 1).metrics = {'score': 0.0, 'size': 10.0}
        p = s.get_one(Campaign, campaign.id)
        score_at(s, p, candidate(s, p, 1, 2, 10), 10)
        s.commit()
    assert shown(db, campaign, 'trajectory').splitlines()[2] == snapshot(
        '- Baseline candidate#1, best candidate#2: score 0 → 2 (+2); 1 new bests.'
    )


def test_the_headline_names_the_best_of_each_track_the_starting_points_and_why_the_campaign_is_held(
    db: Database, campaign: Campaign
) -> None:
    metrics = [
        {'name': 'score', 'direction': 'higher', 'role': 'primary', 'margin': 1},
        {'name': 'size', 'direction': 'lower', 'role': 'primary', 'margin': 2},
    ]
    cfg = reconfigured(campaign.config, metrics=metrics)
    with db.session() as s:
        p = s.get_one(Campaign, campaign.id)
        candidate(s, p, 1, 3, 10, origin=Origin.REFERENCE)
        p.paused, p.paused_reason, p.paused_by = Pause.LAUNCHES, 'budget review', 'human'
        s.commit()
    assert shown(db, campaign, 'headline', cfg) == snapshot("""\
## Campaign

best of score: candidate#1 · score: 1 · may be best on score: 1 · best of size: candidate#1 · size: 5 · may be best on size: 2 · candidates: 1 · starting points: 1 · evaluator: #1, 0s old · paused: launches · paused by: human · paused because: budget review\
""")
