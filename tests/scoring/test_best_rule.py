"""The best rule contract and its default provider ``baseline``: whether a confirmed candidate may be best.

``baseline`` reads the candidate's scores against the baseline's, never against its parent's or the best's.
"""

import ast
import sys
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

import pytest
from piloop_min_gain import MinGain

from piloop.deprecated import PiloopDeprecationWarning
from piloop.plugins.campaign_type.kit.pool.best_rule import baseline as baseline_module
from piloop.plugins.campaign_type.kit.pool.best_rule.baseline import Baseline
from piloop.plugins.campaign_type.kit.pool.best_rule.contract import (
    BEST_RULES,
    BestRule,
    Case,
    Criterion,
    Keep,
    Refuse,
    Scores,
)
from piloop.plugins.host import Tier, data_errors
from piloop.plugins.metric import Direction

SCORE = Criterion('score', Direction.HIGHER)
SIZE = Criterion('size', Direction.LOWER, margin=3)
SEALED = Criterion('score', Direction.HIGHER)


def side(c: int, means: Mapping[str, float], sealed: float | None = None, *, measured: int = 3) -> Scores:
    """One side of a case: candidate#``c``, a child of the baseline candidate#1, with ``means`` and no samples."""
    return Scores(f'candidate#{c}', dict(means), measured, sealed, c, 1 if c != 1 else None, (), ())


def case(
    mine: Mapping[str, float],
    theirs: Mapping[str, float],
    *guardrails: Criterion,
    primary: Criterion = SCORE,
    sealed: tuple[float | None, float | None] | None = None,
    measured: int = 3,
) -> Case:
    """The case of candidate#9 against the baseline, candidate#1, its parent and the best: with a sealed split when
    ``sealed`` gives the two sealed scores."""
    ours, base = sealed if sealed is not None else (None, None)
    baseline = side(1, theirs, base, measured=measured)
    return Case(
        primary=primary,
        guardrails=guardrails,
        candidate=side(9, mine, ours, measured=measured),
        baseline=baseline,
        sealed=SEALED if sealed is not None else None,
        track=primary.name,
        metrics=(primary, *guardrails),
        incumbent=baseline,
        parent=baseline,
    )


def test_a_candidate_within_every_guardrail_may_be_best_whatever_its_primary_did() -> None:
    """The rule judges only what may be best: the pool ranks by the primary, so a worse primary is no refusal."""
    rule = Baseline()
    assert rule.refuse(case({'score': 12, 'size': 10}, {'score': 10, 'size': 10}, SIZE)) is None
    assert rule.refuse(case({'score': 8, 'size': 13}, {'score': 10, 'size': 10}, SIZE)) is None, 'at its margin'
    assert rule.refuse(case({'score': 12}, {'score': 10, 'size': 10}, SIZE)) is None, 'a guardrail it lacks'
    assert rule.refuse(case({'score': 12, 'size': 99}, {'score': 10}, SIZE)) is None, 'a guardrail the baseline lacks'


def test_a_metric_past_its_floor_is_refused_first_and_every_one_is_named() -> None:
    primary = Criterion('score', Direction.HIGHER, floor=5)
    size = Criterion('size', Direction.LOWER, margin=1, floor=20)
    mine, theirs = {'score': 4.5, 'size': 30}, {'score': 10, 'size': 10}
    assert Baseline().refuse(case(mine, theirs, size, primary=primary)) == (
        'floor: score 4.5 past its floor 5; size 30 past its floor 20'
    ), 'the floors before the guardrail that regressed by 20'
    assert Baseline().refuse(case({'score': 5, 'size': 10}, theirs, size, primary=primary)) is None, 'at the floor'
    assert Baseline().refuse(case({'size': 10}, theirs, size, primary=primary)) is None, 'no value: no floor to pass'


