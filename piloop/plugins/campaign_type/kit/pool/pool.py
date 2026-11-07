"""The pool: every scored candidate of a campaign, and the best of each track, read from the measurements.

A candidate enters the pool with its first evaluation. It is **confirmed** once it has ``[evaluator] noise_runs``
successful evaluations of the full panel (one with a deterministic evaluator). The **best** of a track is the
candidate with the best mean of the track's primary among the baseline and the confirmed candidates that the best rule
does not refuse (``best_rule``). It is a query: nothing stores it, so no order of the evaluations decides it.
"""

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from functools import cached_property
from math import sqrt
from statistics import fmean, stdev

from sqlalchemy import Select, select
from sqlalchemy.exc import NoResultFound
from sqlalchemy.orm import Session

from piloop.config import CampaignConfig
from piloop.db.models import Campaign
from piloop.plugins.campaign_type.kit.candidates.ids import CandidateId, Origin
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.evaluation.config import Metric, Track
from piloop.plugins.campaign_type.kit.evaluation.ids import EvaluatorId, Measured, RungName, TrackName
from piloop.plugins.campaign_type.kit.evaluation.measure import (
    Split,
    Status,
    derive,
    means,
    ratios,
    sealed_of,
    split_of,
    status,
)
from piloop.plugins.campaign_type.kit.evaluation.tables import Cancel, Evaluator, Measurement
from piloop.plugins.campaign_type.kit.pool.best_rule.contract import BEST_RULES, BestRule, Case, Criterion, Scores
from piloop.plugins.metric import number

UNSCORED = frozenset({Origin.RECOVERED, Origin.SKIPPED})
"""The origins of the candidates that never enter the pool: the daemon does not measure them."""
WITHIN = 2
"""How many standard deviations of a difference a delta must exceed to be more than noise, in what a worker hears."""


@dataclass(frozen=True)
class Entry:
    """One candidate of the pool under one evaluator, as read from its measurements."""

    id: CandidateId
    parent: CandidateId | None
    origin: Origin
    excluded: str | None
    """Why someone excluded it (``piloop candidate exclude``), or None."""
    status: Status
    """What the evaluator measured of it."""
    means: dict[str, float]
    """The mean of each metric over the evaluations that scored (``Status.scores``), ratios included."""
    samples: dict[str, list[float]]
    """Each metric's value in each evaluation that scored."""
    seal: Status
    """What the sealed split measured of it: nothing without one."""
    sealed: float | None
    """Its score on the sealed split, or None."""

    @property
    def name(self) -> str:
        return f'candidate#{self.id}'


def criterion(m: Metric) -> Criterion:
    """Return what a best rule reads of a metric of ``piloop.toml``."""
    return Criterion(m.name, m.direction, m.margin, m.floor, m.fraction)


def best_rule(cfg: CampaignConfig) -> BestRule:
    """Return the campaign's best rule: ``[plugins.best_rule]``, else ``baseline``."""
    return BEST_RULES.selected(cfg.plugins)


def confirm_runs(s: Session, campaign: Campaign, evaluator_id: EvaluatorId | None) -> int:
    """How many successful evaluations confirm a candidate: ``[evaluator] noise_runs``, or 1 when they would agree.

    They agree when ``noise_runs`` is below 2, or when the baseline's evaluations agree on each track's primary: such
    an evaluator gives one score per version, so a second evaluation shows nothing.
    """
    runs = campaign.config.evaluator.noise_runs
    return 1 if runs < 2 or deterministic(s, campaign, evaluator_id) else runs


def deterministic(s: Session, campaign: Campaign, evaluator_id: EvaluatorId | None) -> bool:
    """Whether the baseline has at least two successful evaluations under ``evaluator_id``, all equal on each primary.

    A ratio primary is the ratio of each evaluation's parts.
    """
    first = baseline(s, campaign)
    if first is None:
        return False
    found = ratios(s.get(Evaluator, evaluator_id) if evaluator_id is not None else None)
    rows = [derive(values, found) for values in status(first, evaluator_id).scores]
    return len(rows) >= 2 and all(
        len({row.get(t.primary.name) for row in rows}) == 1 for t in campaign.config.all_tracks
    )


def baseline(s: Session, campaign: Campaign) -> Candidate | None:
    """Return the campaign's first candidate: the repo's HEAD when the campaign was registered.

    None before it has one: a campaign added by hand, with SQL.
    """
    first = select(Candidate).where(Candidate.campaign_id == campaign.id).order_by(Candidate.id).limit(1)
    return s.scalars(first).first()


