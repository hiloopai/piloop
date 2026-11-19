"""``min_gain``: the built-in best rule, but the best needs a gain of at least ``min_gain`` over the baseline.

Use it when a tiny gain is not worth a new best. The rule is the built-in rule ``baseline`` and changes one thing: the
guardrails, the sealed split and the reasons stay those of ``baseline``. No state, no I/O.
"""

from piloop.capabilities import Baseline, Case, Keep, Refuse, Unanswered, number


class MinGain(Baseline):
    """What ``baseline`` allows, but a candidate whose gain over the baseline is below ``min_gain`` may not be best."""

    min_gain: float
    """The smallest gain over the baseline that may be best, in the primary metric's units."""

    def judge(self, case: Case) -> Keep | Refuse | Unanswered:
        if not isinstance(first := super().judge(case), Keep):
            return first
        name = case.primary.name
        if name not in case.baseline.means:
            return first
        gain = case.primary.direction.gain(case.candidate.means[name], case.baseline.means[name])
        return (
            first
            if gain >= self.min_gain
            else Refuse(f'its gain {number(gain)} is below min_gain {number(self.min_gain)}')
        )
