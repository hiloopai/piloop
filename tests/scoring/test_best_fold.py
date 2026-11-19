"""The best as a fold: the ready candidates in the order in which they became ready, each judged against the best so far.

These build a ``Pool`` from its entries, without a database, and answer each step of its fold with its rule, as the
daemon stores the answers (``tests.scoring.test_standing.judged``).
"""

import random
from dataclasses import replace

import pytest

from piloop.plugins.campaign_type.kit.candidates.ids import CandidateId
from piloop.plugins.campaign_type.kit.evaluation.config import Metric
from piloop.plugins.campaign_type.kit.evaluation.ids import MeasurementId
from piloop.plugins.campaign_type.kit.evaluation.measure import Status
from piloop.plugins.campaign_type.kit.pool.best_rule.baseline import Baseline
from piloop.plugins.campaign_type.kit.pool.best_rule.contract import (
    BestRule,
    Case,
    CaseSample,
    Keep,
    Refuse,
    Unanswered,
)
from piloop.plugins.campaign_type.kit.pool.judging import case
from piloop.plugins.campaign_type.kit.pool.pool import Entry, Ids, Pool, Step
from piloop.plugins.campaign_type.kit.pool.tables import Answer
from piloop.plugins.metric import Direction, MetricRole
from tests.scoring.test_standing import C, config, entry, judged, pool, score

GUARDED = config(
    Metric(name='score', direction=Direction.HIGHER, role=MetricRole.PRIMARY),
    Metric(name='size', direction=Direction.LOWER, role=MetricRole.GUARDRAIL, margin=2),
)


def allowed(found: Pool) -> list[CandidateId]:
    """Those that piloop 0.8 let be best, best first, the older first on a tie: the baseline, and each confirmed
    candidate that the rule does not refuse against the baseline. 0.8's best was the first."""
    assert found.baseline is not None
    base = found.baseline

    def refused(c: CandidateId) -> bool:
        if c == base:
            return False
        if found.entries[c].excluded is not None or not found.confirmed(c):
            return True
        return Baseline().refuse(case(found, Step(c, base, ''), lambda i: CaseSample(i, None, {}, {}))) is not None

    may = sorted(c for c in found.entries if found.mean(c) is not None and not refused(c))
    may.sort(key=lambda c: found.mean(c) or 0.0, reverse=True)
    return may


def shuffled(entries: list[Entry], rng: random.Random) -> list[Entry]:
    """``entries`` whose measurements came in a random order: their ready order is not their id order."""
    ids = list(range(1, 1 + sum(len(e.ids.scored) for e in entries)))
    rng.shuffle(ids)
    found = []
    for e in entries:
        mine = tuple(sorted(MeasurementId(ids.pop()) for _ in e.ids.scored))
        found.append(replace(e, ids=Ids(scored=mine, samples=mine)))
    return found


@pytest.mark.parametrize('seed', range(200))
def test_under_baseline_the_fold_is_the_argmax_of_0_8_but_on_ties(seed: int) -> None:
    """On random pools, with guardrails, exclusions, unconfirmed and unscored candidates and many equal means."""
    rng = random.Random(seed)
    confirm = rng.choice((1, 2))
    entries = [entry(1, None, {'score': [rng.randint(0, 4)], 'size': [10]})]
    for c in range(2, rng.randint(2, 9)):
        runs = rng.randint(0, 2)
        entries.append(
            entry(
                c,
                rng.randint(1, c - 1),
                {'score': [rng.randint(0, 6) for _ in range(runs)], 'size': [rng.randint(8, 14)] * runs},
                excluded='gamed' if rng.random() < 0.15 else None,
            )
        )
    found = judged(
        Pool(
            track=GUARDED.all_tracks[0],
            cfg=GUARDED,
            entries={e.id: e for e in shuffled(entries, rng)},
            baseline=C(1),
            confirm=confirm,
            rule=Baseline(),
            sealed=False,
        )
    )
    old = allowed(found)
    best = found.best()
    assert best is not None
    tied = [c for c in old if found.mean(c) == found.mean(old[0])]
    assert best == old[0] or (len(tied) > 1 and best in tied), 'the same best, but a tie goes to the first ready'
    assert found.ranked[0] == best


