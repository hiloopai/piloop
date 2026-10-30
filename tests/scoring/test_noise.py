"""The ``noise`` verdict rule: a gain must exceed ``m`` standard deviations of the baseline, and hold on the sealed split."""

from inline_snapshot import snapshot

from piloop.plugins.campaign_type.kit.candidates.ids import Outcome
from piloop.plugins.campaign_type.kit.evaluation.ids import RungName
from piloop.plugins.campaign_type.kit.verdicts.verdict_rule.contract import (
    Case,
    Criterion,
    Judgement,
    Sealed,
    Side,
    Stopped,
)
from piloop.plugins.campaign_type.kit.verdicts.verdict_rule.noise import Noise
from piloop.plugins.metric import Direction

BYTES = Criterion('bytes', Direction.LOWER)
SECONDS = Criterion('seconds', Direction.LOWER, margin=10, floor=100)


def case(
    mine: float | None,
    theirs: float | None = 1000,
    *,
    noise: float | None = 10,
    measured: int = 1,
    sealed: Sealed | None = None,
    seconds: tuple[float, float] | None = None,
    stopped: Stopped | None = None,
) -> Case:
    mine_means = {} if mine is None else {'bytes': mine}
    theirs_means = {} if theirs is None else {'bytes': theirs}
    if seconds is not None:
        mine_means['seconds'], theirs_means['seconds'] = seconds
    return Case(
        BYTES,
        (SECONDS,) if seconds is not None else (),
        Side('candidate#9', mine_means, {}, stopped=stopped, measured=measured),
        Side('candidate#4', theirs_means, {}),
        noise=noise,
        sealed=sealed,
    )


def test_a_gain_beyond_m_sigma_keeps_and_one_within_it_discards() -> None:
    rule = Noise()
    assert rule.decide(case(979)) == snapshot(
        Judgement(Outcome.KEEP, 'against candidate#4: bytes 979 vs 1000 (+21; noise 20 over 1 evaluations)')
    )
    assert rule.decide(case(980)).verdict is Outcome.DISCARD, 'at the threshold'
    assert rule.decide(case(1001)).verdict is Outcome.DISCARD, 'a loss'


def test_a_gain_within_the_noise_asks_for_one_more_evaluation_and_the_mean_of_two_decides() -> None:
    rule = Noise()
    assert rule.samples(case(990)) == 2, 'within m·σ: one re-run'
    assert rule.samples(case(979)) == 1, 'beyond it: no re-run'
    assert rule.samples(case(1001)) == 1, 'a loss: no re-run'
    assert rule.samples(case(990, measured=2)) == 2, 'once only'
    assert rule.decide(case(990, measured=2)).verdict is Outcome.DISCARD, '10 is within 2·10/√2'
    assert rule.decide(case(985, measured=2)).verdict is Outcome.KEEP, '15 is beyond 2·10/√2'


def test_without_noise_any_gain_keeps_and_an_unknown_noise_keeps_nothing() -> None:
    assert Noise().decide(case(999, noise=0.0)).verdict is Outcome.KEEP
    assert Noise().samples(case(999, noise=0.0)) == 1
    assert Noise(m=0).decide(case(999)).verdict is Outcome.KEEP, 'm = 0 keeps any gain'
    assert Noise().decide(case(999, noise=None)) == Judgement(
        Outcome.INTERESTING,
        'against candidate#4: bytes 999 vs 1000 (+1); the noise of the baseline is unknown, so it keeps nothing',
    )
    assert Noise().decide(case(1001, noise=None)).verdict is Outcome.DISCARD
    assert Noise().samples(case(990, noise=None)) == 1, 'no re-run on a guess'


def test_a_candidate_without_a_score_or_a_bar_without_one() -> None:
    rule = Noise()
    assert rule.decide(case(None)) == Judgement(Outcome.CRASH, 'no successful measurement')
    stopped = Stopped(RungName('screen'), 'bytes 2000 vs 1000')
    assert rule.decide(case(None, stopped=stopped)) == Judgement(
        Outcome.DISCARD, 'stopped at screen: bytes 2000 vs 1000'
    )
    assert rule.decide(case(2000, None)) == Judgement(
        Outcome.KEEP, 'candidate#4 has no score under the current evaluator'
    )
    assert rule.samples(case(2000, None)) == 1


def test_a_keep_must_hold_on_the_sealed_split() -> None:
    rule = Noise()
    assert rule.decide(case(900, sealed=Sealed(950, 1000))).reason.endswith('; it holds on the sealed split')
    assert rule.decide(case(900, sealed=Sealed(1000, 1000))).verdict is Outcome.DISCARD
    assert rule.decide(case(900, sealed=Sealed(None, 1000))) == snapshot(
        Judgement(
            Outcome.DISCARD,
            'against candidate#4: bytes 900 vs 1000 (+100; noise 20 over 1 evaluations); the sealed split did not score it',
        )
    )
    assert rule.decide(case(900, sealed=Sealed(950, None))).verdict is Outcome.INTERESTING, (
        'a split that did not score the head is no pass'
    )


def test_guardrails_still_hold_when_a_campaign_has_them() -> None:
    rule = Noise()
    assert rule.decide(case(900, seconds=(101, 50))) == Judgement(
        Outcome.DISCARD, 'floor: seconds 101 past its floor 100'
    )
    assert rule.decide(case(900, seconds=(61, 50))) == snapshot(
        Judgement(
            Outcome.INTERESTING,
            'against candidate#4: bytes 900 vs 1000 (+100; noise 20 over 1 evaluations); guardrails: seconds 61 vs 50 beyond its margin 10',
        )
    )
    assert rule.decide(case(900, seconds=(59, 50))).verdict is Outcome.KEEP
