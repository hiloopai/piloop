import shutil
from collections import Counter
from datetime import timedelta
from importlib.metadata import EntryPoint
from pathlib import Path

import pytest
from git import Repo
from pydantic import ValidationError
from sqlalchemy import select

from piloop.config import load_campaign_config
from piloop.db.models import AgentSession, Campaign
from piloop.db.session import Database
from piloop.ids import AgentSessionId, CampaignId
from piloop.plugins.campaign_type.kit.runs.ids import IMPLEMENTER
from piloop.plugins.campaign_type.kit.runs.tables import Run
from piloop.plugins.executor.kit.capacity import Pool
from piloop.plugins.executor.kit.leases import SERVE
from piloop.plugins.harness_choice.contract import HARNESS_CHOICES, HarnessCase, HarnessChoice, chosen
from piloop.plugins.harness_choice.round_robin import RoundRobin
from piloop.plugins.host import ContractBroken, data_errors
from piloop.plugins.launch_policy.contract import LAUNCH_POLICIES, LaunchCase, LaunchPolicy, Share
from piloop.plugins.launch_policy.fewest_live import FewestLive
from piloop.settings import DaemonSettings
from tests.conftest import HARNESS_SLEEPS, ScriptRuntime, register, run_all, workers_of, write_toml
from tests.daemon.test_roles import installed
from tests.daemon.test_run_controller import reload_campaign
from tests.daemon.test_scheduler import (
    add_ideas,
    died_at_startup,
    make_scheduler,
    pause_escalations,
    request_watchdog,
)


class First(LaunchPolicy):
    """``first``, for tests only: the waiting campaigns that it lists go first, in its order, the rest fewest live."""

    campaigns: list[str]

    def pick(self, case: LaunchCase) -> CampaignId | None:
        waiting = {share.name: share.campaign for share in case.campaigns if share.waits}
        listed = next((waiting[name] for name in self.campaigns if name in waiting), None)
        return listed if listed is not None else FewestLive().pick(case)


class Capped(LaunchPolicy):
    """For tests only: fewest live first, but no campaign runs more than ``max_live`` agents (a quota)."""

    max_live: int

    def pick(self, case: LaunchCase) -> CampaignId | None:
        under = tuple(share for share in case.campaigns if share.live < self.max_live)
        return FewestLive().pick(LaunchCase(free=case.free, campaigns=under))


class Rogue(LaunchPolicy):
    """For tests only: picks a campaign that does not wait."""

    def pick(self, case: LaunchCase) -> CampaignId | None:
        return CampaignId(999)


class Cheapest(HarnessChoice):
    """``cheapest``, for tests only: the free harness whose model costs least per ``prices``, ignoring the rotation."""

    prices: dict[str, float]

    def choose(self, case: HarnessCase) -> str | None:
        free = [h for h in case.rotation if h not in case.full]
        return min(free, key=lambda h: self.prices[case.models[h]]) if free else None


def share(campaign: int, live: int, *, waits: bool = True, name: str = '') -> Share:
    return Share(campaign=CampaignId(campaign), name=name or f'c{campaign}', live=live, waits=waits)


def test_both_contracts_are_data_in_data_out() -> None:
    assert data_errors(LaunchPolicy) == [] and data_errors(HarnessChoice) == []


def test_fewest_live_gives_the_slot_to_the_waiting_campaign_with_the_fewest_live_agents_then_the_oldest() -> None:
    policy = LAUNCH_POLICIES.selected({})
    assert isinstance(policy, FewestLive)
    assert policy.pick(LaunchCase(free=2, campaigns=(share(1, 2), share(2, 1), share(3, 1)))) == 2
    assert policy.pick(LaunchCase(free=2, campaigns=(share(1, 2), share(2, 0, waits=False)))) == 1, 'only who waits'
    assert policy.pick(LaunchCase(free=None, campaigns=(share(1, 0, waits=False),))) is None


def test_round_robin_takes_the_turns_harness_or_the_resumable_one_and_passes_over_a_full_one() -> None:
    policy = HARNESS_CHOICES.selected({})
    assert isinstance(policy, RoundRobin)

    def case(turn: int, full: frozenset[str] = frozenset(), resumes: str | None = None) -> HarnessCase:
        return HarnessCase(
            role=IMPLEMENTER,
            rotation=('a', 'b', 'c'),
            full=full,
            turn=turn,
            resumes=resumes,
            models={'a': 'ma', 'b': 'mb', 'c': 'mc'},
        )

    assert [policy.choose(case(n)) for n in range(4)] == ['a', 'b', 'c', 'a']
    assert policy.choose(case(1, full=frozenset({'b'}))) == 'a', 'the first harness that is not full'
    assert policy.choose(case(0, resumes='c')) == 'c', 'it keeps the harness that resumes the last session'
    assert policy.choose(case(0, full=frozenset({'a', 'b', 'c'}))) is None


def test_a_harness_choice_must_pick_a_free_harness_of_the_rotation() -> None:
    class Outside(HarnessChoice):
        def choose(self, case: HarnessCase) -> str | None:
            return 'elsewhere'

    case = HarnessCase(role=IMPLEMENTER, rotation=('a',), full=frozenset(), turn=0, resumes=None, models={'a': 'm'})
    with pytest.raises(ContractBroken, match="harness choice Outside chose 'elsewhere'"):
        chosen(Outside(), case)


