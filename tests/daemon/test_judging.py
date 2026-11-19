"""The daemon asks the best rule each case that a fold waits for, once, and stores the answer (``judging.judge``)."""

import sys
import threading
from pathlib import Path

import pytest
from pytest_mock import MockerFixture
from sqlalchemy import select

from piloop.db.models import Campaign, Comment
from piloop.db.session import Database
from piloop.plugins.campaign_type.kit.candidates.ids import CandidateId, Origin
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.evaluation.ids import EvaluatorId, RungName
from piloop.plugins.campaign_type.kit.evaluation.measure import sealed_evaluator, stored_units
from piloop.plugins.campaign_type.kit.evaluation.tables import Measurement
from piloop.plugins.campaign_type.kit.pool import judging
from piloop.plugins.campaign_type.kit.pool.best_rule.baseline import Baseline
from piloop.plugins.campaign_type.kit.pool.best_rule.contract import CaseSample
from piloop.plugins.campaign_type.kit.pool.judging import case_of, judge
from piloop.plugins.campaign_type.kit.pool.pool import Step
from piloop.plugins.campaign_type.kit.pool.tables import Answer, Judgement
from piloop.plugins.escalation.contract import ESCALATION
from tests.conftest import ScriptRuntime, baseline, disk, home_cas, wait_until
from tests.daemon.test_pool import configure, controller, pool

SCORE = {'name': 'score', 'direction': 'higher', 'role': 'primary'}


def program(tmp_path: Path, answer: str = '{"answer": "keep"}', *, code: int = 0, gate: bool = False) -> list[str]:
    """A best rule program that counts its calls in ``calls``, waits for the file ``go`` with ``gate``, answers
    ``answer``, and exits with ``code``."""
    script = tmp_path / 'rule.py'
    go = tmp_path / 'go'
    script.write_text(
        'import os, sys, time\n'
        f'open({str(tmp_path / "calls")!r}, "a").write("x")\n'
        f'while {gate} and not os.path.exists({str(go)!r}):\n'
        '    time.sleep(0.05)\n'
        'with open(os.environ["PILOOP_RESULT_FILE"], "a") as out:\n'
        f'    out.write({answer + chr(10)!r})\n'
        f'sys.exit({code})\n'
    )
    return [sys.executable, '-I', str(script)]


def calls(tmp_path: Path) -> int:
    return len(path.read_text()) if (path := tmp_path / 'calls').exists() else 0


def command(db: Database, campaign: Campaign, argv: list[str], **keys: object) -> None:
    configure(db, campaign, plugins={'best_rule': {'provider': 'command', 'command': argv, **keys}})


def scored(db: Database, campaign: Campaign, *scores: float, parent: CandidateId | None = None) -> CandidateId:
    """A new candidate, a child of ``parent`` or of the baseline, with one evaluation of each of ``scores``."""
    parent = parent or baseline(db, campaign.id).id
    with db.session() as s:
        p = s.get_one(Campaign, campaign.id)
        n = len(s.scalars(select(Candidate.id).where(Candidate.campaign_id == p.id)).all())
        c = Candidate(
            campaign_id=p.id,
            parent_id=parent,
            tree_sha=f't{n}',
            revision=f'r{n}',
            origin=Origin.RUN,
        )
        s.add(c)
        s.flush()
        assert p.evaluator_id is not None
        s.add_all(Measurement(candidate_id=c.id, evaluator_id=p.evaluator_id, metrics={'score': v}) for v in scores)
        s.commit()
        return c.id


def answers(db: Database) -> list[tuple[CandidateId, CandidateId, Answer, str | None]]:
    with db.session() as s:
        rows = s.scalars(select(Judgement).order_by(Judgement.id)).all()
        return [(j.candidate_id, j.incumbent_id, j.answer, j.reason) for j in rows]


def test_the_rule_answers_each_case_once_and_the_best_follows_its_answers(
    db: Database, campaign: Campaign, tmp_path: Path
) -> None:
    command(db, campaign, program(tmp_path))
    base = baseline(db, campaign.id).id
    better = scored(db, campaign, 5)
    assert pool(db, campaign).best() == base, 'nobody asked the rule yet'
    judge(db, home_cas(db), campaign.id)
    assert (pool(db, campaign).best(), answers(db), calls(tmp_path)) == (
        better,
        [(better, base, Answer.KEEP, None)],
        1,
    )
    judge(db, home_cas(db), campaign.id)
    assert calls(tmp_path) == 1, 'the same case is not asked again'