class OverTheBest(BestRule):
    """A rule that reads the incumbent: keep a candidate only when it beats the best so far by ``by``."""

    by: float

    def judge(self, case: Case) -> Keep | Refuse | Unanswered:
        assert case.parent is not None and case.parent.id == case.candidate.parent, 'the parent is in the case'
        gain = case.candidate.means['score'] - case.incumbent.means['score']
        return Keep() if gain >= self.by else Refuse(f'{gain:g} over {case.incumbent.name}')


def lineage(*, excluded: int | None = None, again: dict[int, list[float]] | None = None) -> Pool:
    """Baseline 10, then 11, 13, 14 and 15, in that order: each a child of the one before."""
    values = {1: [10.0], 2: [11.0], 3: [13.0], 4: [14.0], 5: [15.0], **(again or {})}
    entries = [
        entry(c, c - 1 if c > 1 else None, score(*v), excluded='gamed' if c == excluded else None)
        for c, v in values.items()
    ]
    return judged(replace(pool(*entries), rule=OverTheBest(by=2)))


def test_a_rule_that_reads_the_incumbent_makes_a_chain_and_a_change_in_the_middle_derives_it_again() -> None:
    """11 is 1 over the baseline: refused. 13 is 3 over it: kept. 14 is 1 over 13: refused. 15 is 2 over 13: kept.

    0.8's argmax with the same test against the baseline would keep 15 too, but through 14: the chain differs."""
    found = lineage()
    assert found.best() == C(5)
    assert found.fold.refused == {C(2): '1 over candidate#1', C(4): '1 over candidate#3'}
    assert found.standing(C(4)).refused is None, 'not above the best: it is ranked, and hears no refusal'
    excluded = lineage(excluded=3)
    assert excluded.best() == C(4), 'without 13: 14 is 4 over the baseline, and 15 only 1 over 14'
    assert excluded.fold.refused == {C(2): '1 over candidate#1', C(5): '1 over candidate#4'}
    remeasured = lineage(again={3: [13.0, 11.0]})
    assert remeasured.best() == C(4), '13 is 12 now: 14 is 2 over it, and 15 only 1 over 14'
    assert remeasured.key(C(3), C(1)) != found.key(C(3), C(1)), 'a remeasure makes a new case'


def test_the_fold_waits_at_a_step_without_an_answer_and_the_candidates_after_it_wait_too() -> None:
    first = pool(entry(1, None, score(10)), entry(2, 1, score(12)), entry(3, 1, score(14)))
    unjudged = replace(first, answers={})
    assert unjudged.best() == C(1)
    assert unjudged.fold.waiting is not None and unjudged.fold.waiting.candidate == C(2)
    assert unjudged.refusal(C(2)) == 'the best rule has not judged it yet'
    assert unjudged.refusal(C(3)) is None, 'behind the step that waits: no answer of its own yet'
    failed = replace(first, answers={(C(2), first.key(C(2), C(1))): (Answer.UNANSWERED, 'exit 1')})
    assert (failed.best(), failed.refusal(C(2))) == (C(1), 'the best rule failed: exit 1')
    assert first.best() == C(3)


def test_a_tie_with_the_best_so_far_keeps_the_best_so_far() -> None:
    found = pool(entry(1, None, score(10)), entry(3, 1, score(12)), entry(2, 1, score(12)))
    assert found.best() == C(2), 'candidate#2 became ready first: its measurement ids are lower'
    assert found.fold.waiting is None and found.fold.refused == {}


def test_with_a_sealed_split_a_candidate_is_ready_once_the_split_answered_it() -> None:
    """Its place is the id of the sealed split's answer when that came after the evaluation that confirmed it."""
    sealed = Status.of([({'score': 9.0}, None, None, None)], cancelled=False)
    base = replace(entry(1, None, score(10)), seal=sealed, sealed=9.0)
    late = replace(entry(2, 1, score(12)), seal=sealed, sealed=10.0)
    late = replace(late, ids=replace(late.ids, seal=MeasurementId(900)))
    early = replace(entry(3, 1, score(12)), seal=sealed, sealed=10.0)
    early = replace(early, ids=replace(early.ids, seal=MeasurementId(301)))
    waiting = entry(4, 1, score(20))
    found = judged(replace(pool(base, late, early, waiting), sealed=True))
    assert found.ready() == [C(3), C(2)], 'candidate#4 has no sealed answer: not ready'
    assert found.best() == C(3), 'a tie: candidate#3 became ready first'
    assert found.refusal(C(4)) == 'the sealed split has not scored it yet'
