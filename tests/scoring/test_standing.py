"""The arithmetic of the pool: who may be best, the noise, the line of a candidate, and what its worker hears.

These build a ``Pool`` from its entries, without a database: ``tests/daemon/test_pool.py`` scores real submissions.
"""

from statistics import fmean

from piloop.config import CampaignConfig
from piloop.plugins.campaign_type.kit.candidates.ids import CandidateId, Origin
from piloop.plugins.campaign_type.kit.evaluation.config import Metric, MetricRole
from piloop.plugins.campaign_type.kit.evaluation.ids import TrackName
from piloop.plugins.campaign_type.kit.pool.best_rule.baseline import Baseline
from piloop.plugins.campaign_type.kit.pool.pool import Entry, Pool
from piloop.plugins.metric import Direction

MODELS = {'harness': {'claude': {'model': 'm'}, 'codex': {'model': 'm'}}}
C = CandidateId
"""Short for the candidates of these pools: candidate#1 is always the baseline."""


def config(*metrics: Metric) -> CampaignConfig:
    return CampaignConfig(name='v', metrics=list(metrics), evaluate=['true'], plugins=MODELS)


SCORED = config(Metric(name='score', direction=Direction.HIGHER, role=MetricRole.PRIMARY))


def entry(
    c: int,
    parent: int | None,
    samples: dict[str, list[float]],
    *,
    excluded: str | None = None,
    failed: int = 0,
    stopped: str | None = None,
    sealed: float | None = None,
) -> Entry:
    """Candidate ``c``, a child of ``parent``, with one successful evaluation per value of each list of ``samples``."""
    return Entry(
        id=C(c),
        parent=None if parent is None else C(parent),
        origin=Origin.BASELINE if parent is None else Origin.RUN,
        excluded=excluded,
        means={name: fmean(values) for name, values in samples.items() if values},
        samples=samples,
        measured=max((len(v) for v in samples.values()), default=0),
        failed=failed,
        stopped=stopped,
        sealed=sealed,
    )


def pool(*entries: Entry, cfg: CampaignConfig = SCORED, track: str | None = None, confirm: int = 1) -> Pool:
    """The pool of ``track`` (the first by default) of ``entries``, whose first is the baseline."""
    return Pool(
        track=cfg.track(TrackName(track)) if track is not None else cfg.all_tracks[0],
        cfg=cfg,
        entries={e.id: e for e in entries},
        baseline=entries[0].id if entries else None,
        confirm=confirm,
        rule=Baseline(),
        sealed=False,
    )


def score(*values: float) -> dict[str, list[float]]:
    return {'score': list(values)}


def test_sigma_is_the_spread_of_the_best_s_own_evaluations_and_0_when_one_confirms() -> None:
    assert pool(entry(1, None, score(10, 12, 14)), confirm=3).sigma() == 2.0
    assert pool(entry(1, None, score(10)), confirm=3).sigma() is None, 'one evaluation has no spread'
    assert pool(entry(1, None, score(10, 12, 14)), confirm=1).sigma() == 0.0, 'an evaluator whose runs agree'
    assert pool(entry(1, None, {}), confirm=3).sigma() is None, 'a baseline without a score'
    assert pool(confirm=3).sigma() is None, 'no baseline yet'


def test_a_delta_within_twice_the_noise_of_the_difference_is_labelled_within_noise() -> None:
    """σ is 2. A delta of one evaluation against the best's three is within noise up to 2·2·√(1 + 1/3) = 4.62."""
    found = pool(entry(1, None, score(10, 12, 14)), entry(2, 1, score(11)), entry(3, 1, score(20)), confirm=3)
    close, far = found.standing(C(2)), found.standing(C(3))
    assert {'vs_parent', 'vs_best'} <= set(close.within)
    assert (
        close.told() == 'score 11 over 1 eval; vs parent -1 (within noise), vs best candidate#1 -1 (within noise) (σ 2)'
    )
    assert far.within == ()
    assert far.told() == 'score 20 over 1 eval; vs parent +8, vs best candidate#1 +8 (σ 2)', 'not confirmed: no rank'
    exact = pool(entry(1, None, score(10, 12, 14)), entry(2, 1, score(12)), confirm=1).standing(C(2))
    assert (
        exact.told()
        == 'score 12 over 1 eval: rank 2 of 2; vs parent +0 (within noise), vs best candidate#1 +0 (within noise)'
    ), 'σ 0: only no change is within it, and no σ is shown'


def test_the_best_of_a_line_is_the_best_ranked_ancestor_and_is_told_apart_from_the_parent_and_the_best() -> None:
    """1 → 2 → 3 → 4, and 1 → 5. The best is 5; the best of the line of 4 is 2."""
    found = pool(
        entry(1, None, score(5)),
        entry(2, 1, score(8)),
        entry(3, 2, score(6)),
        entry(4, 3, score(7)),
        entry(5, 1, score(10)),
    )
    assert found.ranked == [C(5), C(2), C(4), C(3), C(1)]
    assert list(found.line(C(4))) == [C(3), C(2), C(1)]
    assert (found.line_best(C(4)), found.line_best(C(3)), found.line_best(C(1))) == (C(2), C(2), None)
    assert found.standing(C(4)).told() == (
        "score 7 over 1 eval: rank 3 of 5; vs parent +1, vs your line's best -1, vs best candidate#5 -3"
    )
    assert found.standing(C(3)).vs_line is None, 'the best of its line is its parent'
    assert (
        found.standing(C(5)).told()
        == 'score 10 over 1 eval: the best of the score track now, rank 1 of 5; vs parent +5'
    )
    assert found.worth_branching(k=1) == [C(5), C(2)], 'one other line, its best'


