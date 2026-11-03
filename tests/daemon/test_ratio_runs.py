"""A ratio end to end: the evaluate command reports its parts, and piloop computes the ratio and scores on it."""

from pathlib import Path

from inline_snapshot import snapshot

from piloop.db.models import Campaign
from piloop.db.session import Database
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.evaluation.measure import metrics
from piloop.plugins.campaign_type.kit.pool.pool import best_of
from piloop.plugins.metric import number
from tests.conftest import ScriptRuntime, baseline
from tests.daemon.test_evaluation_queue import run_candidates
from tests.daemon.test_run_controller import controller, execute, make_run, set_config, told_of

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


def test_a_ratio_guardrail_is_computed_from_its_parts_and_keeps_a_costlier_candidate_from_the_best(
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
        assert best_of(s, s.get_one(Campaign, campaign.id)) != candidate
        assert told_of(s, c.id) == snapshot(
            'solved 20 over 1 eval: it may not be best: guardrails: cost_per_solved 4 vs the baseline 2 beyond its fraction 0.5; vs parent +10, vs best candidate#1 +10'
        )


def test_a_ratio_primary_over_several_evaluations_has_one_value_in_its_score_and_in_status(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    """Each tree's samples are 1, 2 and 6 seconds per line of solve.txt: the mean of the ratios is not the ratio of the
    means. The candidate's first sample beats the baseline, so it is confirmed over ``noise_runs`` evaluations."""
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
        'ratio': {'numerator': 'tokens', 'denominator': 'seconds'},
    }
    parts = [{'name': 'tokens', 'direction': 'higher'}, {'name': 'seconds', 'direction': 'lower'}]
    evaluator = {'scoring': 'run_end', 'noise_runs': 3}
    set_config(db, campaign, evaluate=['python3', '-c', noisy], metrics=[throughput, *parts], evaluator=evaluator)
    ex = controller(db, runtime, tmp_path)
    ex.remeasure(baseline(db, campaign.id).id)
    run_id = make_run(db, campaign, track='tokens_per_s')
    execute(ex, db, run_id)
    [c] = run_candidates(db, run_id)
    with db.session() as s:
        candidate = s.get_one(Candidate, c.id)
        shown = metrics(candidate, s.get_one(Campaign, campaign.id).evaluator_id)['tokens_per_s']
        assert len(candidate.measurements) == 3
        told = told_of(s, c.id)
        assert told is not None and told.startswith(f'tokens_per_s {number(shown)} over 3 evals'), told
