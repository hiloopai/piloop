"""The example plugin ``examples/plugins/inference``: a fact source, a verdict rule and a section for inference work."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from inline_snapshot import snapshot
from piloop_inference.gpu_hours import GPU_HOURS, GPU_USD
from piloop_inference.min_speedup import MinSpeedup
from sqlalchemy import update

from piloop.campaign.stop import check, gather
from piloop.capabilities import (
    SECTIONS,
    Case,
    Criterion,
    Direction,
    Escalate,
    Go,
    Hold,
    Judgement,
    Outcome,
    Rendering,
    Side,
)
from piloop.db.models import Campaign
from piloop.db.session import Database
from piloop.plugins import host
from piloop.plugins.campaign_type.kit.evaluation.tables import Measurement
from piloop.plugins.section.contract import markdown
from tests.campaign.test_stop import add_run
from tests.conftest import cas_of, example
from tests.daemon.test_scheduler import set_config
from tests.reports.test_overview import NOW


@pytest.fixture
def inference(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The example installed, so that the configuration selects its providers."""
    example(tmp_path / 'site', 'inference')
    monkeypatch.syspath_prepend(tmp_path / 'site')
    host.close()
    yield
    host.close()


def against_100(candidate: float) -> Case:
    throughput = Criterion('tokens_per_s', Direction.HIGHER)
    mine, bar = Side('candidate#2', {'tokens_per_s': candidate}, {}), Side('candidate#1', {'tokens_per_s': 100}, {})
    return Case(throughput, (), mine, bar)


def test_min_speedup_keeps_only_a_gain_of_at_least_its_fraction_of_the_bar() -> None:
    rule = MinSpeedup(min_speedup=0.05)
    assert rule.decide(against_100(110)).verdict is Outcome.KEEP
    assert rule.decide(against_100(103)) == Judgement(
        Outcome.INTERESTING,
        'against candidate#1: tokens_per_s 103 vs 100 (+3; aggregate: no per-unit results); 3.0% is below min_speedup 5%',
    )
    assert rule.decide(against_100(90)).verdict is Outcome.DISCARD, 'what bootstrap does not keep stays'


@pytest.mark.usefixtures('inference')
def test_gpu_hours_prices_the_wall_time_of_every_evaluation_and_a_hold_at_holds_the_campaign_at_its_budget(
    db: Database, campaign: Campaign
) -> None:
    sources = {'use': ['campaign', 'optimization', 'gpu_hours'], 'gpu_hours': {'gpus': 2, 'usd_per_gpu_hour': 3}}
    add_run(db, campaign)
    with db.session() as s:
        s.execute(update(Measurement).values(seconds=1800))
        s.commit()
    for budget, answer in [(10, Go), (6, Escalate)]:
        set_config(db, campaign, plugins={'fact_source': sources, 'stop_rule': {'hold_at': {'gpu_usd': budget}}})
        with db.session() as s:
            p = s.get_one(Campaign, campaign.id)
            facts = gather(s, p, p.config, [GPU_HOURS, GPU_USD], NOW)
            assert (facts.get(GPU_HOURS), facts.get(GPU_USD)) == (2, 6), 'two evaluations of 30 min, 2 GPUs, 3 USD'
            found = check(s, p, p.config)
        assert isinstance(found, answer), budget
    assert isinstance(found, Escalate) and isinstance(found.then, Hold)


@pytest.mark.usefixtures('inference')
def test_latency_shows_the_latest_candidates_full_benchmark_and_nothing_before_one(
    db: Database, campaign: Campaign
) -> None:
    latency = [SECTIONS.validated({'provider': 'latency', 'metrics': ['p95_ms', 'match'], 'rows': 2})]
    with db.session() as s:
        s.execute(update(Measurement).values(metrics={'score': 1}, rung='smoke'))
        s.commit()
        p = s.get_one(Campaign, campaign.id)
        assert markdown(latency, Rendering(s, NOW, p, Path('/no/workspace'), cas_of(s))) == '', 'a rung is not one'
    for _ in range(3):
        add_run(db, campaign)
    with db.session() as s:
        evaluator = s.get_one(Campaign, campaign.id).evaluator_id or 0
        s.add_all(Measurement(candidate_id=4, evaluator_id=evaluator, metrics={'p95_ms': ms}) for ms in (10, 20))
        s.add(Measurement(candidate_id=3, evaluator_id=evaluator, metrics={'p95_ms': 50, 'match': 0.99}))
        s.commit()
        p = s.get_one(Campaign, campaign.id)
        text = markdown(latency, Rendering(s, NOW, p, Path('/no/workspace'), cas_of(s)))
    assert text == snapshot("""\
## Latency

| candidate | verdict | p95_ms | match |
|---|---|---|---|
| #4 | discard | 15 | — |
| #3 | discard | 50 | 0.99 |\
""")