def track_of(cfg: CampaignConfig, candidate: Candidate) -> Track:
    """Return the track of the candidate's run, its idea's: the track it is scored on.

    A candidate without one, such as the baseline or a reference, gets the first track (``fallback_track``).
    """
    run = candidate.run
    return fallback_track(cfg, run.idea.track if run is not None and run.idea is not None else None)


def fallback_track(cfg: CampaignConfig, name: TrackName | None) -> Track:
    """Return the track called ``name``, else the first track. An idea can name a track that ``piloop.toml`` removed."""
    return next((t for t in cfg.all_tracks if t.name == name), cfg.all_tracks[0])


@dataclass(frozen=True)
class Standing:
    """Where one candidate stands in the pool of its track: what its run's worker hears, and what the views show."""

    candidate: CandidateId
    track: TrackName
    metric: str
    """The track's primary."""
    mean: float | None
    """Its mean of the primary, or None without an evaluation that scored."""
    status: Status
    confirmed: bool
    refused: str | None
    """Why the best rule refuses it, when it is confirmed and better than the best, or None."""
    excluded: str | None
    best: CandidateId | None
    """The best of the track, or None before the campaign has a baseline."""
    rank: int | None
    """Its place among those that may be best, best first, or None when it may not be."""
    of: int
    """How many may be best."""
    vs_parent: float | None
    vs_line: float | None
    """The gain over the best of its line, the candidates on its chain of parents, when that is neither its parent nor
    the best."""
    vs_best: float | None
    sigma: float | None
    """The standard deviation of the best's own evaluations of the primary, or None with fewer than two."""
    sealed: float | None
    within: tuple[str, ...] = ()
    """The deltas of ``vs_parent``, ``vs_line`` and ``vs_best`` that are within the noise, by name."""

    def told(self) -> str:
        """What its run's worker hears, after ``candidate#<id> (<revision>):``."""
        if self.mean is None:  # a score without the primary is none
            match self.status.state:
                case Measured.STOPPED:
                    return f'stopped at a rung of the ladder: {self.status.stopped}'
                case Measured.CRASHED:
                    return 'crash: no evaluation succeeded'
                case Measured.CANCELLED:
                    return 'cancelled: not scored'
                case _:
                    return 'not scored'
        evals = f'{self.status.scored} eval' + ('s' if self.status.scored != 1 else '')
        head = f'{self.metric} {number(self.mean)} over {evals}'
        if self.best == self.candidate:
            return f'{head}: the best of the {self.track} track now, rank 1 of {self.of}{self._deltas(best=False)}'
        if self.excluded is not None:
            return f'{head}: excluded ({self.excluded}){self._deltas()}'
        if self.refused is not None:
            return f'{head}: it may not be best: {self.refused}{self._deltas()}'
        if self.rank is not None:
            return f'{head}: rank {self.rank} of {self.of}{self._deltas()}'
        return f'{head}{self._deltas()}'

    def _deltas(self, *, best: bool = True) -> str:
        parts = [
            f'{label} {number(value, signed=True)}{" (within noise)" if key in self.within else ""}'
            for key, label, value in (
                ('vs_parent', 'vs parent', self.vs_parent),
                ('vs_line', "vs your line's best", self.vs_line),
                ('vs_best', f'vs best candidate#{self.best}', self.vs_best if best else None),
            )
            if value is not None
        ]
        sigma = f' (σ {number(self.sigma)})' if self.sigma is not None and self.sigma > 0 else ''
        return ('; ' + ', '.join(parts) + sigma) if parts else ''


