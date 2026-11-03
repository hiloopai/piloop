"""``best_cost``: tell a human when a new best costs too much, and stop at a harder line. No state, no I/O.

The rule compares the fact ``spend`` names, since the latest new best, with its value per new best so far. Past
``escalate_at`` times, it asks a human once for each new best; past ``stop_at`` times, it stops the campaign. It is the
built-in rule ``limits`` with these two lines, so ``max_hours``, ``hold_at`` and the others still apply. Before the
first new best there is no value per new best, and the rule is ``limits``.
"""

from pydantic import Field

from piloop.capabilities import (
    NEW_BESTS,
    Escalate,
    Fact,
    FactValue,
    Go,
    Hold,
    Limits,
    Stop,
    StopCase,
    per_best,
    reading,
    since_best,
)


class BestCost(Limits):
    """The cost since the latest new best against the cost per new best: escalate, then stop, at a multiple."""

    spend: str = 'spend'
    """The fact that is what the campaign spent so far, which a source of ``[plugins.fact_source] use`` reads."""
    escalate_at: float = Field(default=2, gt=0)
    """Ask a human once the cost since the latest new best is this many times the cost per new best."""
    stop_at: float = Field(default=4, gt=0)
    """Stop once the cost since the latest new best is this many times the cost per new best."""

    def reads(self) -> list[Fact[FactValue]]:
        return list(dict.fromkeys([*super().reads(), reading(self.spend), NEW_BESTS, since_best(self.spend)]))

    def check(self, case: StopCase) -> Go | Hold | Stop | Escalate:
        answer = super().check(case)
        each, since = per_best(case.facts, self.spend), case.facts.get(since_best(self.spend))
        if not isinstance(answer, Go) or each is None or since is None:
            return answer
        if since >= self.stop_at * each:
            return Stop(
                f'best_cost: {self.spend} {since:g} since the latest new best, {self.stop_at:g} × the {each:g} per'
                ' new best'
            )
        if since >= self.escalate_at * each:
            # one escalation for each new best: the reason names it, not the amount, which changes on every pass
            return Escalate(
                f'best_cost: since new best {case.facts.get(NEW_BESTS)}, {self.spend} passed {self.escalate_at:g} ×'
                f' its value per new best; it stops at {self.stop_at:g} ×'
            )
        return answer