def test_a_guardrail_regressed_from_the_baseline_beyond_its_margin_is_refused() -> None:
    errors = Criterion('errors', Direction.LOWER, margin=50)
    coverage = Criterion('coverage', Direction.HIGHER, margin=10)
    theirs = {'score': 10, 'errors': 300, 'coverage': 1020}
    mine = {'score': 30, 'errors': 420, 'coverage': 1005}
    assert Baseline().refuse(case(mine, theirs, errors, coverage)) == (
        'guardrails: errors 420 vs the baseline 300 beyond its margin 50;'
        ' coverage 1005 vs the baseline 1020 beyond its margin 10'
    )
    assert Baseline().refuse(case({**mine, 'errors': 350}, theirs, errors, coverage)) == (
        'guardrails: coverage 1005 vs the baseline 1020 beyond its margin 10'
    )


def test_a_guardrail_s_fraction_is_of_the_baseline_s_value_and_none_allows_no_regression() -> None:
    latency = Criterion('latency', Direction.LOWER, fraction=0.1)
    theirs = {'score': 10, 'latency': 40}
    assert Baseline().refuse(case({'score': 11, 'latency': 44}, theirs, latency)) is None
    assert Baseline().refuse(case({'score': 11, 'latency': 44.5}, theirs, latency)) == (
        'guardrails: latency 44.5 vs the baseline 40 beyond its fraction 0.1'
    )
    gain = Criterion('gain', Direction.HIGHER, fraction=0.5)
    assert Baseline().refuse(case({'gain': -6}, {'gain': -4}, gain)) is None, 'a fraction of |-4|: 2'
    strict = Criterion('size', Direction.LOWER)
    assert Baseline().refuse(case({'size': 10.5}, {'size': 10}, strict)) == (
        'guardrails: size 10.5 vs the baseline 10 beyond its margin 0'
    )


def test_with_a_sealed_split_the_candidate_must_beat_the_baseline_there() -> None:
    mine, theirs = {'score': 12}, {'score': 10}
    assert Baseline().refuse(case(mine, theirs, sealed=(11, 10))) is None
    assert Baseline().refuse(case(mine, theirs, sealed=(None, 10))) == 'the sealed split did not score it'
    assert Baseline().refuse(case(mine, theirs, sealed=(11, None))) == 'the sealed split did not score the baseline'
    assert Baseline().refuse(case(mine, theirs, sealed=(10, 10))) == (
        'it does not beat the baseline on the sealed split (10 vs 10)'
    ), 'a gain of 0 is no gain'
    assert Baseline().refuse(case(mine, theirs, sealed=(9.5, 10))) == (
        'it does not beat the baseline on the sealed split (9.5 vs 10)'
    )
    lower = replace(case(mine, theirs, sealed=(9, 10)), sealed=Criterion('score', Direction.LOWER))
    assert Baseline().refuse(lower) is None, "in the sealed metric's direction"
    assert Baseline().refuse(case(mine, theirs, sealed=None)) is None, 'no sealed split: its scores are not read'


def test_a_floor_comes_before_a_guardrail_and_a_guardrail_before_the_sealed_split() -> None:
    floored = Criterion('size', Direction.LOWER, margin=1, floor=20)
    theirs = {'score': 10, 'size': 10}
    assert Baseline().refuse(case({'score': 12, 'size': 25}, theirs, floored, sealed=(None, None))) == (
        'floor: size 25 past its floor 20'
    )
    assert Baseline().refuse(case({'score': 12, 'size': 15}, theirs, floored, sealed=(None, None))) == (
        'guardrails: size 15 vs the baseline 10 beyond its margin 1'
    )


def test_a_criterion_allows_its_margin_or_its_fraction_of_the_baseline() -> None:
    assert (SIZE.allowed(40), SIZE.limit()) == (3, 'margin 3')
    relative = Criterion('size', Direction.LOWER, fraction=0.25)
    assert (relative.allowed(-40), relative.limit()) == (10, 'fraction 0.25')
    assert (SCORE.allowed(40), SCORE.limit()) == (0, 'margin 0'), 'a primary allows no regression'
    floored = Criterion('size', Direction.LOWER, floor=20)
    assert (floored.past_floor(20.5), floored.past_floor(20), SIZE.past_floor(1e9)) == (True, False, False)


def test_the_contract_is_data_in_data_out_and_baseline_is_the_default() -> None:
    """A rule can move out of process or to a Wasm component: nothing live crosses its contract."""
    assert data_errors(BestRule) == []
    assert BEST_RULES.tier is Tier.PURE and BEST_RULES.group == 'piloop.best_rules'
    assert BEST_RULES.selected({}) == Baseline()


