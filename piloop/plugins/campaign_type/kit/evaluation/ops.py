"""The operations on measurements that the API and the CLI run: a measurement added by hand, and holds."""

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from piloop.actor import Actor
from piloop.campaign import escalations
from piloop.campaign.ops import Refused, existing, pause
from piloop.campaign.stop import held
from piloop.config import CampaignConfig
from piloop.db.events import emit
from piloop.db.models import Campaign, Pause
from piloop.ids import TargetKind, TargetRef
from piloop.plugins.campaign_type.kit.candidates.ids import CANDIDATE, CandidateId, Origin, Outcome
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.evaluation.measure import record
from piloop.plugins.campaign_type.kit.evaluation.tables import Measurement
from piloop.plugins.campaign_type.kit.evaluation.wire import Report
from piloop.plugins.campaign_type.kit.verdicts.judge import RejudgeRequested
from piloop.plugins.campaign_type.kit.verdicts.verdict import (
    label,
    pending,
    rejudgeable,
)
from piloop.plugins.escalation.contract import Choice, EscalationView


def hold_on_failed_baseline(s: Session, candidate: Candidate, logs: Path | None) -> None:
    """Pause the launches of an unpaused campaign when ``candidate`` is its baseline and each evaluation of it failed.

    Nothing can be judged against a baseline without a score, and no agent should spend on a campaign that has none.
    One that an older evaluator measured has one, and its campaign goes on. The pause measures what is asked, so it
    can end on a measured baseline. The reason is the last error, with ``logs``, where that evaluation left its logs,
    and what to do. The caller commits.
    """
    errors = [m.error for m in candidate.measurements]
    campaign = s.get_one(Campaign, candidate.campaign_id)
    if candidate.origin is not Origin.BASELINE or campaign.paused is not None or None in errors or not errors:
        return
    where = '' if logs is None else f'; its logs are in {logs}'
    fix = f'fix it, then `piloop candidate remeasure {candidate.id}` and `piloop resume {campaign.name}`'
    pause(s, campaign.id, evals=False, reason=f'the baseline failed to evaluate: {errors[-1]}{where}; {fix}')


def hold_on_cancelled_baseline(s: Session, candidate: Candidate, actor: Actor) -> None:
    """Pause every measurement of a campaign whose baseline never measured, when ``actor`` cancelled its evaluation.

    A cancel records nothing, and the daemon measures a head that has no score at once: the pause holds that until
    someone resumes the campaign. The caller commits.
    """
    campaign = s.get_one(Campaign, candidate.campaign_id)
    if candidate.origin is not Origin.BASELINE or campaign.paused is Pause.ALL or candidate.measurements:
        return
    why = f'{actor} cancelled the evaluation of the baseline; `piloop resume {campaign.name}` measures it again'
    pause(s, campaign.id, evals=True, reason=why)


def noise_unknown(s: Session, campaign: Campaign, baseline: Candidate, metric: str) -> None:
    """Escalate, once, that the baseline's evaluations give no noise of ``metric``: too few succeeded.

    Until someone measures the baseline again, the ``noise`` verdict rule keeps nothing. The caller commits.
    """
    runs = campaign.config.evaluator.noise_runs
    asked = EscalationView(
        headline=f'The noise of {metric} is unknown, so no candidate of {campaign.name} can be kept. What now?',
        why=(
            f'Fewer than 2 of the evaluations of the baseline candidate#{baseline.id} succeeded, out of the'
            f' {runs} of [evaluator] noise_runs.'
        ),
        choices=(
            Choice(
                id='A',
                label=f'Fix the evaluator, then `piloop candidate remeasure {baseline.id}`.',
                result='Its new evaluations give the noise, and the keeps go on.',
            ),
            Choice(id='B', label='Set [evaluator] noise_runs = 1.', result='There is no noise: any gain keeps.'),
        ),
        recommended='A',
        details=f'campaign {campaign.name}, baseline candidate#{baseline.id}, metric {metric}',
    )
    escalations.escalate_once(s, TargetRef(TargetKind.CAMPAIGN, campaign.id), asked, priority=1)