@dataclass(frozen=True)
class Pool:
    """The pool of one track under one evaluator: each scored candidate, the baseline, and the rule that judges them."""

    track: Track
    cfg: CampaignConfig
    entries: dict[CandidateId, Entry]
    baseline: CandidateId | None
    confirm: int
    rule: BestRule
    sealed: bool

    @classmethod
    def of(cls, s: Session, campaign: Campaign, track: Track, evaluator_id: EvaluatorId | None = None) -> Pool:
        """Return the pool of ``track``, under ``evaluator_id``, by default the campaign's current evaluator.

        It reads columns, not rows: a campaign can have thousands of candidates.
        """
        cfg = campaign.config
        ev = campaign.evaluator_id if evaluator_id is None else evaluator_id
        hidden = sealed_of(s, campaign, cfg)
        first = baseline(s, campaign)
        found = ratios(s.get(Evaluator, ev) if ev is not None else None)
        rows = s.execute(
            select(Candidate.id, Candidate.parent_id, Candidate.origin, Candidate.excluded_reason).where(
                Candidate.campaign_id == campaign.id, Candidate.origin.not_in(UNSCORED)
            )
        ).all()
        ours = select(Candidate.id).where(Candidate.campaign_id == campaign.id)
        visible, split = _statuses(s, ours, ev), _statuses(s, ours, hidden.id) if hidden is not None else {}
        name, none = cfg.all_tracks[0].primary.name, Status()
        entries = {
            c: Entry(
                id=c,
                parent=parent,
                origin=origin,
                excluded=excluded,
                status=(mine := visible.get(c, none)),
                means=derive(means(mine.scores), found),
                samples=_samples(mine.scores),
                seal=(seal := split.get(c, none)),
                # the sealed command reports the score, or its result is refused
                sealed=fmean(v[name] for v in seal.scores) if seal.scores else None,
            )
            for c, parent, origin, excluded in rows
        }
        return cls(
            track=track,
            cfg=cfg,
            entries=entries,
            baseline=first.id if first is not None else None,
            confirm=confirm_runs(s, campaign, ev),
            rule=best_rule(cfg),
            sealed=cfg.evaluator.sealed is not None,
        )

    @property
    def primary(self) -> Metric:
        return self.track.primary

    def mean(self, c: CandidateId | None) -> float | None:
        """The mean of the primary of ``c``, or None."""
        return None if c is None or c not in self.entries else self.entries[c].means.get(self.primary.name)

    def confirmed(self, c: CandidateId) -> bool:
        """Whether ``c`` has the evaluations that confirm it."""
        return self.entries[c].status.scored >= self.confirm

    def refusal(self, c: CandidateId) -> str | None:
        """Why ``c`` may not be best, or None. The baseline may always be best."""
        entry = self.entries[c]
        if c == self.baseline:
            return None
        if entry.excluded is not None:
            return f'excluded: {entry.excluded}'
        if self.mean(c) is None:
            return 'no successful evaluation'
        if not self.confirmed(c):
            return f'{entry.status.scored} of the {self.confirm} evaluations that confirm it'
        assert self.baseline is not None  # a campaign with a candidate has a baseline
        sealed = criterion(self.cfg.all_tracks[0].primary) if self.sealed else None
        case = Case(
            criterion(self.primary),
            tuple(map(criterion, self.track.guardrails)),
            self._scores(entry),
            self._scores(self.entries[self.baseline]),
            sealed,
        )
        return self.rule.refuse(case)

    def _scores(self, entry: Entry) -> Scores:
        return Scores(entry.name, entry.means, entry.status.scored, entry.sealed)

    @cached_property
    def ranked(self) -> list[CandidateId]:
        """Those that may be best and have a mean, best first; the older first on a tie."""
        direction = self.primary.direction
        may = [c for c in self.entries if self.mean(c) is not None and self.refusal(c) is None]
        ordered = sorted(may)
        ordered.sort(key=lambda c: self.mean(c) or 0.0, reverse=direction.better(1.0, 0.0))
        return ordered

    def ranked_all(self) -> list[CandidateId]:
        """Every candidate with a mean, best first, whether it may be best or not; the older first on a tie."""
        ordered = sorted(c for c in self.entries if self.mean(c) is not None)
        ordered.sort(key=lambda c: self.mean(c) or 0.0, reverse=self.primary.direction.better(1.0, 0.0))
        return ordered

    def best(self) -> CandidateId | None:
        """The best: the first of ``ranked``, else the baseline, else None before the campaign has a baseline."""
        return next(iter(self.ranked), self.baseline)

    def line(self, c: CandidateId) -> Iterator[CandidateId]:
        """The candidates on the chain of parents of ``c``, its parent first."""
        seen = {c}
        at = self.entries[c].parent if c in self.entries else None
        while at is not None and at in self.entries and at not in seen:
            seen.add(at)
            yield at
            at = self.entries[at].parent

    def line_best(self, c: CandidateId) -> CandidateId | None:
        """The best of the line of ``c``: the first of ``ranked`` on its chain of parents, or None."""
        mine = set(self.line(c))
        return next((r for r in self.ranked if r in mine), None)

    def confirm_due(self, c: CandidateId) -> bool:
        """Whether ``c`` is to be confirmed: it beats the best (``beats_best``), and it needs more evaluations.

        It needs none once it is confirmed, once its crashes reach the count, or once someone cancelled its evaluation
        (``Status.needs``). Until then piloop evaluates it.
        """
        return self.beats_best(c) and self.entries[c].status.needs(self.confirm) > 0

    def seal_due(self, c: CandidateId) -> bool:
        """Whether the sealed split is to score ``c``: the campaign has one, ``c`` beats the best, and the split has not.

        A crash of the sealed command is the split's answer too, and a cancel stands (``Status.settled``): only a new
        sealed command, or an ask, runs it again. A confirmed candidate is due too, so a new sealed command scores the one that would be best.
        """
        return self.sealed and not self.entries[c].seal.settled and self.beats_best(c)

    def beats_best(self, c: CandidateId) -> bool:
        """Whether the mean of ``c``, not excluded and not the best, beats the best's."""
        mine, best = self.mean(c), self.best()
        if mine is None or self.entries[c].excluded is not None or best == c:
            return False
        theirs = self.mean(best)
        return theirs is None or self.primary.direction.better(mine, theirs)

    def sigma(self) -> float | None:
        """The standard deviation of the best's evaluations of the primary: 0 for an evaluator whose runs agree."""
        if self.confirm == 1:
            return 0.0
        best = self.best()
        values = (
            [] if best is None or best not in self.entries else self.entries[best].samples.get(self.primary.name, [])
        )
        return stdev(values) if len(values) >= 2 else None

    def gain(self, c: CandidateId, other: CandidateId | None) -> float | None:
        """How much better the primary of ``c`` is than ``other``'s: positive when better."""
        mine, theirs = self.mean(c), self.mean(other)
        if mine is None or theirs is None or other == c:
            return None
        return self.primary.direction.gain(mine, theirs)

    def standing(self, c: CandidateId) -> Standing:
        """Where ``c`` stands. ``c`` must be in the pool."""
        entry, best, ranked = self.entries[c], self.best(), self.ranked
        line, sigma = self.line_best(c), self.sigma()
        deltas = {'vs_parent': entry.parent, 'vs_line': line, 'vs_best': best}
        within = tuple(
            key
            for key, other in deltas.items()
            if other is not None
            and (key != 'vs_line' or other not in (best, entry.parent))
            and (gain := self.gain(c, other)) is not None
            and sigma is not None
            and other is not None
            and abs(gain) <= WITHIN * sigma * sqrt(1 / max(entry.status.scored, 1) + 1 / max(self._n(other), 1))
        )
        confirmed = self.confirmed(c)
        return Standing(
            candidate=c,
            track=self.track.name,
            metric=self.primary.name,
            mean=self.mean(c),
            status=entry.status,
            confirmed=confirmed,
            refused=self.refusal(c) if self._would_be_best(c) else None,
            excluded=entry.excluded,
            best=best,
            rank=ranked.index(c) + 1 if c in ranked else (1 if c == best else None),
            of=max(len(ranked), 1),
            vs_parent=self.gain(c, entry.parent),
            vs_line=self.gain(c, line) if line not in (None, best, entry.parent) else None,
            vs_best=self.gain(c, best),
            sigma=sigma,
            sealed=entry.sealed if confirmed else None,
            within=within,
        )

    def _would_be_best(self, c: CandidateId) -> bool:
        """Whether ``c`` is confirmed, not excluded, and better than the best: only the best rule keeps it from it."""
        mine, theirs = self.mean(c), self.mean(self.best())
        if mine is None or not self.confirmed(c) or self.entries[c].excluded is not None or c == self.baseline:
            return False
        return theirs is None or self.primary.direction.better(mine, theirs)

    def _n(self, c: CandidateId) -> int:
        return self.entries[c].status.scored if c in self.entries else 0

    def worth_branching(self, k: int = 3) -> list[CandidateId]:
        """The best, then the ``k`` best of ``ranked`` on other lines than the best's, one per line.

        Two candidates share a line when one is an ancestor of the other.
        """
        best = self.best()
        if best is None:
            return []
        taken = [best]
        for c in self.ranked:
            if len(taken) > k:
                break
            mine = {c, *self.line(c)}
            if not any(t in mine or c in set(self.line(t)) for t in taken):
                taken.append(c)
        return taken


