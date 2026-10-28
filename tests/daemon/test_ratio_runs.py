"""A ratio guardrail end to end: the evaluate command reports its parts, and piloop computes and judges the ratio."""

from pathlib import Path

from piloop.db.models import Campaign
from piloop.db.session import Database
from piloop.plugins.campaign_type.kit.candidates.ids import Outcome
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.evaluation.measure import metrics
from piloop.plugins.metric import number
from tests.conftest import ScriptRuntime, baseline
from tests.daemon.test_evaluation_queue import run_candidates
from tests.daemon.test_run_controller import controller, execute, make_run, set_config

TASKS = 40
EVALUATE = f"""
import json, os
lines = len(open('solve.txt').read().splitlines())
solved = {{f't{{i}}': float(i < 10 * lines) for i in range({TASKS})}}
cost = {{f't{{i}}': 0.5 * lines * lines for i in range({TASKS})}}
result = {{'metrics': {{'solved': sum(solved.values()), 'cost_usd': sum(cost.values())}},
          'units': {{'solved': solved, 'cost_usd': cost}}}}
open(os.environ['PILOOP_RESULT_FILE'], 'a').write(json.dumps(result) + '\\n')
"""
"""Each line of solve.txt solves 10 more of 40 tasks, and costs more per task than the one before."""
METRICS = [
    {'name': 'solved', 'direction': 'higher', 'role': 'primary'},
    {'name': 'cost_usd', 'direction': 'lower'},
    {
        'name': 'cost_per_solved',
        'direction': 'lower',
        'role': 'guardrail',
        'fraction': 0.5,
        'ratio': {'numerator': 'cost_usd', 'denominator': 'solved'},
    },
]


def test_a_ratio_guardrail_is_computed_from_its_parts_and_holds_back_a_costlier_keep(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    set_config(db, campaign, evaluate=['python3', '-c', EVALUATE], metrics=METRICS)
    ex = controller(db, runtime, tmp_path)
    ex.remeasure(baseline(db, campaign.id).id)
    run_id = make_run(db, campaign, track='solved')
    execute(ex, db, run_id)
    [c] = run_candidates(db, run_id)
    with db.session() as s:
        candidate = s.get_one(Candidate, c.id)
        evaluator = s.get_one(Campaign, campaign.id).evaluator_id
        assert metrics(candidate, evaluator) == {'solved': 20.0, 'cost_usd': 80.0, 'cost_per_solved': 4.0}
        assert 'cost_per_solved' not in candidate.measurements[-1].metrics, 'piloop computes it; nothing stores it'
        assert candidate.verdict is Outcome.INTERESTING
        assert candidate.verdict_reason is not None
        assert 'cost_per_solved 4 vs 2 (+2; 95% worst case' in candidate.verdict_reason
        assert 'beyond its fraction 0.5' in candidate.verdict_reason


def test_a_ratio_primary_with_repeats_has_one_value_in_its_verdict_and_in_status(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    """Each tree's samples are 1, 2 and 6 seconds per line of solve.txt: the mean of the pair ratios is not the ratio
    of the means."""
    noisy = f"""
import json, os, pathlib
lines = len(open('solve.txt').read().splitlines())
count = pathlib.Path({str(tmp_path)!r}) / f'count{{lines}}'
k = int(count.read_text()) if count.exists() else 0
count.write_text(str(k + 1))
result = {{'metrics': {{'tokens': 512.0, 'seconds': [1.0, 2.0, 6.0][k % 3] * lines}}}}
open(os.environ['PILOOP_RESULT_FILE'], 'a').write(json.dumps(result) + '\\n')
"""
    throughput = {
        'name': 'tokens_per_s',
        'direction': 'higher',
        'role': 'primary',
        'repeats': 3,
        'ratio': {'numerator': 'tokens', 'denominator': 'seconds'},
    }
    parts = [{'name': 'tokens', 'direction': 'higher'}, {'name': 'seconds', 'direction': 'lower'}]
    set_config(db, campaign, evaluate=['python3', '-c', noisy], metrics=[throughput, *parts])
    ex = controller(db, runtime, tmp_path)
    ex.remeasure(baseline(db, campaign.id).id)
    run_id = make_run(db, campaign, track='tokens_per_s')
    execute(ex, db, run_id)
    [c] = run_candidates(db, run_id)
    with db.session() as s:
        candidate = s.get_one(Candidate, c.id)
        shown = metrics(candidate, s.get_one(Campaign, campaign.id).evaluator_id)['tokens_per_s']
        assert len(candidate.measurements) == 3
        assert candidate.verdict_reason is not None
        assert f'tokens_per_s {number(shown)} vs' in candidate.verdict_reason, candidate.verdict_reason
