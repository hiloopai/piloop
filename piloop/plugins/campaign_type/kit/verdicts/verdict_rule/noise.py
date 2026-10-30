"""``noise``: the default verdict rule. It keeps a candidate whose primary beats the bar by more than the noise.

The noise is ``σ``, the standard deviation of the baseline's score over its first evaluations (``[evaluator]
noise_runs``). A candidate keeps when its gain over the bar is above ``m·σ/√n``, with ``n`` its evaluations. A positive
gain within ``m·σ`` on one evaluation asks for one more (``samples``). With a sealed split (``[evaluator] sealed``),
the gain must hold there too. Guardrails, when a campaign has them, may regress by at most their tolerance and never
pass their floor. The rule imports only the standard library, pydantic and its contract, as a rule from a plugin does.
"""

import math

from pydantic import Field

from piloop.plugins.campaign_type.kit.verdicts.verdict_rule.contract import (
    Case,
    Judgement,
    Outcome,
    Stopped,
    VerdictRule,
)
from piloop.plugins.metric import number


class Noise(VerdictRule):
    """``noise``: the primary must beat the bar by more than ``m`` standard deviations of the baseline's score."""

    m: float = Field(default=2, ge=0)
    """How many standard deviations of the baseline's score a gain must exceed, over the square root of the
    candidate's evaluations. 0 keeps any gain."""

    def threshold(self, case: Case, noise: float) -> float:
        """The gain that a keep must exceed: ``m·σ/√n``."""
        return self.m * noise / math.sqrt(max(case.candidate.measured, 1))

    def gain(self, case: Case) -> float | None:
        """The primary's gain over the bar, or None when a side has no score."""
        name, mine, theirs = case.primary.name, case.candidate.means, case.bar.means
        if name not in mine or name not in theirs:
            return None
        return case.primary.direction.gain(mine[name], theirs[name])

    def samples(self, case: Case) -> int:
        """Two evaluations when the first gain is positive but within ``m·σ``: one re-run. Else those it has."""
        gain, measured = self.gain(case), case.candidate.measured
        if (
            gain is not None
            and case.noise is not None
            and measured == 1
            and 0 < gain <= self.threshold(case, case.noise)
        ):
            return 2
        return measured

    def decide(self, case: Case) -> Judgement:
        """Return the verdict, in this order.

        1. No primary value is ``discard`` after a stop at a rung of the ladder, else ``crash``.
        2. A guardrail past its floor is ``discard``.
        3. A bar with no score is ``keep``.
        4. A gain while the noise is unknown (``Case.noise``) is ``interesting``: nothing keeps on a guess.
        5. A gain at or below ``m·σ/√n`` is ``discard``.
        6. A sealed score that does not beat the bar's sealed score is ``discard``. A bar that the split did not score
           is ``interesting``: a fault of the split is never a pass.
        7. A guardrail that regressed beyond its tolerance is ``interesting``.
        8. Anything else is ``keep``.
        """
        primary, mine, theirs = case.primary, case.candidate.means, case.bar.means
        match primary.name in mine, case.candidate.stopped:
            case False, Stopped(rung=rung, reason=why):
                return Judgement(Outcome.DISCARD, f'stopped at {rung}: {why}')
            case False, None:
                return Judgement(Outcome.CRASH, 'no successful measurement')
            case _:
                pass
        floors = [
            f'{m.name} {number(mine[m.name])} past its floor {number(m.floor)}'
            for m in case.guardrails
            if m.floor is not None and m.name in mine and m.past_floor(mine[m.name])
        ]
        if floors:
            return Judgement(Outcome.DISCARD, 'floor: ' + '; '.join(floors))
        gain = self.gain(case)
        if gain is None:
            return Judgement(Outcome.KEEP, f'{case.bar.name} has no score under the current evaluator')
        if case.noise is None:
            return Judgement(
                Outcome.DISCARD if gain <= 0 else Outcome.INTERESTING,
                f'against {case.bar.name}: {primary.name} {number(mine[primary.name])} vs {number(theirs[primary.name])}'
                f' ({number(gain, signed=True)}); the noise of the baseline is unknown, so it keeps nothing',
            )
        threshold = self.threshold(case, case.noise)
        reason = (
            f'against {case.bar.name}: {primary.name} {number(mine[primary.name])} vs {number(theirs[primary.name])}'
            f' ({number(gain, signed=True)}; noise {number(threshold)} over {case.candidate.measured} evaluations)'
        )
        if gain <= threshold:
            return Judgement(Outcome.DISCARD, reason)
        if (sealed := case.sealed) is not None:
            if sealed.candidate is None:
                return Judgement(Outcome.DISCARD, f'{reason}; the sealed split did not score it')
            if sealed.bar is None:
                return Judgement(Outcome.INTERESTING, f'{reason}; the sealed split did not score {case.bar.name}')
            if primary.direction.gain(sealed.candidate, sealed.bar) <= 0:
                return Judgement(Outcome.DISCARD, f'{reason}; the gain does not hold on the sealed split')
            reason += '; it holds on the sealed split'
        beyond = [
            f'{m.name} {number(mine[m.name])} vs {number(theirs[m.name])} beyond its {m.limit()}'
            for m in case.guardrails
            if m.name in mine
            and m.name in theirs
            and m.direction.gain(mine[m.name], theirs[m.name]) < -m.allowed(theirs[m.name])
        ]
        if beyond:
            return Judgement(Outcome.INTERESTING, f'{reason}; guardrails: ' + '; '.join(beyond))
        return Judgement(Outcome.KEEP, reason)