def _statuses(s: Session, ours: Select[CandidateId], evaluator_id: EvaluatorId | None) -> dict[CandidateId, Status]:
    """The ``Status`` under ``evaluator_id`` of each candidate of ``ours`` that it measured or that a cancel holds."""
    rows: dict[CandidateId, list[tuple[dict[str, float], str | None, RungName | None, str | None]]] = {}
    evaluated = (
        select(Measurement.candidate_id, Measurement.metrics, Measurement.error, Measurement.rung, Measurement.stopped)
        .where(Measurement.candidate_id.in_(ours), Measurement.evaluator_id == evaluator_id)
        .order_by(Measurement.id)
    )
    for c, values, error, rung, stopped in s.execute(evaluated):
        rows.setdefault(c, []).append((values, error, rung, stopped))
    held = select(Cancel.candidate_id).where(Cancel.candidate_id.in_(ours), Cancel.evaluator_id == evaluator_id)
    cancelled = set(s.scalars(held))
    return {c: Status.of(rows.get(c, ()), cancelled=c in cancelled) for c in rows.keys() | cancelled}


def _samples(rows: Sequence[dict[str, float]]) -> dict[str, list[float]]:
    found: dict[str, list[float]] = {}
    for row in rows:
        for name, value in row.items():
            found.setdefault(name, []).append(value)
    return found


