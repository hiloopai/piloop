"""The operations on measurements that the API and the CLI run: a measurement added by hand, and holds."""

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from piloop.actor import Actor
from piloop.campaign import escalations
from piloop.campaign.ops import Refused, existing, hold
from piloop.campaign.stop import held
from piloop.config import CampaignConfig
from piloop.db.models import Campaign, Pause
from piloop.ids import TargetKind, TargetRef
from piloop.plugins.campaign_type.kit.candidates.ids import CANDIDATE, CandidateId, Origin
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.evaluation.ids import EvaluatorId
from piloop.plugins.campaign_type.kit.evaluation.measure import record
from piloop.plugins.campaign_type.kit.evaluation.tables import Measurement
from piloop.plugins.campaign_type.kit.evaluation.wire import Report
from piloop.plugins.campaign_type.kit.pool.pool import UNSCORED, Pool, track_of
from piloop.plugins.escalation.contract import Choice, EscalationView


def hold_on_failed_baseline(s: Session, candidate: Candidate, logs: Path | None) -> None:
    """Hold the launches of the campaign (``hold``) when ``candidate`` is its baseline and each evaluation of it failed.

    Nothing can be compared with a baseline without a score, and no agent should spend on a campaign that has none.
    One that an older evaluator measured has one, and its campaign goes on. The pause measures what is asked, so it
    can end on a measured baseline. The reason is the last error, with ``logs``, where that evaluation left its logs,
    and what to do. The caller commits.
    """
    errors = [m.error for m in candidate.measurements]
    campaign = s.get_one(Campaign, candidate.campaign_id)
    if candidate.origin is not Origin.BASELINE or None in errors or not errors:
        return
    where = '' if logs is None else f'; its logs are in {logs}'
    fix = f'fix it, then `piloop candidate remeasure {candidate.id}` and `piloop resume {campaign.name}`'
    hold(campaign, Pause.LAUNCHES, f'the baseline failed to evaluate: {errors[-1]}{where}; {fix}')


def hold_on_cancelled_baseline(s: Session, candidate: Candidate, actor: Actor) -> None:
    """Hold every measurement of a campaign (``hold``) whose baseline never measured, when ``actor`` cancelled its evaluation.

    A cancel records nothing, and the daemon measures a baseline that has no score at once: the pause holds that until
    someone resumes the campaign. The caller commits.
    """
    campaign = s.get_one(Campaign, candidate.campaign_id)
    if candidate.origin is not Origin.BASELINE or candidate.measurements:
        return
    hold(
        campaign,
        Pause.ALL,
        f'{actor} cancelled the evaluation of the baseline; `piloop resume {campaign.name}` measures it again',
    )


def hold_on_infra_errors(
    s: Session, candidate_id: CandidateId, evaluation: int, evaluator_id: EvaluatorId, error: str, tries: int
) -> None:
    """Hold the measurements of the candidate's campaign (``hold``), and escalate: its evaluation failed for the machine.

    It failed ``tries`` times in a row for a cause that is not the candidate's (``[evaluation] stop_after_tries``). That
    cause is the machine's or the evaluator's: the next evaluations would fail too, and the daemon would ask for this
    one again. Nothing is recorded, so the candidate waits as it was, and a resume evaluates it again. The escalation
    names the candidate, the evaluation, the evaluator and the last error. The caller commits.
    """
    candidate = s.get_one(Candidate, candidate_id)
    campaign = s.get_one(Campaign, candidate.campaign_id)
    failed = f'evaluation#{evaluation} of candidate#{candidate_id} under evaluator#{evaluator_id} failed {tries} times'
    resume = f'`piloop resume {campaign.name}`'
    hold(campaign, Pause.ALL, f'{failed} for a cause that is not its own: {error}; fix it, then {resume}')
    asked = EscalationView(
        headline=f"Evaluations of {campaign.name} fail for a cause that is not their candidate's: fix it, then resume.",
        why=(
            f"{failed} in a row for a cause that is not the candidate's, such as a lost machine or a retired"
            f' evaluator. piloop recorded no measurement, does not retry it, and paused the measurements of'
            f' {campaign.name}.'
        ),
        choices=(
            Choice(
                id='A',
                label=f'Fix the cause, then {resume}.',
                result=f'piloop evaluates candidate#{candidate_id} again.',
            ),
            Choice(
                id='B',
                label=f'Change the evaluator in piloop.toml, then `piloop campaign sync` and {resume}.',
                result=f'The new evaluator measures candidate#{candidate_id}.',
            ),
        ),
        recommended='A',
        details=f'candidate#{candidate_id}, evaluation#{evaluation}, evaluator#{evaluator_id}: {error}',
    )
    escalations.escalate_once(s, TargetRef(CANDIDATE, candidate_id), asked, priority=1)


def sealed_refused(s: Session, campaign: Campaign, error: str) -> None:
    """Escalate, once, that the sealed command writes a result that piloop refuses. The caller commits.

    No candidate can be best while that lasts: the sealed split scores none.
    """
    asked = EscalationView(
        headline=f'The sealed command of {campaign.name} writes results that piloop refuses. No candidate can be best.',
        why=(
            'The command in [evaluator] sealed scores each candidate that piloop confirms. piloop refuses its results,'
            ' so the best stays where it is.'
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
                result='The best is chosen on the visible split alone.',
            ),
        ),
        recommended='A',
        details=f'campaign {campaign.name}: {error}',
    )
    escalations.escalate_once(s, TargetRef(TargetKind.CAMPAIGN, campaign.id), asked, priority=1)


@dataclass(frozen=True)
class Added:
    """What ``add_measurement`` recorded, and where the candidate stands now."""

    measurement: Measurement
    standing: str
    """Where the candidate stands in the pool of its track, in one line."""


def add_measurement(s: Session, candidate_id: CandidateId, report: Report, *, actor: Actor) -> Added:
    """Record ``report`` as a measurement of the candidate under the campaign's current evaluator.

    Its mean includes it at once, so the pool and its best follow. Emits ``MeasurementRecorded``, shaped like the
    daemon's.

    Raises:
        Refused: the campaign has no evaluator yet, the candidate never enters the pool (``skipped``, ``recovered``),
            the report names a metric that ``piloop.toml`` does not, or a report without an error lacks one.
    """
    candidate = existing(s, Candidate, candidate_id, 'candidate')
    campaign = s.get_one(Campaign, candidate.campaign_id)
    if campaign.evaluator_id is None:
        raise Refused(f'campaign {campaign.name} has no evaluator yet: run `piloop init`')
    if candidate.origin in UNSCORED:
        raise Refused(
            f'candidate#{candidate.id} is {candidate.origin}: it never enters the pool; submit its tree to score it'
        )
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
    standing = Pool.of(s, campaign, track_of(cfg, candidate)).standing(candidate.id)
    return Added(measurement, standing.told())


def measuring_held(s: Session, campaign: Campaign, cfg: CampaignConfig) -> str:
    """Return `` once …``: when the daemon measures the campaign, as the scheduler's pass decides, or '' for now.

    A stopped campaign is measured too, but only what someone asked for (``ledger.due``).
    """
    if campaign.paused is Pause.ALL:
        return f' once campaign {campaign.name} is resumed (its evaluations are paused)'
    if (why := held(s, campaign, cfg)) is not None:
        return f' once the stop rule no longer holds it ({why})'
    return ''
