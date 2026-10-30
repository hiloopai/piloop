"""The ``evaluation`` component's keys of ``piloop.toml``: its evaluator, its metrics, its tracks and its ladder."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    model_validator,
)

from piloop.plugins.campaign_type.kit.evaluation.ids import RungName, TrackName
from piloop.plugins.metric import Direction


class Scoring(StrEnum):
    """Which versions of a run piloop scores."""

    SUBMIT = 'submit'
    """Those that the implementer submits while it works (``piloop submit``), each as it comes. The others are
    ``skipped``."""
    RUN_END = 'run_end'
    """Every version, after the run ends.

    Kept for the eval suite's arm that turns in-run scoring off (A1): deleted, with ``_score_each`` and the trailers
    that only it reads, once that ablation reads out.
    """


class EvaluatorConfig(BaseModel):
    """``[evaluator]``: how the campaign's versions are scored, beside ``evaluate``, the command that scores one."""

    model_config = ConfigDict(extra='forbid', frozen=True, use_attribute_docstrings=True)

    dev: list[str] | None = Field(default=None, min_length=1)
    """Optional. A fast command (argv) that checks a change, which an implementer runs in its own checkout as often as
    it likes. Each implementer's brief names it. piloop never runs it and records nothing of it. Example:
    ``["uv", "run", "python", "evaluate.py", "--quick"]``."""
    sealed: list[str] | None = Field(default=None, min_length=1)
    """Optional. The command (argv) that scores a version on the sealed split, as ``evaluate`` does: its score is the
    primary. piloop runs it only on a version that would become the new head, and on the head, and keeps the version
    only if it beats the head there too. Keep its data and its scorer out of the repo, in the home or on the
    evaluator's machines, so that no implementer reads them. Example: ``["python3", "/srv/sealed/score.py"]``."""
    scoring: Scoring = Scoring.SUBMIT
    """Which versions of a run piloop scores: ``submit``, those that the implementer submits; ``run_end``, every one,
    after the run ends."""
    submissions: int = Field(default=3, ge=1)
    """How many versions one run may submit (``piloop submit``)."""
    noise_runs: int = Field(default=3, ge=1)
    """How many times piloop evaluates the baseline under each evaluator. The standard deviation of its score is the
    noise that a gain must exceed (``[plugins.verdict_rule]``). 1: no noise, any gain counts."""


class MetricRole(StrEnum):
    """How a metric affects the verdict. https://piloop.autoresearch.inc/concepts/verdict explains the rule."""

    PRIMARY = 'primary'
    GUARDRAIL = 'guardrail'
    WATCH = 'watch'


class Tolerance(BaseModel):
    """How far a metric may go: never past ``floor``, nor worse than the bar by more than ``margin`` or ``fraction``.

    A guardrail has one, and so has each stop of a rung of the ladder. A guardrail tests its regression on the confidence bound of the bootstrap when the evaluate command reports
    per-unit results or pairs, else on the aggregate. A stop of the ladder tests the rung's values.
    """

    model_config = ConfigDict(extra='forbid', frozen=True, use_attribute_docstrings=True)

    floor: float | None = None
    """Optional. The worst value allowed, whatever the bar: at least this for ``higher``, at most this for ``lower``."""
    margin: float | None = Field(default=None, ge=0)
    """The largest regression from the bar, in the metric's own units: ``20`` for 20 ms. Not with ``fraction``."""
    fraction: float | None = Field(default=None, gt=0)
    """The largest regression from the bar, as a fraction of the bar's value: ``0.1`` for 10%. Not with ``margin``."""

    @model_validator(mode='after')
    def _one_regression(self) -> Self:
        if self.margin is not None and self.fraction is not None:
            raise ValueError('set margin or fraction, not both')
        return self

    @property
    def regression(self) -> bool:
        """Whether it limits the regression from the bar: it has a ``margin`` or a ``fraction``."""
        return self.margin is not None or self.fraction is not None

    @property
    def empty(self) -> bool:
        """Whether it limits nothing."""
        return not self.regression and self.floor is None


class Ratio(BaseModel):
    """A metric that piloop computes from two that the evaluate command reports: ``numerator / denominator``.

    Each side's value is the ratio of its means. The bootstrap resamples the units of both parts together, or else the
    pairs, and takes the ratio of their sums in each resample, so the verdict tests the ratio with confidence.
    """

    model_config = ConfigDict(extra='forbid', frozen=True, use_attribute_docstrings=True)

    numerator: str
    """The metric above the line, which the evaluate command reports. Example: ``cost_usd``."""
    denominator: str
    """The metric below the line, which the evaluate command reports. Example: ``solved``."""