def test_a_line_stops_at_a_parent_outside_the_pool_and_at_a_cycle() -> None:
    found = pool(entry(1, None, score(1)), entry(2, 9, score(2)), entry(3, 4, score(3)), entry(4, 3, score(4)))
    assert list(found.line(C(2))) == [], 'candidate#9 is not scored: a recovered tree, say'
    assert list(found.line(C(3))) == [C(4)]
    assert list(found.line(C(7))) == []


def test_a_lower_is_better_primary_ranks_low_first_and_counts_a_drop_as_a_gain() -> None:
    cfg = config(Metric(name='seconds', direction=Direction.LOWER, role=MetricRole.PRIMARY))
    found = pool(
        entry(1, None, {'seconds': [10]}),
        entry(2, 1, {'seconds': [8]}),
        entry(3, 1, {'seconds': [12]}),
        entry(4, 1, {'seconds': [8]}),
        cfg=cfg,
    )
    assert found.ranked == [C(2), C(4), C(1), C(3)], 'the older first on a tie'
    assert (found.best(), found.gain(C(2), C(1)), found.gain(C(3), C(1))) == (C(2), 2, -2)
    assert found.standing(C(3)).told() == 'seconds 12 over 1 eval: rank 4 of 4; vs parent -2, vs best candidate#2 -4'
    unconfirmed = pool(entry(1, None, {'seconds': [10, 10]}), entry(2, 1, {'seconds': [9]}), cfg=cfg, confirm=2)
    assert unconfirmed.confirm_due(C(2)), 'a lower mean beats the best'
    assert not unconfirmed.confirm_due(C(1)), 'the best is not confirmed against itself'


def test_a_candidate_may_not_be_best_when_excluded_unscored_or_unconfirmed_and_the_baseline_always_may() -> None:
    found = pool(
        entry(1, None, score(5), excluded='a human took it out'),
        entry(2, 1, score(9), excluded='gamed the split'),
        entry(3, 1, {}, failed=2),
        entry(4, 1, score(9)),
        entry(5, 1, score(7, 7, 7)),
        confirm=3,
    )
    assert [found.refusal(C(c)) for c in range(1, 6)] == [
        None,
        'excluded: gamed the split',
        'no successful evaluation',
        '1 of the 3 evaluations that confirm it',
        None,
    ]
    assert found.ranked == [C(5), C(1)]
    assert [found.confirm_due(C(c)) for c in range(1, 6)] == [False, False, False, True, False]
    assert found.gain(C(4), C(4)) is None and found.gain(C(4), None) is None and found.gain(C(3), C(1)) is None


def test_what_a_worker_hears_of_a_candidate_without_a_score_or_one_that_was_excluded() -> None:
    found = pool(
        entry(1, None, score(5)),
        entry(2, 1, {}, failed=1),
        entry(3, 1, {}, stopped='accuracy 0.4 past its floor 0.5'),
        entry(4, 1, {}),
        entry(5, 1, score(9), excluded='gamed the split'),
    )
    assert [found.standing(C(c)).told() for c in range(2, 6)] == [
        'crash: no evaluation succeeded',
        'stopped at a rung of the ladder: accuracy 0.4 past its floor 0.5',
        'not scored',
        'score 9 over 1 eval: excluded (gamed the split); vs parent +4, vs best candidate#1 +4',
    ]


def test_a_baseline_without_a_score_is_still_the_best_and_a_first_score_is_due_to_be_confirmed() -> None:
    found = pool(entry(1, None, {}), entry(2, 1, score(3)), confirm=3)
    assert (found.ranked, found.best(), found.standing(C(1)).rank, found.standing(C(1)).of) == ([], C(1), 1, 1)
    assert found.confirm_due(C(2))
    empty = pool()
    assert (empty.best(), empty.worth_branching()) == (None, [])


def test_the_sealed_score_shows_once_a_candidate_is_confirmed() -> None:
    found = pool(entry(1, None, score(5, 5), sealed=4), entry(2, 1, score(6), sealed=7), confirm=2)
    assert (found.standing(C(1)).sealed, found.standing(C(2)).sealed) == (4, None)


def test_every_other_track_s_primary_is_a_guardrail_of_a_track_against_the_baseline() -> None:
    """A serving candidate must hold accuracy within its margin of the baseline's; its own track's best is per track."""
    cfg = config(
        Metric(name='acc', direction=Direction.HIGHER, role=MetricRole.PRIMARY, track='accuracy', margin=2, floor=90),
        Metric(name='cost', direction=Direction.LOWER, role=MetricRole.PRIMARY, track='serving', margin=1),
    )
    baseline = entry(1, None, {'acc': [95], 'cost': [10]})
    cheap = entry(2, 1, {'acc': [92], 'cost': [8]})
    held = entry(3, 1, {'acc': [94], 'cost': [9]})
    serving = pool(baseline, cheap, held, cfg=cfg, track='serving')
    assert serving.refusal(C(2)) == 'guardrails: acc 92 vs the baseline 95 beyond its margin 2'
    assert (serving.ranked, serving.best()) == ([C(3), C(1)], C(3))
    accuracy = pool(baseline, cheap, held, cfg=cfg, track='accuracy')
    assert (accuracy.ranked, accuracy.best()) == ([C(1), C(3), C(2)], C(1)), 'a cheaper candidate holds cost'
    alone = pool(baseline, cheap, cfg=cfg, track='serving')
    assert alone.standing(C(2)).told() == (
        'cost 8 over 1 eval: it may not be best: guardrails: acc 92 vs the baseline 95 beyond its margin 2;'
        ' vs parent +2, vs best candidate#1 +2'
    ), 'it would be best but for the rule'
    assert accuracy.standing(C(2)).refused is None, 'not better than the best: the rule is not why'