def best_of(s: Session, campaign: Campaign, track: Track | None = None) -> Candidate | None:
    """Return the best of ``track``, by default the first track, or None before the campaign has a baseline.

    Right after an evaluator change, while the current one has scored nothing but the baseline, the best of the first
    track is the one before the change (``carried``): the best stays where it was until the new scores say otherwise.
    """
    pool = Pool.of(s, campaign, track or campaign.config.all_tracks[0])
    found = pool.best()
    before = carried(s, campaign) if track is None and found == pool.baseline else None
    if before is not None and pool.mean(before.id) is None:
        return before
    return s.get_one(Candidate, found) if found is not None else None


def branching(s: Session, campaign: Campaign, k: int = 3) -> list[str]:
    """The lines of a worker's brief that name what is worth branching from, beside the best (``worth_branching``)."""
    pool = Pool.of(s, campaign, campaign.config.all_tracks[0])
    found = pool.worth_branching(k)[1:]
    if not found:
        return []
    lines = ['- Other lines worth branching from, with `piloop idea claim <id> --base <candidate>`:']
    for c in found:
        entry, gain = pool.entries[c], pool.gain(c, pool.best())
        versus = '' if gain is None else f', {number(gain, signed=True)} vs the best'
        summary = s.get_one(Candidate, c).summary or 'no summary'
        mean = pool.mean(c)
        assert mean is not None  # ranked: it has a mean
        evals = f'{entry.status.scored} eval' + ('s' if entry.status.scored != 1 else '')
        lines.append(f'  - candidate#{c}: {pool.primary.name} {number(mean)} over {evals}{versus} — {summary}')
    return lines


@dataclass(frozen=True)
class BestScore:
    """The best of one track, its primary's score, and its gain over the baseline's."""

    track: Track
    candidate: CandidateId | None
    """None before the campaign has a baseline."""
    value: float | None
    """None when the current evaluator has not measured it."""
    delta: float | None
    """None when the best is the baseline, or either is not measured."""


def best_scores(s: Session, campaign: Campaign) -> list[BestScore]:
    """Return the best of each track of ``campaign``, with its score and its gain over the baseline."""
    found = []
    for track in campaign.config.all_tracks:
        pool = Pool.of(s, campaign, track)
        best = pool.best()
        gain = pool.gain(best, pool.baseline) if best is not None else None
        found.append(BestScore(track, best, pool.mean(best), gain))
    return found


def checkout_candidate(s: Session, campaign: Campaign) -> Candidate:
    """Return what an agent without a run gets checked out: the best of the first track.

    Raises:
        NoResultFound: The campaign has no candidate yet (``baseline``).
    """
    found = best_of(s, campaign)
    if found is None:
        raise NoResultFound(f'campaign {campaign.name} has no candidate yet')
    return found


def carried(s: Session, campaign: Campaign) -> Candidate | None:
    """The best of the first track under the evaluator before the current one, when it is not the baseline.

    After an evaluator change no candidate has a score: the daemon measures this one, so that the best stays where it
    was until the new scores say otherwise.
    """
    first = baseline(s, campaign)
    visible = select(Measurement.evaluator_id).where(
        Measurement.candidate_id.in_(select(Candidate.id).where(Candidate.campaign_id == campaign.id)),
        Measurement.evaluator_id != campaign.evaluator_id,
    )
    hidden = {
        e.id
        for e in s.scalars(select(Evaluator).where(Evaluator.campaign_id == campaign.id))
        if split_of(e) is Split.SEALED
    }
    before = max((e for e in s.scalars(visible.distinct()) if e not in hidden), default=None)
    if before is None or first is None:
        return None
    best = Pool.of(s, campaign, campaign.config.all_tracks[0], before).best()
    return None if best is None or best == first.id else s.get_one(Candidate, best)