class Metric(Tolerance):
    """One metric that the evaluate command reports, by its name in ``metrics`` of its result.

    Its ``floor``, ``margin`` and ``fraction`` (``Tolerance``) apply to a guardrail, which needs a ``margin`` or a
    ``fraction``, and to each primary of a campaign with several tracks: when a candidate of another track is judged,
    that primary is a guardrail.
    """

    name: str
    """The name of the metric, as the evaluate command reports it."""
    direction: Direction
    """``higher`` when a larger value is better, ``lower`` when a smaller value is better."""
    role: MetricRole = MetricRole.WATCH
    """How the metric affects the verdict. A ``primary`` metric must beat the bar with confidence for a ``keep``, and
    each track has exactly one. A ``guardrail`` may get worse by at most its ``margin`` or ``fraction``, or the candidate is at most
    ``interesting``. It must never pass its ``floor``, or the candidate is a ``discard``. A ``watch`` metric is
    recorded and never affects the verdict."""
    track: TrackName | None = None
    """Primary metrics only. The name of the track (objective) that this metric is the primary of. When unset, the
    track has the metric's name, so a campaign with one primary needs nothing here. With several primaries, each one
    names its own track, and each track needs a ``[tracks.<name>]`` table."""
    ratio: Ratio | None = None
    """Optional. Primaries and guardrails only. piloop computes this metric from two others of ``[[metrics]]``, and the
    evaluate command does not report it. Example: ``{ numerator = "cost_usd", denominator = "solved" }``."""
    repeats: int | None = Field(default=None, ge=1)
    """Optional. Marks a noisy metric, such as a time: each evaluation of a run's candidate that this metric judges runs
    the evaluate command ``repeats`` times on the candidate and as many times on its bar, one after the other, and the
    verdict compares these pairs. Load then slows both sides alike. With several, the largest number counts. Allowed on
    a guardrail, and on a primary."""

    @model_validator(mode='after')
    def _check(self) -> Self:
        if self.role is MetricRole.GUARDRAIL and not self.regression:
            raise ValueError(f'guardrail {self.name!r} needs a margin or a fraction')
        if self.role is MetricRole.WATCH and self.repeats is not None:
            raise ValueError(f'{self.name!r}: repeats applies to primaries and guardrails only')
        if self.role is MetricRole.WATCH and not self.empty:
            raise ValueError(f'{self.name!r}: floor, margin and fraction apply to guardrails and primaries only')
        if self.role is MetricRole.WATCH and self.ratio is not None:
            raise ValueError(f'{self.name!r}: ratio applies to primaries and guardrails only')
        if self.role is not MetricRole.PRIMARY and self.track is not None:
            raise ValueError(f'{self.name!r}: track applies to primaries only')
        return self

    @property
    def track_name(self) -> TrackName:
        """The track that a primary decides: its ``track``, or else its own name."""
        return self.track or TrackName(self.name)

    def better(self, a: float, b: float) -> bool:
        """Whether ``a`` is better than ``b`` for this metric."""
        return self.direction.better(a, b)

    def gain(self, a: float, b: float) -> float:
        """How much better ``a`` is than ``b``, in the metric's units. Positive when ``a`` is better."""
        return self.direction.gain(a, b)


class Rung(BaseModel):
    """``[[ladder]]``: one rung of the evaluation ladder, which a run's evaluation climbs before the full panel.

    A rung without ``evaluate`` is reported by the evaluate command itself, as a line with ``"rung": "<name>"`` before
    its full result. A rung with ``evaluate`` runs that command first, which reports one line. After each rung, the
    climb of a run's own version stops when ``stop`` says so, and piloop ends the evaluate command.
    """

    model_config = ConfigDict(extra='forbid', frozen=True, use_attribute_docstrings=True)

    name: RungName = Field(pattern=r'^[a-z][a-z0-9_-]{0,31}$')
    """The rung's name, which its line names and which the measurements of it record."""
    stop: dict[str, Tolerance] = Field(min_length=1)
    """When the climb stops at this rung, by metric of ``[[metrics]]``. Example:
    ``{ accuracy = { margin = 0.03 }, latency_ms = { fraction = 0.1 } }``."""
    evaluate: list[str] | None = Field(default=None, min_length=1)
    """Optional. A command (argv) of its own, which runs before the evaluate command and reports this rung alone. The
    rungs that have one come first in ``[[ladder]]``."""

    @model_validator(mode='after')
    def _each_stop_limits(self) -> Self:
        if empty := sorted(name for name, test in self.stop.items() if test.empty):
            raise ValueError(f'stop of {empty}: set floor, margin or fraction')
        return self


class TrackConfig(BaseModel):
    """``[tracks.<name>]``: one track of the campaign. A track is an objective with its own primary metric and brief.

    A track has the name of its primary's ``track``, or else the name of the primary. All tracks build on one trunk. A
    run starts from the head, which is the latest keep on the trunk. A keep on any track must also hold the primary of
    every other track within that primary's ``margin`` and ``floor``. A table is needed only for an addendum.

    Experimental: campaigns with more than one track.
    """

    model_config = ConfigDict(extra='forbid', frozen=True, use_attribute_docstrings=True)

    addendum: str | None = None
    """A repo-relative file that a worker gets when it claims an idea of this track (``piloop idea claim``), after the
    track's keep rule. Use it to say how work on this objective is done, for example "profile first" or "keep the
    output identical"."""


@dataclass(frozen=True)
class Track:
    """One track, as the daemon judges and schedules it."""

    name: TrackName
    primary: Metric
    addendum: str | None
    guardrails: tuple[Metric, ...]
    """What a candidate of this track must hold: the campaign's guardrails, then the primary of every other track, each
    with its margin and floor."""

    @property
    def repeats(self) -> int | None:
        """The pairs of each evaluation of this track's candidates: the largest ``repeats`` of the metrics that judge.

        None: one sample, unpaired.
        """
        return max((m.repeats for m in (self.primary, *self.guardrails) if m.repeats is not None), default=None)
