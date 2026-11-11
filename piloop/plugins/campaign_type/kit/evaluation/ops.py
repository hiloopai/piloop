"""The operations on measurements that the API and the CLI run: a measurement added by hand, and holds."""

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import delete
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session

from piloop.actor import Actor
from piloop.campaign import escalations
from piloop.campaign.ops import Refused, existing, hold
from piloop.campaign.stop import gather, held
from piloop.config import CampaignConfig
from piloop.db.models import Campaign, Pause, utcnow
from piloop.ids import TargetKind, TargetRef
from piloop.plugins.campaign_type.kit.candidates.ids import CANDIDATE, CandidateId, Origin
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.evaluation import task_holds
from piloop.plugins.campaign_type.kit.evaluation.ids import EvaluatorId, Measured, Reading
from piloop.plugins.campaign_type.kit.evaluation.measure import record, status
from piloop.plugins.campaign_type.kit.evaluation.tables import Evaluation, EvaluationTaskFailure, Measurement
from piloop.plugins.campaign_type.kit.evaluation.wire import Report
from piloop.plugins.campaign_type.kit.pool.pool import UNSCORED, Pool, track_of
from piloop.plugins.escalation.contract import Choice, EscalationView
from piloop.plugins.fact_source.facts import reading


def hold_on_failed_baseline(s: Session, candidate: Candidate, logs: Path | None) -> None:
    """Hold the launches of the campaign (``hold``) when ``candidate`` is its baseline and no evaluator scored it.

    It holds once the current evaluator's evaluations of it crashed (``Measured.CRASHED``). Nothing can be compared with a baseline without a score, and no agent should spend on a campaign that has none.
    One that an older evaluator scored has one, and its campaign goes on. The pause measures what is asked, so it
    can end on a measured baseline. The reason is the last error, with ``logs``, where that evaluation left its logs,
    and what to do. The caller commits.
    """
    campaign = s.get_one(Campaign, candidate.campaign_id)
    now = status(candidate, campaign.evaluator_id)
    ever = any(m.reading is Reading.SCORE for m in candidate.measurements)
    if candidate.origin is not Origin.BASELINE or now.state is not Measured.CRASHED or ever:
        return
    *_, last = (m.error for m in candidate.measurements if m.reading is Reading.CRASH)
    where = '' if logs is None else f'; its logs are in {logs}'
    fix = f'fix it, then `piloop candidate remeasure {candidate.id}` and `piloop resume {campaign.name}`'
    hold(campaign, Pause.LAUNCHES, f'the baseline failed to evaluate: {last}{where}; {fix}')


def hold_on_cancelled_baseline(s: Session, candidate: Candidate, actor: Actor) -> None:
    """Hold every measurement of a campaign (``hold``) whose baseline never measured, when ``actor`` cancelled its evaluation.

    A cancel records nothing, and it stands (``Cancel``): nothing measures the baseline until someone asks. The pause
    says so. The caller commits.
    """
    campaign = s.get_one(Campaign, candidate.campaign_id)
    if candidate.origin is not Origin.BASELINE or candidate.measurements:
        return
    hold(
        campaign,
        Pause.ALL,
        f'{actor} cancelled the evaluation of the baseline; `piloop candidate remeasure {candidate.id}` and'
        f' `piloop resume {campaign.name}` measure it again',
    )


AVAILABLE_EVAL_IMAGES = reading('available_eval_images')


