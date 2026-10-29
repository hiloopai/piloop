"""What a campaign spent: the ``leases`` and ``models`` fact sources, and the ``spend`` source that sums them.

``leases`` prices the seconds that the campaign's jobs held a worker, each worker's time split among the jobs that it
held then, at its pool's ``usd_per_hour``. ``models`` prices the tokens of the agents that ended. ``spend`` sums the
facts that it names, and serves each part at ``GET /v1/campaigns/{campaign}/spend``. An unknown part, such as a pool or
a model without a price, makes the sum unknown: never a part of it shown as all of it.
"""

from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import Path

import httpx2 as httpx
import pytest
from covalence import Cid
from sqlalchemy.orm import Session

from piloop.actor import System
from piloop.campaign.stop import gather
from piloop.config import CampaignConfig
from piloop.db.events import emit
from piloop.db.models import AgentSession, AgentSessionState, Campaign, Lease, LeaseKind, LeaseState, utcnow
from piloop.db.session import Database
from piloop.ids import AgentSessionId, TargetRef
from piloop.plugins.campaign_type.kit.candidates.ids import CANDIDATE, CandidateId
from piloop.plugins.executor.contract import EXECUTORS, Executor, ExecutorConfig, Host
from piloop.plugins.executor.kit.capacity import Pool
from piloop.plugins.executor.kit.leases import LeaseQueued
from piloop.plugins.executor.kit.tables import LeaseWorker, WorkerInstance
from piloop.plugins.fact_source.command import Command
from piloop.plugins.fact_source.facts import COMPUTE_USD, MODELS_USD, SPEND, since_keep
from piloop.plugins.fact_source.leases import LeaseFacts, compute_usd
from piloop.plugins.fact_source.models import ModelFacts
from piloop.plugins.harness.contract import ModelId, Usage
from piloop.plugins.price_list.genai_prices import GenaiPrices
from piloop.settings import HOME_PLUGINS, Settings, settings
from tests.campaign.test_stop import add_run
from tests.conftest import bare_config, baseline, reconfigured, serving_api, store_config

PRICES = {'opus': {'input': 15.0, 'output': 75.0, 'cache_read': 1.5, 'cache_write': 18.75}}
SPENDING = {
    'fact_source': {'use': ['campaign', 'optimization', 'leases', 'models', 'spend'], 'models': {'prices': PRICES}}
}
PRICED = {'agents': Pool(usd_per_hour=3.6), 'evals': Pool(usd_per_hour=7.2)}
"""0.001 and 0.002 USD a second."""


def configure(db: Database, campaign: Campaign, plugins: Mapping[str, object]) -> CampaignConfig:
    """Give ``campaign`` the tables of ``plugins`` over its own, and return its config."""
    with db.session() as s:
        found = s.get_one(Campaign, campaign.id)
        found.config = reconfigured(found.config, plugins=plugins)
        s.commit()
        return found.config


def other(db: Database, tmp_path: Path) -> Campaign:
    """A second campaign of the home, whose jobs share the workers."""
    with db.session() as s:
        found = Campaign(name='other', path=str(tmp_path / 'other'), config=bare_config('other'))
        s.add(found)
        s.commit()
        return found


def agent(
    s: Session, campaign: Campaign, model: str | None = 'opus', usage: Usage | None = None, harness: str | None = None
) -> AgentSessionId:
    """An agent of ``campaign`` that ran ``model`` on ``harness``. With ``usage``, it ended."""
    row = AgentSession(kind='implementer', campaign_id=campaign.id, harness=harness, model=model, usage=usage)
    row.state = AgentSessionState.RUNNING if usage is None else AgentSessionState.EXITED
    s.add(row)
    s.flush()
    return row.id