def sealed_refused(s: Session, campaign: Campaign, error: str) -> None:
    """Escalate, once, that the sealed command writes a result that piloop refuses. The caller commits.

    No candidate gets a verdict while that lasts: each would-be keep waits, and its sealed evaluation runs again.
    """
    asked = EscalationView(
        headline=f'The sealed command of {campaign.name} writes results that piloop refuses. No candidate can be kept.',
        why=(
            'The command in [evaluator] sealed scores each candidate that would be kept. piloop refuses its results.'
            ' These candidates wait, and piloop runs the command again later.'
        ),
        choices=(
            Choice(
                id='A',
                label='Fix the command in [evaluator] sealed, then `piloop campaign sync`.',
                result='The waiting candidates are scored with the fixed command.',
            ),
            Choice(
                id='B',
                label='Remove [evaluator] sealed, then `piloop campaign sync`.',
                result='Keeps are judged on the visible split alone.',
            ),
        ),
        recommended='A',
        details=f'campaign {campaign.name}: {error}',
    )
    escalations.escalate_once(s, TargetRef(TargetKind.CAMPAIGN, campaign.id), asked, priority=1)


@dataclass(frozen=True)
class Added:
    """What ``add_measurement`` recorded, and what it does to the candidate's verdict."""

    measurement: Measurement
    rejudged: bool
    """Whether the daemon judges the candidate again (``RejudgeRequested``)."""
    effect: str
    """What happens to the verdict, in one line: when the daemon judges it again, or why the verdict stays."""


def add_measurement(s: Session, candidate_id: CandidateId, report: Report, *, actor: Actor) -> Added:
    """Record ``report`` as a measurement of the candidate under the campaign's current evaluator.

    The daemon judges the candidate again when its verdict is the daemon's ``crash``, ``discard`` or ``interesting``
    on a run, or it is a run's commit that the daemon never measured (``verdict.rejudgeable``), with every measurement
    under the current evaluator. It does that when it measures
    the campaign again: not while the campaign is paused with its evaluations, or held by its stop rule. Any
    other verdict stays, and a keep's mean, which later runs from it must beat, includes the new measurement. Emits
    ``MeasurementRecorded``, shaped like the daemon's, and the request (``RejudgeRequested``).

    Raises:
        Refused: the campaign has no evaluator yet, the candidate is pending (the daemon is measuring and judging it),
            the report names a metric that ``piloop.toml`` does not, or a report without an error lacks one.
    """
    candidate = existing(s, Candidate, candidate_id, 'candidate')
    campaign = s.get_one(Campaign, candidate.campaign_id)
    if campaign.evaluator_id is None:
        raise Refused(f'campaign {campaign.name} has no evaluator yet: run `piloop init`')
    if pending(candidate):
        raise Refused(f'candidate#{candidate.id} is pending: the daemon judges it now; add the measurement after that')
    cfg = campaign.config
    names = {m.name for m in cfg.metrics}
    if unknown := sorted(report.metrics.keys() - names):
        raise Refused(f'piloop.toml has no metric {", ".join(unknown)}: its metrics are {", ".join(sorted(names))}')
    missing = sorted(names - report.metrics.keys())
    if report.error is None and missing:
        raise Refused(f'the report has no {", ".join(missing)}: give every metric, or the error')
    measurement = record(
        s,
        candidate,
        campaign.evaluator_id,
        actor,
        metrics=report.metrics,
        error=report.error,
        seconds=report.seconds,
    )
    if not rejudgeable(candidate):
        return Added(measurement, False, _stays(candidate))
    asked = RejudgeRequested(measurement=measurement.id)
    emit(s, asked, TargetRef(CANDIDATE, candidate.id), actor)
    later = measuring_held(s, campaign, cfg)
    return Added(measurement, True, f'the daemon judges it again with all its measurements{later}')


def _stays(candidate: Candidate) -> str:
    if candidate.verdict is Outcome.KEEP:
        return 'its keep stays; the runs that start from it must beat its new mean'
    return f'its verdict stays: {label(candidate)}, set by {candidate.verdict_by or "nobody"}'


def measuring_held(s: Session, campaign: Campaign, cfg: CampaignConfig) -> str:
    """Return `` once …``: when the daemon measures the campaign, as the scheduler's pass decides, or '' for now.

    A stopped campaign is measured too, but only what someone asked for (``ledger.due``).
    """
    if campaign.paused is Pause.ALL:
        return f' once campaign {campaign.name} is resumed (its evaluations are paused)'
    if (why := held(s, campaign, cfg)) is not None:
        return f' once the stop rule no longer holds it ({why})'
    return ''
