"""``after_bests``: stop the campaign after ``bests`` new bests, or at a limit of the built-in rule. No state, no I/O.

Use it for a campaign that wants a few good changes and no more, for example "make the build pass on the new
compiler". It stops when a run's candidate became the best that many times. The rule is the built-in rule ``limits``
with one more limit, so ``max_hours``, ``max_tokens``, ``hold_at`` and the others still apply.
"""

from piloop.capabilities import NEW_BESTS, Escalate, Fact, FactValue, Go, Hold, Limits, Stop, StopCase


class AfterBests(Limits):
    """The campaign ends after ``bests`` new bests. ``limits`` holds it and stops it at its own limits."""

    bests: int
    """The number of new bests that ends the campaign."""

    def limits(self) -> dict[Fact[FactValue], float]:
        return super().limits() | {NEW_BESTS: self.bests}

    def check(self, case: StopCase) -> Go | Hold | Stop | Escalate:
        answer = super().check(case)
        bests = case.facts.get(NEW_BESTS)
        if not isinstance(answer, Go) or bests is None or bests < self.bests:
            return answer
        return Stop(f'after_bests: {bests} new bests')