def test_two_judges_of_one_case_store_one_answer(db: Database, campaign: Campaign, tmp_path: Path) -> None:
    command(db, campaign, program(tmp_path, gate=True))
    scored(db, campaign, 5)
    both = [threading.Thread(target=judge, args=(db, home_cas(db), campaign.id)) for _ in range(2)]
    for t in both:
        t.start()
    wait_until(lambda: calls(tmp_path) == 2, 'both judges to ask the rule')
    (tmp_path / 'go').touch()
    for t in both:
        t.join()
    assert calls(tmp_path) == 2, 'both asked: nothing serialized these two'
    assert len(answers(db)) == 1, 'the first answer stays'


def test_a_remeasure_a_margin_or_the_rule_s_table_makes_new_cases_and_the_fold_is_judged_again_from_the_baseline(
    db: Database, campaign: Campaign, tmp_path: Path
) -> None:
    argv = program(tmp_path)

    def guarded(margin: float, **keys: object) -> None:
        size = {'name': 'size', 'direction': 'lower', 'role': 'guardrail', 'margin': margin}
        rule = {'provider': 'command', 'command': argv, **keys}
        configure(db, campaign, metrics=[SCORE, size], plugins={'best_rule': rule})

    guarded(100)
    base = baseline(db, campaign.id).id
    first = scored(db, campaign, 5)
    second = scored(db, campaign, 7, parent=first)
    judge(db, home_cas(db), campaign.id)
    assert [a[:2] for a in answers(db)] == [(first, base), (second, first)]
    with db.session() as s:
        p = s.get_one(Campaign, campaign.id)
        assert p.evaluator_id is not None
        s.add(Measurement(candidate_id=first, evaluator_id=p.evaluator_id, metrics={'score': 5}))
        s.commit()
    judge(db, home_cas(db), campaign.id)
    assert [a[:2] for a in answers(db)][2:] == [(first, base), (second, first)], 'a remeasure changes both cases'
    guarded(50)
    judge(db, home_cas(db), campaign.id)
    assert len(answers(db)) == 6, 'a margin is part of each case'
    guarded(50, version='2')
    judge(db, home_cas(db), campaign.id)
    assert ([a[:2] for a in answers(db)][6:], calls(tmp_path)) == ([(first, base), (second, first)], 8)


def test_a_rule_that_fails_is_stored_unanswered_escalated_once_and_the_fold_waits_until_its_table_changes(
    db: Database, campaign: Campaign, tmp_path: Path
) -> None:
    command(db, campaign, program(tmp_path, code=3))
    base = baseline(db, campaign.id).id
    first = scored(db, campaign, 5)
    later = scored(db, campaign, 9)
    judge(db, home_cas(db), campaign.id)
    found = pool(db, campaign)
    assert found.best() == base, 'fail closed: a broken rule never makes a best'
    assert answers(db) == [(first, base, Answer.UNANSWERED, 'exit 3: ')]
    assert found.refusal(first) == 'the best rule failed: exit 3: '
    assert found.refusal(later) is None, 'it waits behind the first'
    judge(db, home_cas(db), campaign.id)
    assert calls(tmp_path) == 1, 'no retry loop: the same case is not asked again'
    with db.session() as s:
        escalations = s.scalars(select(Comment).where(Comment.kind == ESCALATION)).all()
        assert len(escalations) == 1 and 'did not answer' in escalations[0].body
    command(db, campaign, program(tmp_path), version='2')
    judge(db, home_cas(db), campaign.id)
    assert pool(db, campaign).best() == later


def test_a_guardrail_breach_never_reaches_the_program(db: Database, campaign: Campaign, tmp_path: Path) -> None:
    metrics = [
        {'name': 'score', 'direction': 'higher', 'role': 'primary'},
        {'name': 'size', 'direction': 'lower', 'role': 'guardrail', 'margin': 1},
    ]
    configure(
        db, campaign, metrics=metrics, plugins={'best_rule': {'provider': 'command', 'command': program(tmp_path)}}
    )
    base = baseline(db, campaign.id).id
    with db.session() as s:
        p = s.get_one(Campaign, campaign.id)
        assert p.evaluator_id is not None
        c = Candidate(campaign_id=p.id, parent_id=base, tree_sha='big', revision='big', origin=Origin.RUN)
        s.add(c)
        s.flush()
        s.add(Measurement(candidate_id=c.id, evaluator_id=p.evaluator_id, metrics={'score': 9, 'size': 50}))
        s.commit()
    judge(db, home_cas(db), campaign.id)
    assert (calls(tmp_path), answers(db)[0][2:]) == (
        0,
        (Answer.REFUSE, 'guardrails: size 50 vs the baseline 5 beyond its margin 1'),
    )