def hold_if_exhausted(s: Session, campaign: Campaign, held_keys: set[str]) -> bool:
    """Hold a campaign whose evaluator inventory is zero or whose entire task roster is held."""
    if task_holds.CAMPAIGN in held_keys:
        return True
    images = gather(s, campaign, campaign.config, [AVAILABLE_EVAL_IMAGES], utcnow()).get(AVAILABLE_EVAL_IMAGES)
    if images == 0:
        reason = 'the evaluator inventory reports zero usable eval images'
        repair = 'Restore an eval image and verify the inventory reports it.'
    elif task_holds.all_tasks_held(campaign, held_keys):
        reason = 'no task in the campaign TASKS.txt is available for evaluation'
        repair = (
            'Restore an eligible task in TASKS.txt or retry a repaired task with `piloop candidate remeasure <id>`.'
        )
    else:
        return False
    resume = f'`piloop resume {campaign.name}`'
    hold(campaign, Pause.ALL, f'{reason}. {repair} Then run {resume}.')
    table = EvaluationTaskFailure
    s.execute(
        insert(table)
        .values(campaign_id=campaign.id, task_key=task_holds.CAMPAIGN, held=True, failures=0)
        .on_conflict_do_update(index_elements=[table.campaign_id, table.task_key], set_={'held': True})
    )
    s.execute(
        delete(Evaluation).where(Evaluation.started_at.is_(None), Evaluation.candidate.has(campaign_id=campaign.id))
    )
    escalation = EscalationView(
        headline=f'Campaign {campaign.name} paused because {reason}.',
        why='New workers and evaluations wait until an eligible task has a usable evaluator.',
        choices=(
            Choice(id='A', label=f'{repair} Then run {resume}.', result='The campaign can schedule work again.'),
            Choice(id='B', label='Leave the campaign paused.', result='Queued work waits.'),
        ),
        recommended='A',
        details=reason,
    )
    escalations.escalate_once(s, TargetRef(TargetKind.CAMPAIGN, campaign.id), escalation, priority=1)
    return True


def hold_on_infra_errors(
    s: Session, candidate_id: CandidateId, evaluation: int, evaluator_id: EvaluatorId, error: str, limit: int
) -> None:
    """Count one failed evaluation attempt, in the transaction that removed it. Escalate once at the threshold."""
    candidate = s.get_one(Candidate, candidate_id)
    table = EvaluationTaskFailure
    count = table.failures + 1
    attempt = (
        insert(table)
        .values(campaign_id=candidate.campaign_id, task_key=task_holds.key(candidate), failures=1, held=limit == 1)
        .on_conflict_do_update(
            index_elements=[table.campaign_id, table.task_key],
            set_={'failures': count, 'held': count >= limit},
            where=table.held.is_(False),
        )
        .returning(table.failures, table.held)
    )
    changed = s.execute(attempt).one_or_none()
    if changed is None or not changed.held:
        return
    affected = Candidate.campaign_id == candidate.campaign_id
    if candidate.task_id:
        campaign = s.get_one(Campaign, candidate.campaign_id)
        if hold_if_exhausted(s, campaign, task_holds.held_keys(s, campaign.id)):
            return
        affected &= Candidate.task_id == candidate.task_id
        scope = f'task {candidate.task_id!r}'
        resume = f'`piloop candidate remeasure {candidate_id}`'
    else:
        campaign = s.get_one(Campaign, candidate.campaign_id)
        scope = f'campaign {campaign.name}'
        resume = f'`piloop resume {campaign.name}`'
        hold(
            campaign,
            Pause.ALL,
            f'{changed.failures} consecutive infrastructure failures on submissions without a TASK: {error}; fix it, then {resume}',
        )
    s.execute(delete(Evaluation).where(Evaluation.started_at.is_(None), Evaluation.candidate.has(affected)))
    asked = EscalationView(
        headline=f'Evaluations paused for {scope} after {changed.failures} consecutive infrastructure failures.',
        why=(
            f'The evaluator could not measure {scope}. Its pending submissions can be evaluated after the cause is fixed.'
        ),
        choices=(
            Choice(
                id='A',
                label=f'Fix the cause, then {resume}.',
                result=f'PiLoop resumes evaluations for {scope} and measures candidate#{candidate_id} again.',
            ),
            Choice(
                id='B',
                label='Leave these evaluations on hold.',
                result=f'Evaluations for {scope} wait for an operator.',
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