def hold(
    s: Session,
    worker: str | None,
    held: tuple[float, float | None],
    now: datetime,
    *,
    agent: AgentSessionId | None = None,
    candidate: CandidateId | None = None,
) -> None:
    """A lease of ``worker`` on the job of ``agent`` or of an evaluation of ``candidate``.

    ``held`` is the seconds after ``now - 1000`` at which the worker took the job and let go of it (None: it holds it).
    """
    zero = now - timedelta(seconds=1000)
    start, end = held
    lease = Lease(
        kind=LeaseKind.AGENT if agent is not None else LeaseKind.EVALUATION,
        agent_session_id=agent,
        evaluation_id=None if agent is not None else 1,
        state=LeaseState.QUEUED if worker is None else LeaseState.RUNNING if end is None else LeaseState.REPORTED,
        worker=worker,
        job_cid=Cid.of(b'{}'),
        acquired_at=None if worker is None else zero + timedelta(seconds=start),
        ended_at=None if end is None else zero + timedelta(seconds=end),
    )
    s.add(lease)
    s.flush()
    if candidate is not None:
        on = TargetRef(CANDIDATE, candidate)
        emit(s, LeaseQueued(lease=lease.id, lease_kind=LeaseKind.EVALUATION), on, System.DAEMON)


def test_compute_is_each_worker_s_time_split_among_the_jobs_that_it_held_then_at_its_pool_s_price(
    db: Database, campaign: Campaign, tmp_path: Path
) -> None:
    """A worker's second is split evenly among its jobs then. Its idle time is no campaign's.

    Each worker is in the pool of its instance, which outlives it, else of its row.
    """
    now, them = utcnow(), other(db, tmp_path)
    with db.session() as s:
        mine, theirs, live = agent(s, campaign), agent(s, them), agent(s, campaign)
        hold(s, 'agents-1', (0, 100), now, agent=mine)
        hold(s, 'agents-1', (50, 150), now, agent=theirs)  # half of 50..100 is ours, 100..150 is theirs
        hold(s, 'agents-1', (200, None), now, agent=live)  # idle 150..200, then ours until now: 800 s
        hold(s, None, (0, None), now, agent=mine)  # queued: no worker held it
        hold(s, 'laptop', (0, 10), now, candidate=baseline(db, campaign.id).id)
        s.add(WorkerInstance(name='agents-1', pool='agents'))
        s.add(LeaseWorker(id='laptop', pool='evals', version='0', seen_at=now))
        s.commit()
        found = s.get_one(Campaign, campaign.id)
        assert compute_usd(s, found, PRICED, now) == pytest.approx((50 + 25 + 800) * 0.001 + 10 * 0.002)
        theirs_usd = (25 + 50) * 0.001
        assert compute_usd(s, s.get_one(Campaign, them.id), PRICED, now) == pytest.approx(theirs_usd)
        assert compute_usd(s, found, {'agents': Pool(usd_per_hour=3.6), 'evals': Pool()}, now) is None, (
            'a pool without a price makes the compute unknown, never 0'
        )
        assert compute_usd(s, found, {'agents': Pool(usd_per_hour=3.6)}, now) is None, 'and a pool that is gone'


