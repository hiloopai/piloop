"""The best rule at work: the case of each step that a fold waits at, the rule's answer, and its row.

The best of a track is a query (``Pool.fold``), and a query cannot wait for a program. So the daemon asks the rule
here, outside any query, and stores each answer (``Judgement``): the fold reads it on its next walk.
"""

import time
from collections.abc import Callable
from typing import assert_never

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session

from piloop.db.models import Campaign
from piloop.db.session import Database, write_transaction
from piloop.ids import CampaignId
from piloop.plugins.campaign_type.kit.candidates.ids import CandidateId
from piloop.plugins.campaign_type.kit.evaluation.evaluate import UNITS
from piloop.plugins.campaign_type.kit.evaluation.ids import MeasurementId
from piloop.plugins.campaign_type.kit.evaluation.tables import Measurement
from piloop.plugins.campaign_type.kit.pool.best_rule.contract import Case, CaseSample, Keep, Refuse, Scores, Unanswered
from piloop.plugins.campaign_type.kit.pool.ops import unanswered
from piloop.plugins.campaign_type.kit.pool.pool import Pool, Step, before
from piloop.plugins.campaign_type.kit.pool.tables import Answer, Judgement
from piloop.plugins.cas.contract import Cas


def pools(s: Session, campaign: Campaign) -> list[Pool]:
    """The pools whose best the campaign reads.

    Each track's under the current evaluator, then the first track's under the evaluator before it: its best is
    ``carried``.
    """
    tracks = campaign.config.all_tracks
    found = [Pool.of(s, campaign, t) for t in tracks]
    if (old := before(s, campaign)) is not None:
        found.append(Pool.of(s, campaign, tracks[0], old))
    return found


def waiting(s: Session, campaign: Campaign) -> tuple[Pool, Step] | None:
    """The first step that a fold of the campaign waits at and that nobody asked the rule yet, with its pool, or None."""
    for pool in pools(s, campaign):
        fold = pool.fold
        if fold.waiting is not None and fold.error is None:
            return pool, fold.waiting
    return None


def judge(db: Database, cas: Cas, campaign_id: CampaignId) -> None:
    """Ask the best rule each step that a fold of the campaign waits at, and store each answer, until none waits.

    It stops at an answer that is ``Unanswered``: that is stored too, so the rule is not asked the same case again, and
    piloop escalates. The fold waits there until a new case or a changed ``[plugins.best_rule]`` table. Two judges of
    one case store one row: the first answer stays. The rule runs outside any session: a program can take a minute.
    """
    while True:
        with db.session() as s:
            found = waiting(s, s.get_one(Campaign, campaign_id))
            if found is None:
                return
            pool, step = found
            try:
                case: Case | str = case_of(s, cas, pool, step)
            except LookupError as e:  # a blob that the CAS lost: the rule cannot be asked, so it does not answer
                case = f'cannot read the case: {e}'
        started = time.monotonic()
        answer = _asked(pool, case)
        seconds = time.monotonic() - started
        verdict, reason = _row(answer)
        assert pool.evaluator is not None  # a pool with a ready candidate has an evaluator
        with db.session() as s:
            write_transaction(s)
            row = dict(
                candidate_id=step.candidate,
                incumbent_id=step.incumbent,
                evaluator_id=pool.evaluator,
                key=step.key,
                answer=verdict,
                reason=reason,
                seconds=seconds,
            )
            s.execute(insert(Judgement).values(**row).on_conflict_do_nothing())
            if isinstance(answer, Unanswered):
                unanswered(s, s.get_one(Campaign, campaign_id), step.candidate, answer.error)
            s.commit()
        if isinstance(answer, Unanswered):
            return


def _asked(pool: Pool, case: Case | str) -> Keep | Refuse | Unanswered:
    if isinstance(case, str):
        return Unanswered(case)
    try:
        return pool.rule.judge(case)
    except Exception as e:  # a rule from a plugin that raises cannot keep a candidate: it does not answer
        return Unanswered(f'{type(e).__name__}: {e}')


def _row(answer: Keep | Refuse | Unanswered) -> tuple[Answer, str | None]:
    match answer:
        case Keep(reason=reason):
            return Answer.KEEP, reason
        case Refuse(reason=reason):
            return Answer.REFUSE, reason
        case Unanswered(error=error):
            return Answer.UNANSWERED, error
        case never:
            assert_never(never)


def case_of(s: Session, cas: Cas, pool: Pool, step: Step) -> Case:
    """The case of ``step``, with every sample of each side and its per-unit results from the CAS (``case``).

    Raises:
        LookupError: The CAS has not the per-unit results of a sample.
    """
    ids = {i for c in sides(pool, step) for i in (*pool.entries[c].ids.samples, *pool.entries[c].ids.sealed)}
    rows = {m.id: m for m in s.scalars(select(Measurement).where(Measurement.id.in_(ids)))}
    blobs: dict[MeasurementId, dict[str, dict[str, float]]] = {}
    for m in rows.values():
        if m.units is not None:
            if (data := cas.get(m.units)) is None:
                raise LookupError(f'the per-unit results {m.units} of measurement {m.id} are not in the CAS')
            blobs[m.id] = UNITS.validate_json(data)
    return case(
        pool,
        step,
        lambda i: CaseSample(id=i, rung=rows[i].rung, metrics=dict(rows[i].metrics), units=blobs.get(i, {})),
    )


def sides(pool: Pool, step: Step) -> list[CandidateId]:
    """The candidates of the case of ``step``: the candidate, the incumbent, the baseline, and its parent in the pool."""
    assert pool.baseline is not None  # a step's campaign has a baseline
    parent = pool.entries[step.candidate].parent
    return [
        step.candidate,
        step.incumbent,
        pool.baseline,
        *([parent] if parent is not None and parent in pool.entries else []),
    ]


def case(pool: Pool, step: Step, sample: Callable[[MeasurementId], CaseSample]) -> Case:
    """The case of ``step``: the candidate, the incumbent, the baseline and the parent.

    ``sample`` gives each of their measurements.
    """
    assert pool.baseline is not None  # a step's campaign has a baseline
    parent = pool.entries[step.candidate].parent

    def scores(c: CandidateId) -> Scores:
        entry = pool.entries[c]
        return Scores(
            name=entry.name,
            means=entry.means,
            measured=entry.status.scored,
            sealed=entry.sealed,
            id=c,
            parent=entry.parent,
            samples=tuple(map(sample, entry.ids.samples)),
            sealed_samples=tuple(map(sample, entry.ids.sealed)),
        )

    primary, guardrails, sealed, everything = pool.criteria()
    return Case(
        primary=primary,
        guardrails=guardrails,
        candidate=scores(step.candidate),
        baseline=scores(pool.baseline),
        sealed=sealed,
        track=pool.track.name,
        metrics=everything,
        incumbent=scores(step.incumbent),
        parent=scores(parent) if parent is not None and parent in pool.entries else None,
    )