def two_campaigns(db: Database, campaign: Campaign, tmp_path: Path) -> Campaign:
    """A second campaign, ``other``, next to ``campaign``: both have four implementer slots and three ideas."""
    other_path = tmp_path / 'other' / 'campaign'
    shutil.copytree(campaign.path, other_path)
    for path in (Path(campaign.path), other_path):
        write_toml(path, HARNESS_SLEEPS, max_implementers=4)
    toml = other_path / 'piloop.toml'
    toml.write_text(toml.read_text().replace('name = "demo"', 'name = "other"'))
    Repo(other_path).git.commit('-qam', 'other')
    other = register(db, other_path)
    reload_campaign(db, campaign)
    add_ideas(db, campaign, 'a1', 'a2', 'a3')
    add_ideas(db, other, 'b1', 'b2', 'b3')
    return other


def launched_by_campaign(db: Database, launched: list[AgentSessionId]) -> Counter[str]:
    with db.session() as s:
        return Counter(s.get_one(Campaign, s.get_one(AgentSession, i).campaign_id).name for i in launched)


def test_a_launch_policy_that_puts_one_campaign_first_gets_it_every_slot_it_can_take(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    """With ``first``, the listed campaign takes the three free slots; ``fewest_live`` would have split them 2 and 1."""
    two_campaigns(db, campaign, tmp_path)
    scheduler = make_scheduler(db, runtime, tmp_path, plugins={'launch_policy': First(campaigns=['other'])})
    workers_of(scheduler._controller).leases.pools = {SERVE: Pool(agents=3)}
    assert launched_by_campaign(db, run_all(scheduler)) == {'other': 3}
    scheduler.stop(timeout=5)


def test_a_launch_policy_that_picks_no_campaign_leaves_the_free_slots_idle(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    """A quota of one live agent per campaign: of four free slots, two stay free."""
    two_campaigns(db, campaign, tmp_path)
    scheduler = make_scheduler(db, runtime, tmp_path, plugins={'launch_policy': Capped(max_live=1)})
    workers_of(scheduler._controller).leases.pools = {SERVE: Pool(agents=4)}
    assert launched_by_campaign(db, run_all(scheduler)) == {'demo': 1, 'other': 1}
    scheduler.stop(timeout=5)


def test_a_launch_policy_that_picks_a_campaign_that_does_not_wait_breaks_its_contract(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    add_ideas(db, campaign, 'a1')
    scheduler = make_scheduler(db, runtime, tmp_path, plugins={'launch_policy': Rogue()})
    with pytest.raises(ContractBroken, match=r'launch policy Rogue picked campaign 999, which waits for no slot'):
        run_all(scheduler)
    scheduler.stop(timeout=5)


def test_the_startup_breaker_counts_with_the_launch_policys_limits(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    """``[plugins.launch_policy] startup_breaker = 1``: a single death at startup pauses the campaign's launches."""
    policy = FewestLive(startup_breaker=1, startup_window=timedelta(minutes=2))
    died_at_startup(db, campaign, IMPLEMENTER, minutes_ago=3)
    wanted = request_watchdog(db, campaign)
    scheduler = make_scheduler(db, runtime, tmp_path, plugins={'launch_policy': policy})
    assert wanted in run_all(scheduler), 'the death is older than the window'
    died_at_startup(db, campaign, IMPLEMENTER)
    request_watchdog(db, campaign)
    assert run_all(scheduler) == []
    [escalation] = pause_escalations(db)
    assert '1 agents failed in the last 2 minutes.' in escalation.body
    scheduler.stop(timeout=5)


def test_a_campaigns_harness_choice_picks_the_harness_of_each_run_it_queues(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cost-aware choice: every run goes to the harness whose model is cheapest, whatever the rotation's turn."""
    installed(monkeypatch, [EntryPoint('cheapest', f'{__name__}:Cheapest', HARNESS_CHOICES.group)])
    write_toml(Path(campaign.path), '#!/bin/sh\ntrue\n')
    cfg = load_campaign_config(
        Path(campaign.path),
        plugins={
            'harness': {'use': ['script', 'pi'], 'script': {'model': 'dear'}, 'pi': {'model': 'cheap'}},
            'role': {'implementer': {'max_concurrent': 3}},
            'harness_choice': {'provider': 'cheapest', 'prices': {'dear': 10.0, 'cheap': 1.0}},
        },
    )
    with db.session() as s:
        s.get_one(Campaign, campaign.id).config = cfg
        s.commit()
    add_ideas(db, campaign, 'a', 'b', 'c')
    scheduler = make_scheduler(db, runtime, tmp_path)
    run_all(scheduler)
    scheduler.stop(timeout=5)
    with db.session() as s:
        assert [r.harness for r in s.scalars(select(Run).order_by(Run.id))] == ['pi', 'pi', 'pi']


def test_the_daemon_section_has_no_startup_breaker_any_more() -> None:
    with pytest.raises(ValidationError, match='startup_breaker'):
        DaemonSettings.model_validate({'startup_breaker': 3})
