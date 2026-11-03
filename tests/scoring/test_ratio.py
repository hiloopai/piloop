"""Ratio metrics: piloop computes ``numerator / denominator`` from the means of what the evaluate command reports."""

import re
from pathlib import Path

import pytest

from piloop.config import ConfigError, load_campaign_config
from piloop.db.models import Campaign
from piloop.db.session import Database
from piloop.plugins.campaign_type.kit.candidates.ids import Origin
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.evaluation.config import Ratio
from piloop.plugins.campaign_type.kit.evaluation.measure import current_evaluator, derive
from piloop.plugins.campaign_type.kit.evaluation.tables import Measurement
from piloop.plugins.campaign_type.kit.pool.pool import Pool
from tests.conftest import baseline

SOLVED = {'name': 'solved', 'direction': 'higher', 'role': 'primary'}
COST = {'name': 'cost_usd', 'direction': 'lower'}
PER_SOLVED = {
    'name': 'cost_per_solved',
    'direction': 'lower',
    'role': 'guardrail',
    'fraction': 0.05,
    'ratio': {'numerator': 'cost_usd', 'denominator': 'solved'},
}


def test_a_ratio_is_computed_not_reported_and_its_parts_are_required(campaign_path: Path) -> None:
    cfg = load_campaign_config(campaign_path, metrics=[SOLVED, COST, PER_SOLVED])
    assert (cfg.reported, cfg.required) == (['solved', 'cost_usd'], ['solved', 'cost_usd'])
    assert cfg.evaluator_definition['judging'] == [
        {'name': 'cost_per_solved', 'direction': 'lower', 'ratio': {'numerator': 'cost_usd', 'denominator': 'solved'}},
        {'name': 'solved', 'direction': 'higher'},
    ]
    other = {**PER_SOLVED, 'ratio': {'numerator': 'cost_usd', 'denominator': 'tasks'}}
    tasks = {'name': 'tasks', 'direction': 'higher'}
    assert load_campaign_config(campaign_path, metrics=[SOLVED, COST, tasks, other]).evaluator_key != cfg.evaluator_key


@pytest.mark.parametrize(
    ('metrics', 'error'),
    [
        ([SOLVED, PER_SOLVED], "'cost_per_solved': ratio of ['cost_usd'], which are not metrics that evaluate reports"),
        (
            [SOLVED, COST, {**PER_SOLVED, 'ratio': {'numerator': 'cost_usd', 'denominator': 'cost_usd'}}],
            "'cost_per_solved': ratio of 'cost_usd' by itself",
        ),
        (
            [SOLVED, COST, {**PER_SOLVED, 'role': 'watch', 'fraction': None}],
            "'cost_per_solved': ratio applies to primaries and guardrails only",
        ),
    ],
)
def test_a_ratio_divides_two_reported_metrics(
    campaign_path: Path, metrics: list[dict[str, object]], error: str
) -> None:
    with pytest.raises(ConfigError, match=re.escape(error)):
        load_campaign_config(campaign_path, metrics=metrics)


def test_no_stop_of_the_ladder_reads_a_ratio(campaign_path: Path) -> None:
    ladder = [{'name': 'screen', 'stop': {'cost_per_solved': {'margin': 0.1}}}]
    with pytest.raises(ConfigError, match=re.escape("ladder.screen.stop: ['cost_per_solved'] are ratios")):
        load_campaign_config(campaign_path, metrics=[SOLVED, COST, PER_SOLVED], ladder=ladder)


def test_a_ratio_is_the_ratio_of_the_means_and_needs_a_denominator() -> None:
    found = {'cost_per_solved': Ratio(numerator='cost_usd', denominator='solved')}
    assert derive({'cost_usd': 3.0, 'solved': 6.0}, found) == {'cost_usd': 3.0, 'solved': 6.0, 'cost_per_solved': 0.5}
    assert derive({'cost_usd': 3.0, 'solved': 0.0}, found) == {'cost_usd': 3.0, 'solved': 0.0}
    assert derive({'cost_usd': 3.0}, found) == {'cost_usd': 3.0}


def test_the_pool_reads_a_ratio_guardrail_as_the_ratio_of_the_means_against_the_baseline_s(
    db: Database, campaign: Campaign
) -> None:
    """Two evaluations of 2/4 and 7/8 cost 4.5 per 6 solved: 0.75, not the mean of the ratios, 0.6875. The baseline's
    ratio is 0.5, and 5% of it allows 0.025."""
    first = baseline(db, campaign.id)
    with db.session() as s:
        p = s.get_one(Campaign, campaign.id)
        p.config = load_campaign_config(Path(campaign.path), metrics=[SOLVED, COST, PER_SOLVED])
        evaluator = current_evaluator(s, p, p.config).id
        pricier = Candidate(campaign_id=p.id, parent_id=first.id, tree_sha='t', revision='r', origin=Origin.RUN)
        s.add(pricier)
        s.flush()
        runs = {first.id: [(4.0, 2.0)], pricier.id: [(4.0, 2.0), (8.0, 7.0)]}
        s.add_all(
            Measurement(candidate_id=c, evaluator_id=evaluator, metrics={'solved': solved, 'cost_usd': cost})
            for c, values in runs.items()
            for solved, cost in values
        )
        s.flush()
        pool = Pool.of(s, p, p.config.all_tracks[0])
        assert pool.entries[pricier.id].means == {'solved': 6.0, 'cost_usd': 4.5, 'cost_per_solved': 0.75}
        assert (
            pool.refusal(pricier.id) == 'guardrails: cost_per_solved 0.75 vs the baseline 0.5 beyond its fraction 0.05'
        )
        assert pool.best() == first.id
