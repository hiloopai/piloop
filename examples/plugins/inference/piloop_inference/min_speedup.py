"""``min_speedup``: the built-in best rule, but the best must beat the baseline by at least ``min_speedup``.

A throughput benchmark is noisy, and a gain of a few percent can be luck that a few evaluations do not rule out. Every
run starts from the best by default, so a lucky best costs the runs after it. The rule is ``baseline`` with one more
test: its guardrails, sealed split and reasons stay.
"""

from piloop.capabilities import Baseline, Case


class MinSpeedup(Baseline):
    """What ``baseline`` allows, but a candidate that beats the baseline by less than ``min_speedup`` may not be best."""

    min_speedup: float
    """The smallest gain over the baseline that may be best, as a fraction of the baseline: ``0.05`` for 5%."""

    def refuse(self, case: Case) -> str | None:
        if (why := super().refuse(case)) is not None:
            return why
        name = case.primary.name
        base = case.baseline.means.get(name)
        if not base:
            return None
        speedup = case.primary.direction.gain(case.candidate.means[name], base) / abs(base)
        return None if speedup >= self.min_speedup else f'{speedup:.1%} is below min_speedup {self.min_speedup:.0%}'