def test_the_built_in_rule_imports_only_the_standard_library_its_contract_and_the_metric_types() -> None:
    """``baseline`` is a pure provider like a plugin's: it reads nothing of piloop's state."""
    tree = ast.parse(Path(baseline_module.__file__).read_text())
    imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names} | {
        n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module is not None
    }
    assert {m for m in imported if m.split('.')[0] not in sys.stdlib_module_names} == {
        'piloop',  # its compat markers (piloop.deprecated)
        'piloop.plugins.campaign_type.kit.pool.best_rule.contract',
        'piloop.plugins.metric',
    }


@pytest.mark.parametrize(
    ('mine', 'theirs', 'sealed'),
    [
        ({'score': 4, 'size': 10}, {'score': 1, 'size': 10}, None),
        ({'score': 12, 'size': 14}, {'score': 1, 'size': 10}, None),
        ({'score': 12, 'size': 10}, {'score': 1, 'size': 10}, (1.0, 2.0)),
        ({'score': 12, 'size': 10}, {'size': 10}, None),
    ],
)
def test_the_example_rule_min_gain_keeps_every_refusal_of_baseline(
    mine: dict[str, float], theirs: dict[str, float], sealed: tuple[float, float] | None
) -> None:
    """``examples/plugins/min-gain`` calls the built-in rule and changes one thing, through the host's contract."""
    found = case(mine, theirs, SIZE, sealed=sealed)
    assert MinGain(min_gain=2).judge(found) == Baseline().judge(found)


def test_the_example_rule_min_gain_refuses_a_gain_over_the_baseline_below_min_gain() -> None:
    assert MinGain(min_gain=2).judge(case({'score': 11, 'size': 10}, {'score': 10, 'size': 10}, SIZE)) == Refuse(
        'its gain 1 is below min_gain 2'
    )
    assert MinGain(min_gain=1).judge(case({'score': 11, 'size': 10}, {'score': 10, 'size': 10}, SIZE)) == Keep()


def test_baseline_judges_with_the_reason_of_refuse() -> None:
    assert Baseline().judge(case({'score': 12}, {'score': 10}, sealed=(None, 10))) == Refuse(
        'the sealed split did not score it'
    )
    assert Baseline().judge(case({'score': 12}, {'score': 10})) == Keep()


class OnlyRefuse(BestRule):
    """A rule of piloop 0.8: it defines ``refuse`` alone."""

    def refuse(self, case: Case) -> str | None:
        return None if case.candidate.means['score'] > 11 else 'too small'


class ExtendsBaseline(Baseline):
    """A rule of piloop 0.8 that extends ``baseline`` through ``refuse``, as ``min_gain`` did."""

    def refuse(self, case: Case) -> str | None:
        return super().refuse(case) or ('too small' if case.candidate.means['score'] <= 11 else None)


@pytest.mark.parametrize('rule', [OnlyRefuse(), ExtendsBaseline()])
def test_a_rule_that_defines_refuse_alone_still_judges_and_warns(rule: BestRule) -> None:
    with pytest.warns(PiloopDeprecationWarning, match='define judge instead; refuse is removed in piloop 0.10.0'):
        assert rule.judge(case({'score': 12}, {'score': 10})) == Keep()
    with pytest.warns(PiloopDeprecationWarning):
        assert rule.judge(case({'score': 11}, {'score': 10})) == Refuse('too small')


def test_a_rule_that_extends_baseline_through_refuse_keeps_its_refusals() -> None:
    with pytest.warns(PiloopDeprecationWarning):
        assert ExtendsBaseline().judge(case({'score': 12, 'size': 20}, {'score': 10, 'size': 10}, SIZE)) == Refuse(
            'guardrails: size 20 vs the baseline 10 beyond its margin 3'
        )


def test_a_rule_that_defines_neither_judge_nor_refuse_fails_when_asked() -> None:
    class Nothing(BestRule):
        pass

    with pytest.warns(PiloopDeprecationWarning), pytest.raises(NotImplementedError, match='neither judge nor refuse'):
        Nothing().judge(case({'score': 12}, {'score': 10}))
