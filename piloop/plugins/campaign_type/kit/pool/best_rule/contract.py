"""The contract of a best rule: whether a confirmed candidate may be the best of its track.

piloop scores every candidate into the campaign's pool. The **best** of a track is the candidate with the best mean of
the track's primary among those that may be best: the baseline, and each candidate that is confirmed (it has
``[evaluator] noise_runs`` evaluations) and that the best rule does not refuse. piloop calls the rule's ``refuse`` with a
``Case``: the candidate's scores, the baseline's, and the metrics that decide. The rule returns why the candidate may
not be best, or None.

The rule reads nothing else: it is a pure provider. ``[plugins.best_rule] provider`` in ``piloop.toml`` selects the
rule, and the other keys of that table are its fields. Without the table, the rule is ``baseline``.

This module imports only the Python standard library, the plugin host and the metric types. A rule imports this module,
and nothing else of piloop.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass

from piloop.plugins.host import Capability, One, Parameters, Scope, Tier
from piloop.plugins.metric import Direction, number


@dataclass(frozen=True)
class Criterion:
    """A metric that decides: the primary of the candidate's track, or one of its guardrails."""

    name: str
    """The metric's name, as the evaluate command reports it."""
    direction: Direction
    """Whether a larger or a smaller value is better."""
    margin: float | None = None
    """The largest regression from the baseline that a guardrail may show, in the metric's units. None on a primary."""
    floor: float | None = None
    """The worst value that a candidate may have, or None."""
    fraction: float | None = None
    """The largest regression from the baseline that a guardrail may show, as a fraction of the baseline's value. A
    guardrail has a ``margin`` or a ``fraction``, never both."""

    def allowed(self, baseline: float) -> float:
        """The largest regression from ``baseline``, the baseline's value, in the metric's units.

        It is ``margin``, or ``fraction`` of ``|baseline|``, or 0 without either.
        """
        return self.fraction * abs(baseline) if self.fraction is not None else self.margin or 0.0

    def limit(self) -> str:
        """How a reason names the regression allowed: ``margin 20`` or ``fraction 0.1``."""
        return (
            f'fraction {number(self.fraction)}' if self.fraction is not None else f'margin {number(self.margin or 0)}'
        )

    def past_floor(self, value: float) -> bool:
        """Whether ``value`` is past the floor, in the metric's direction."""
        return self.floor is not None and self.direction.better(self.floor, value)


@dataclass(frozen=True)
class Scores:
    """One side of the case: the candidate, or the baseline."""

    name: str
    """How a reason names it: ``candidate#73``."""
    means: dict[str, float]
    """The mean of each metric over its successful evaluations of the full panel."""
    measured: int
    """How many successful evaluations of the full panel ``means`` are over."""
    sealed: float | None
    """Its score on the sealed split (``[evaluator] sealed``), or None when the split did not score it."""


@dataclass(frozen=True)
class Case:
    """All that a rule gets to decide whether one confirmed candidate may be best."""

    primary: Criterion
    """The primary metric of the track."""
    guardrails: tuple[Criterion, ...]
    """The campaign's guardrails, then the primary of every other track."""
    candidate: Scores
    """The confirmed candidate."""
    baseline: Scores
    """The campaign's baseline, which may always be best."""
    sealed: Criterion | None
    """The metric that the sealed split scores, the primary of the first track, or None without a sealed split."""


class BestRule(Parameters, ABC):
    """A best rule: its fields are its parameters. A rule has no error case: it answers every case."""

    @abstractmethod
    def refuse(self, case: Case) -> str | None:
        """Return why ``case.candidate`` may not be the best of its track, or None when it may."""


BEST_RULES = Capability('best_rule', BestRule, tier=Tier.PURE, table=One(Scope.CAMPAIGN, default='baseline'))
"""The best rule capability: every best rule by name. The default is ``baseline``
(``piloop.plugins.campaign_type.kit.pool.best_rule.baseline``)."""