def test_the_case_has_each_side_with_every_sample_its_rung_its_units_and_its_sealed_samples(
    db: Database, campaign: Campaign, tmp_path: Path
) -> None:
    configure(db, campaign, evaluator={'noise_runs': 1, 'sealed': ['true']})
    cas = home_cas(db)
    base = baseline(db, campaign.id).id
    parent = scored(db, campaign, 3)
    units = {'score': {'u1': 2.0, 'u2': 3.0}}
    with db.session() as s:
        p = s.get_one(Campaign, campaign.id)
        hidden = sealed_evaluator(s, p, p.config)
        assert hidden is not None and p.evaluator_id is not None
        visible, sealed = p.evaluator_id, EvaluatorId(hidden.id)
        c = Candidate(campaign_id=p.id, parent_id=parent, tree_sha='c', revision='c', origin=Origin.RUN)
        s.add(c)
        s.flush()
        rows = [
            Measurement(candidate_id=base, evaluator_id=sealed, metrics={'score': 1}),
            Measurement(candidate_id=parent, evaluator_id=sealed, metrics={'score': 2}),
            Measurement(candidate_id=c.id, evaluator_id=visible, metrics={'score': 4}, rung=RungName('screen')),
            Measurement(candidate_id=c.id, evaluator_id=visible, metrics={'score': 5}, units=stored_units(cas, units)),
            Measurement(candidate_id=c.id, evaluator_id=visible, metrics={}, error='crashed'),
            Measurement(candidate_id=c.id, evaluator_id=sealed, metrics={'score': 6}, units=stored_units(cas, units)),
        ]
        s.add_all(rows)
        s.commit()
        made, ids = c.id, [m.id for m in rows]
    judge(db, cas, campaign.id)
    assert [a[:3] for a in answers(db)] == [(parent, base, Answer.KEEP), (made, parent, Answer.KEEP)]
    on = pool(db, campaign)
    with db.session() as s:
        case = case_of(s, cas, on, Step(made, parent, on.key(made, parent)))
    assert (case.candidate.id, case.candidate.parent, case.incumbent.id, case.baseline.id) == (
        made,
        parent,
        parent,
        base,
    )
    assert case.parent is not None and case.parent.id == parent and case.parent.sealed_samples[0].id == ids[1]
    assert case.candidate.samples == (
        CaseSample(id=ids[2], rung='screen', metrics={'score': 4.0}, units={}),
        CaseSample(id=ids[3], rung=None, metrics={'score': 5.0}, units=units),
    ), 'every rung, and no crash'
    assert case.candidate.sealed_samples == (CaseSample(id=ids[5], rung=None, metrics={'score': 6.0}, units=units),)
    assert (case.track, [m.name for m in case.metrics]) == ('score', ['score', 'size'])
    with db.session() as s:
        s.get_one(Measurement, ids[3]).units = disk(tmp_path / 'elsewhere').put(b'{}')  # a blob this CAS lacks
        s.commit()
    with db.session() as s, pytest.raises(LookupError, match='are not in the CAS'):
        case_of(s, cas, on, Step(made, parent, on.key(made, parent)))


def test_a_rule_that_raises_or_a_case_that_cannot_be_read_is_no_answer(
    db: Database, campaign: Campaign, tmp_path: Path, mocker: MockerFixture
) -> None:
    first = scored(db, campaign, 5)
    mocker.patch.object(Baseline, 'judge', side_effect=RuntimeError('boom'))
    judge(db, home_cas(db), campaign.id)
    assert answers(db)[-1][2:] == (Answer.UNANSWERED, 'RuntimeError: boom')
    with db.session() as s:
        p = s.get_one(Campaign, campaign.id)
        assert p.evaluator_id is not None
        lost = disk(tmp_path / 'elsewhere').put(b'{}')  # a blob that this CAS lacks
        s.add(Measurement(candidate_id=first, evaluator_id=p.evaluator_id, metrics={'score': 5}, units=lost))
        s.commit()
    judge(db, home_cas(db), campaign.id)
    found = answers(db)[-1]
    assert found[2] is Answer.UNANSWERED and (found[3] or '').startswith('cannot read the case: ')


def test_the_daemon_s_judge_never_raises_and_its_span_has_the_error(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path, mocker: MockerFixture
) -> None:
    failing = mocker.patch.object(judging, 'judge', side_effect=OSError('disk gone'))
    controller(db, runtime, tmp_path).judge(campaign.id)
    assert failing.call_count == 1, 'it ran, failed, and the pass goes on'


def test_a_measurement_corrected_by_hand_is_a_new_case(db: Database, campaign: Campaign, tmp_path: Path) -> None:
    """``piloop sql`` may correct a row: the key names what the measurements say, not only their ids."""
    command(db, campaign, program(tmp_path))
    first = scored(db, campaign, 5)
    judge(db, home_cas(db), campaign.id)
    with db.session() as s:
        s.scalars(select(Measurement).where(Measurement.candidate_id == first)).one().metrics = {'score': 6.0}
        s.commit()
    judge(db, home_cas(db), campaign.id)
    assert calls(tmp_path) == 2
