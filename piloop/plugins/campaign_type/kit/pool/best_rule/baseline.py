"""``baseline``: the default best rule. A confirmed candidate may be best when it holds against the baseline.

It must keep every guardrail within its floor and its tolerance of the baseline's value, and, in a campaign with a
sealed split, beat the baseline there on the primary. The rule imports only the standard library and its contract, as a
rule from a plugin does.
"""

from piloop.plugins.campaign_type.kit.pool.best_rule.contract import BestRule, Case
from piloop.plugins.metric import number


class Baseline(BestRule):
    """``baseline``: within each guardrail of the baseline, and better than the baseline on the sealed split."""

    def refuse(self, case: Case) -> str | None:
        """Return why the candidate may not be best, in this order, or None.

        1. A metric that decides is past its floor.
        2. A guardrail regressed from the baseline beyond its tolerance.
        3. With a sealed split: the split did not score the candidate or the baseline, or the candidate does not beat
           the baseline there.
        """
        mine, theirs = case.candidate.means, case.baseline.means
        floors = [
            f'{m.name} {number(mine[m.name])} past its floor {number(m.floor)}'
            for m in (case.primary, *case.guardrails)
            if m.floor is not None and m.name in mine and m.past_floor(mine[m.name])
        ]
        if floors:
            return 'floor: ' + '; '.join(floors)
        beyond = [
            f'{m.name} {number(mine[m.name])} vs the baseline {number(theirs[m.name])} beyond its {m.limit()}'
            for m in case.guardrails
            if m.name in mine
            and m.name in theirs
            and m.direction.gain(mine[m.name], theirs[m.name]) < -m.allowed(theirs[m.name])
        ]
        if beyond:
            return 'guardrails: ' + '; '.join(beyond)
        if case.sealed is None:
            return None
        ours, base = case.candidate.sealed, case.baseline.sealed
        if ours is None:
            return 'the sealed split did not score it'
        if base is None:
            return 'the sealed split did not score the baseline'
        if case.sealed.direction.gain(ours, base) <= 0:
            return f'it does not beat the baseline on the sealed split ({number(ours)} vs {number(base)})'
        return None
