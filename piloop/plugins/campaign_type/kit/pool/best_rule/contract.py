"""The contract of a best rule: whether a candidate may replace the best of its track.

piloop scores every candidate into the campaign's pool. A candidate is **ready** once it is confirmed (it has
``[evaluator] noise_runs`` evaluations), and, with a sealed split, once the split answered it. piloop walks the ready
candidates in the order in which they became ready, from the baseline. When one beats the best so far on the track's
primary, piloop calls the rule's ``judge`` with a ``Case``: the candidate, the best that it would replace, the baseline,
its parent, and the metrics. On ``Keep`` the candidate is the best. piloop stores each answer, so it asks once for each
case.

The rule reads nothing else: it is a pure provider. ``[plugins.best_rule] provider`` in ``piloop.toml`` selects the
rule, and the other keys of that table are its fields. Without the table, the rule is ``baseline``.

This module imports only the Python standard library, the plugin host, the metric types and the compat markers. A rule
imports this module, and nothing else of piloop.
"""

import warnings
from abc import ABC
from dataclasses import dataclass

from piloop import deprecated
from piloop.plugins.host import Capability, One, Parameters, Scope, Tier
from piloop.plugins.metric import Direction, MetricRole, number


@dataclass(frozen=True)
class Criterion:
    """A metric of ``piloop.toml``: the primary of the candidate's track, a guardrail, or a ``watch`` metric."""

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
    role: MetricRole = MetricRole.WATCH
    """How the metric decides, as ``[[metrics]] role`` says."""
    track: str | None = None
    """The track that a primary decides, or None for another metric."""

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
class CaseSample:
    """One successful evaluation of one side of the case."""

    id: int
    """The id of its measurement."""
    rung: str | None
    """The rung of the ladder that it measured, or None for the full panel."""
    metrics: dict[str, float]
    """What the evaluate command reported."""
    units: dict[str, dict[str, float]]
    """Each metric's per-unit results: metric, then unit id, then the unit's contribution. Empty without units."""


@dataclass(frozen=True)
class Scores:
    """One side of the case: the candidate, the best that it would replace, the baseline, or the candidate's parent."""

    name: str
    """How a reason names it: ``candidate#73``."""
    means: dict[str, float]
    """The mean of each metric over its successful evaluations of the full panel."""
    measured: int
    """How many successful evaluations of the full panel ``means`` are over."""
    sealed: float | None
    """Its score on the sealed split (``[evaluator] sealed``), or None when the split did not score it."""
    id: int
    """Its candidate id."""
    parent: int | None
    """The candidate id of its parent, or None for the baseline."""
    samples: tuple[CaseSample, ...]
    """Its successful evaluations by the current evaluator, every rung included, oldest first."""
    sealed_samples: tuple[CaseSample, ...]
    """Its successful evaluations by the sealed split, oldest first."""


@dataclass(frozen=True)
class Case:
    """All that a rule gets to decide whether one candidate may replace the best of its track."""

    primary: Criterion
    """The primary metric of the track."""
    guardrails: tuple[Criterion, ...]
    """The campaign's guardrails, then the primary of every other track."""
    candidate: Scores
    """The candidate: confirmed, and better than ``incumbent`` on the primary."""
    baseline: Scores
    """The campaign's baseline."""
    sealed: Criterion | None
    """The metric that the sealed split scores, the primary of the first track, or None without a sealed split."""
    track: str
    """The track whose best the candidate would become."""
    metrics: tuple[Criterion, ...]
    """Every metric of ``piloop.toml``, ``watch`` metrics included."""
    incumbent: Scores
    """The best that the candidate would replace. It is the baseline until a candidate replaces it."""
    parent: Scores | None
    """The candidate's parent, or None when it has none in the pool."""


@dataclass(frozen=True)
class Keep:
    """The candidate replaces the best."""

    reason: str | None = None
    """Why, or None."""


@dataclass(frozen=True)
class Refuse:
    """The candidate may not be best."""

    reason: str
    """Why: its worker hears it. Name no hidden unit in it."""


@dataclass(frozen=True)
class Unanswered:
    """The rule could not decide, such as a program that failed. piloop escalates, and the best stays."""

    error: str
    """What failed: the escalation shows it."""


class BestRule(Parameters, ABC):
    """A best rule: its fields are its parameters."""

    def judge(self, case: Case) -> Keep | Refuse | Unanswered:
        """Return whether ``case.candidate`` may replace ``case.incumbent`` as the best of ``case.track``.

        The answer must be a function of the case: piloop stores the first answer for a case and never asks again.
        """
        deprecated.until('0.10.0', 'a best rule that defines refuse, not judge: make judge abstract, delete refuse')
        warnings.warn(
            f'{type(self).__name__} defines refuse: define judge instead; refuse is removed in piloop 0.10.0',
            deprecated.PiloopDeprecationWarning,
            stacklevel=2,
        )
        why = self.refuse(case)
        return Keep() if why is None else Refuse(why)

    def refuse(self, case: Case) -> str | None:
        """Deprecated: define ``judge``. Return why ``case.candidate`` may not be best, or None when it may."""
        raise NotImplementedError(f'{type(self).__name__} defines neither judge nor refuse')


BEST_RULES = Capability('best_rule', BestRule, tier=Tier.PURE, table=One(Scope.CAMPAIGN, default='baseline'))
"""The best rule capability: every best rule by name. The default is ``baseline``
(``piloop.plugins.campaign_type.kit.pool.best_rule.baseline``)."""