def test_leases_reads_the_prices_of_the_home_s_pools(
    db: Database, campaign: Campaign, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``[plugins.executor.pools.<pool>] usd_per_hour`` of ``config.toml``. An executor without pools prices nothing."""
    now = utcnow()
    with db.session() as s:
        hold(s, 'serve', (0, 100), now, agent=agent(s, campaign))
        s.add(LeaseWorker(id='serve', pool='serve', version='0', seen_at=now))
        s.commit()
        found = s.get_one(Campaign, campaign.id)
        home = HOME_PLUGINS.validate_python({'executor': {'pools': {'serve': {'usd_per_hour': 36}}}})
        monkeypatch.setitem(vars(settings), 'providers', home)
        assert LeaseFacts().gather(s, found, frozenset({COMPUTE_USD}), now) == {COMPUTE_USD: pytest.approx(1.0)}
        monkeypatch.setattr(EXECUTORS, 'selected', lambda _plugins: Elsewhere())
        assert LeaseFacts().gather(s, found, frozenset({COMPUTE_USD}), now) == {COMPUTE_USD: None}


class Elsewhere(ExecutorConfig):
    """An executor of a package, which has no pools."""

    def open(self, host: Host) -> Executor[ExecutorConfig]:
        raise NotImplementedError


def test_models_prices_the_tokens_of_the_agents_that_ended_and_an_unpriced_model_is_unknown(
    db: Database, campaign: Campaign, tmp_path: Path
) -> None:
    """``prices`` first, then the price list, by the vendor's id of the model that its harness names.

    A running agent has no tokens yet. A model that neither prices is unknown, and so is the total.
    """
    them = other(db, tmp_path)
    tokens = Usage(input=1_000_000, output=100_000, cache_read=2_000_000, cache_write=40_000, total=0, turns=10)
    with db.session() as s:
        agent(s, campaign, harness='script', usage=tokens)  # opus: its price in prices
        agent(s, campaign, harness='claude', model='claude-sonnet-4-5', usage=tokens)
        agent(s, campaign, harness='gone', model='claude-haiku-4-5', usage=tokens)  # a harness no longer configured
        agent(s, campaign, model='haiku')  # running: its tokens are not known yet
        agent(s, them, model='haiku', usage=tokens)
        s.commit()
        found = s.get_one(Campaign, campaign.id)
        source = ModelFacts.model_validate({'prices': PRICES})
        listed = GenaiPrices()
        sonnet, haiku = (listed.usd(ModelId(vendor, name), tokens) for vendor, name in LISTED)
        assert sonnet is not None and haiku is not None
        own = 15.0 + 0.1 * 75.0 + 2 * 1.5 + 0.04 * 18.75
        assert source.gather(s, found, frozenset({MODELS_USD}), utcnow()) == {
            MODELS_USD: pytest.approx(own + sonnet + haiku)
        }
        assert source.models_usd(s, s.get_one(Campaign, them.id)) is None, 'no list knows haiku: unknown, never 0'
        agent(s, campaign, model=None, usage=tokens)
        assert source.models_usd(s, found) is None, "the harness's own default model has no price either"


LISTED = (('anthropic', 'claude-sonnet-4-5'), (None, 'claude-haiku-4-5'))


def test_spend_sums_its_facts_and_its_route_serves_each_part_and_the_spend_per_keep(
    db: Database, campaign: Campaign, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``spend`` is a fact that ``hold_at`` and the ``cost`` section read. Its route has the parts, as of their reading."""
    home = HOME_PLUGINS.validate_python({'executor': {'pools': {'serve': {'usd_per_hour': 3.6}}}})
    monkeypatch.setitem(vars(settings), 'providers', home)
    now = utcnow()
    cfg = configure(db, campaign, SPENDING)
    tokens = Usage(input=1_000_000, output=0, cache_read=0, cache_write=0, total=0, turns=1)
    with db.session() as s:
        hold(s, 'serve', (0, 1000), now, agent=agent(s, campaign, usage=tokens))
        s.add(LeaseWorker(id='serve', pool='serve', version='0', seen_at=now))
        s.commit()
        found = s.get_one(Campaign, campaign.id)
        assert gather(s, found, cfg, [SPEND], now).get(SPEND) == pytest.approx(15.0 + 1.0)
    add_run(db, campaign, keep_score=2.0)
    add_run(db, campaign, keep_score=3.0)
    uds = httpx.HTTPTransport(uds=str(Settings(home=tmp_path).socket_file))
    with serving_api(Settings(home=tmp_path)), httpx.Client(transport=uds, base_url='http://piloop') as http:
        answer = http.get(f'/v1/campaigns/{campaign.name}/spend').json()
        assert answer['usd'] == pytest.approx(16.0)
        assert answer['per_keep_usd'] == pytest.approx(8.0), 'two keeps: the baseline is none'
        assert [(p['fact'], p['usd']) for p in answer['parts']] == [
            ('compute_usd', pytest.approx(1.0)),
            ('models_usd', pytest.approx(15.0)),
        ]
        assert all(datetime.fromisoformat(p['as_of']) >= now for p in answer['parts']), 'computed as they are read'
        assert http.get(f'/v1/campaigns/{campaign.id}/spend').json()['usd'] == answer['usd'], 'by its id too'
        assert http.get('/v1/campaigns/nope/spend').json()['error'] == 'unknown_campaign'


def test_an_unknown_part_makes_the_spend_unknown_and_a_campaign_without_the_source_has_none(
    db: Database, campaign: Campaign, tmp_path: Path
) -> None:
    """A model without a price is unknown, so the sum is too: never the other parts shown as all of it."""
    configure(db, campaign, SPENDING)
    with db.session() as s:
        agent(s, campaign, model='haiku', usage=Usage(input=1, output=0, cache_read=0, cache_write=0, total=1, turns=1))
        s.commit()
    them = other(db, tmp_path)
    uds = httpx.HTTPTransport(uds=str(Settings(home=tmp_path).socket_file))
    with serving_api(Settings(home=tmp_path)), httpx.Client(transport=uds, base_url='http://piloop') as http:
        answer = http.get(f'/v1/campaigns/{campaign.name}/spend').json()
        assert (answer['usd'], answer['per_keep_usd']) == (None, None)
        assert [(p['fact'], p['usd']) for p in answer['parts']] == [('compute_usd', 0.0), ('models_usd', None)]
        assert http.get(f'/v1/campaigns/{them.name}/spend').json() is None, 'no spend source: no spend'
        store_config(db, them.id, {'name': 'other'})
        refused = http.get(f'/v1/campaigns/{them.name}/spend')
        assert (refused.status_code, refused.json()['error']) == (409, 'refused')


def test_a_part_that_a_source_reads_is_as_of_its_reading_and_unknown_before_the_first(
    db: Database, campaign: Campaign, tmp_path: Path
) -> None:
    """A ``command`` source's value, such as a cloud's bill, is a part of the spend as of when it was read."""
    billing = {'provider': 'command', 'command': ['true'], 'metrics': ['gpu_usd']}
    fact_source = {'use': ['campaign', 'optimization', 'billing', 'spend'], 'billing': billing}
    cfg = configure(db, campaign, {'fact_source': fact_source | {'spend': {'facts': ['gpu_usd']}}})
    uds = httpx.HTTPTransport(uds=str(Settings(home=tmp_path).socket_file))
    with serving_api(Settings(home=tmp_path)), httpx.Client(transport=uds, base_url='http://piloop') as http:
        assert http.get(f'/v1/campaigns/{campaign.name}/spend').json() == {
            'usd': None,
            'parts': [{'fact': 'gpu_usd', 'usd': None, 'as_of': None}],
            'per_keep_usd': None,
        }
        with db.session() as s:
            [source] = [found for found in cfg.fact_sources() if isinstance(found, Command)]
            source.record(s, s.get_one(Campaign, campaign.id), {'gpu_usd': 12.5})
            s.commit()
            read_at = source.as_of(s, s.get_one(Campaign, campaign.id), since_keep('gpu_usd'), utcnow())
        assert read_at is not None, 'and of its growth since the latest keep'
        [part] = http.get(f'/v1/campaigns/{campaign.name}/spend').json()['parts']
        assert (part['usd'], datetime.fromisoformat(part['as_of'])) == (12.5, read_at)


@pytest.mark.parametrize('facts', [['nothing_usd'], ['spend']])
def test_a_spend_of_a_fact_that_no_other_source_supplies_is_refused(campaign: Campaign, facts: list[str]) -> None:
    """A typo would leave the spend unknown for good, and the spend of itself has no end."""
    plugins = {'fact_source': SPENDING['fact_source'] | {'spend': {'facts': facts}}}
    with pytest.raises(ValueError, match=f"the fact source 'spend' reads {facts[0]}, which no other source"):
        reconfigured(campaign.config, plugins=plugins)
