import json
import re
import subprocess
import tomllib
import warnings
from collections.abc import Mapping
from datetime import timedelta
from pathlib import Path
from unittest.mock import ANY

import pytest
from inline_snapshot import snapshot
from pydantic import ValidationError

from piloop import config as config_module
from piloop.config import CampaignConfig, ConfigError, ConfigMissing, load_campaign_config
from piloop.deprecated import PiloopDeprecationWarning
from piloop.ids import RoleName
from piloop.plugins.campaign_type.kit.evaluation.config import Metric, MetricRole, TrackConfig
from piloop.plugins.campaign_type.kit.evaluation.ids import TrackName
from piloop.plugins.campaign_type.kit.pool.best_rule.baseline import Baseline
from piloop.plugins.campaign_type.kit.pool.pool import best_rule, criterion
from piloop.plugins.campaign_type.kit.runs.ids import IMPLEMENTER
from piloop.plugins.campaign_type.optimization.section.pack import Pack
from piloop.plugins.executor.contract import EXECUTORS
from piloop.plugins.executor.workers import Workers
from piloop.plugins.fact_source.contract import FACT_SOURCES
from piloop.plugins.fact_source.models import ModelFacts
from piloop.plugins.harness.contract import Harness, each_provider
from piloop.plugins.metric import Direction
from piloop.plugins.role.contract import Continuity, ImplementerRole
from piloop.plugins.role.orchestrator import OrchestratorRole
from piloop.plugins.section.contract import SECTIONS
from piloop.plugins.section.goal import Goal
from piloop.plugins.section.lessons import Lessons
from piloop.plugins.stop_rule.contract import STOP_RULES, StopRule
from piloop.plugins.stop_rule.limits import Limits
from piloop.settings import Settings
from tests.conftest import AGENT_TIMEOUT, LEAD, LEAD_TRIGGERS, bare_config, role_of


def test_load_precedence(campaign_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = load_campaign_config(campaign_path)
    assert (cfg.name, cfg.runtime, cfg.harnesses, cfg.harness_for('script').model) == (
        'demo',
        'subprocess',
        ['script'],
        'test-model',
    )
    assert [t.primary.name for t in cfg.all_tracks] == ['score'] and [m.name for m in cfg.metrics] == ['score', 'size']
    assert cfg.harness_for('script').headless == ['{prompt}']
    assert cfg.harness_for('pi') == each_provider()['pi']
    override = {'executable': 'x'}
    loaded = load_campaign_config(campaign_path, plugins={'harness': {'script': override}})
    assert (loaded.harness_for('script').executable, loaded.harness_for('script').model) == ('x', 'test-model'), (
        'an override merges into the table of the file'
    )

    monkeypatch.setenv('PILOOP_CAMPAIGN_PLUGINS__ROLE__IMPLEMENTER__MAX_CONCURRENT', '3')
    monkeypatch.setenv('PILOOP_CAMPAIGN_EVALUATE_TIMEOUT', '9')
    cfg = load_campaign_config(campaign_path)
    assert (role_of(cfg, ImplementerRole).max_concurrent, cfg.evaluate_timeout) == (3, 9)
    assert load_campaign_config(campaign_path, evaluate_timeout=None).evaluate_timeout == 9


def test_a_table_of_a_built_in_harness_changes_only_its_keys_and_env_takes_values_and_secrets(
    campaign_path: Path,
) -> None:
    env = {'ANTHROPIC_BASE_URL': 'https://gw.example.com', 'ANTHROPIC_API_KEY': {'secret': 'TEAM_KEY'}}
    tables = {'use': ['script', 'claude'], 'claude': {'model': 'c', 'env': env}}
    claude = load_campaign_config(campaign_path, plugins={'harness': tables}).harness_for('claude')
    built_in = each_provider()['claude']
    assert claude.model_copy(update={'env': {}, 'model': None}) == built_in, (
        'the built-in keys that the table does not set'
    )
    assert (claude.literals, claude.secrets) == (
        {'ANTHROPIC_BASE_URL': 'https://gw.example.com'},
        {'ANTHROPIC_API_KEY': 'TEAM_KEY'},
    )
    with pytest.raises(ConfigError, match='plugins.harness'):
        load_campaign_config(campaign_path, plugins={'harness': ['claude']})
    with pytest.raises(ConfigError, match='plugins.harness.claude.env.ANTHROPIC_API_KEY'):
        load_campaign_config(
            campaign_path, plugins={'harness': {'claude': {'env': {'ANTHROPIC_API_KEY': {'value': 'sk-1'}}}}}
        )


def test_roles_are_typed_per_kind(campaign_path: Path) -> None:
    cfg = load_campaign_config(campaign_path)
    assert cfg.roles.all() == {
        IMPLEMENTER: ImplementerRole(timeout=AGENT_TIMEOUT, max_concurrent=1),
        LEAD: OrchestratorRole(
            triggers=['run_failed'],
            continuity=Continuity.FRESH,
            harnesses=['script'],
            model={'script': 'lead-model'},
            timeout=AGENT_TIMEOUT,
        ),
    }, 'every role, in the order of its entry point, with its table over its defaults'
    assert cfg.role(LEAD) is role_of(cfg, OrchestratorRole)
    assert cfg.role_harnesses(IMPLEMENTER) == ['script'], 'unset: the campaign rotation'
    assert cfg.role_models(LEAD) == {'script': 'lead-model'}, "role map over each harness's model"
    assert CampaignConfig.model_validate(cfg.model_dump(mode='json')) == cfg, 'round-trips through the campaign row'
    dumped = cfg.model_dump(mode='json')['plugins']
    assert dumped['role']['implementer'] == {'timeout': AGENT_TIMEOUT, 'max_concurrent': 1}, 'only what its table sets'
    assert dumped['harness']['script'] == {
        'model': 'test-model',
        'executable': ANY,
        'headless': ['{prompt}'],
    }
    defaults = CampaignConfig(
        name='bare',
        metrics=[Metric(name='a', direction=Direction.HIGHER, role=MetricRole.PRIMARY)],
        evaluate=['true'],
        plugins={'harness': {'claude': {'model': 'm'}, 'codex': {'model': 'm'}}},
    )
    assert defaults.context == [], 'no analysis artifacts unless the campaign names them'
    implementer = role_of(defaults, ImplementerRole)
    assert (implementer.timeout, implementer.max_concurrent) == (3600, 2), 'two workers, an hour each'
    assert list(defaults.roles.all()) == [IMPLEMENTER, 'orchestrator'], 'no research, no watchdog'


LEAD_OF_0_5 = 'the cron trigger is removed: delete cron from [plugins.role.orchestrator] triggers'
LOW_WATER_OF_0_6 = (
    'the low_water trigger is removed with the research role: delete low_water from [plugins.role.orchestrator]'
    ' triggers'
)
ROUTINE_OF_0_5 = 'nothing routine wakes the orchestrator now, so routine_every is removed'
QUIET_OF_0_6 = (
    "the news trigger is the orchestrator's own now: write quiet_seconds in [plugins.role.orchestrator], not in"
    ' [plugins.trigger.news]'
)


def dropped(warned: pytest.WarningsRecorder) -> list[str]:
    return sorted(str(w.message).partition(';')[0] for w in warned)


def test_an_orchestrator_of_0_5_that_plans_on_a_timer_loads_from_its_file_without_it_and_warns(
    campaign_path: Path,
) -> None:
    toml = campaign_path / 'piloop.toml'
    lead = 'triggers = ["news", "low_water", "cron", "run_failed"]'
    tables = '\n[plugins.trigger.news]\nquiet_seconds = 5\nroutine_every = 900\n'
    toml.write_text(toml.read_text().replace(LEAD_TRIGGERS, lead) + tables)
    with pytest.warns(PiloopDeprecationWarning) as warned:
        cfg = load_campaign_config(campaign_path)
    assert dropped(warned) == sorted([ROUTINE_OF_0_5, LOW_WATER_OF_0_6, LEAD_OF_0_5, QUIET_OF_0_6])
    lead = role_of(cfg, OrchestratorRole)
    assert (lead.triggers, lead.quiet_seconds) == (['news', 'run_failed'], 5)


def test_an_orchestrator_that_a_query_wakes_loads_without_it_and_warns(campaign_path: Path) -> None:
    """The orchestrator wakes only on news and failed runs: it drops any other trigger, such as a query of the board."""
    toml = campaign_path / 'piloop.toml'
    query = "SELECT 1 FROM ideas WHERE campaign_id = :campaign AND status = 'open'"
    tables = f'\n[plugins.trigger.board]\nprovider = "sql"\nwhen = "{query}"\n'
    toml.write_text(toml.read_text().replace(LEAD_TRIGGERS, 'triggers = ["news", "board"]') + tables)
    with pytest.warns(PiloopDeprecationWarning) as warned:
        cfg = load_campaign_config(campaign_path)
    assert dropped(warned) == [
        'the orchestrator wakes only on news and run_failed now, not on board: delete them from'
        ' [plugins.role.orchestrator] triggers',
        'the sql trigger is removed with the sweeps of your own: delete [plugins.trigger.board]',
    ]
    assert role_of(cfg, OrchestratorRole).triggers == ['news']


def test_a_campaign_that_0_5_stored_with_an_orchestrator_that_plans_on_a_timer_loads_without_it_and_warns(
    campaign_path: Path,
) -> None:
    """The stored copy of ``piloop.toml`` (``campaigns.config``) goes the same validation as the file: no migration."""
    stored = load_campaign_config(campaign_path).model_dump(mode='json')
    stored['plugins']['role']['orchestrator'] = {'triggers': ['news', 'low_water', 'cron', 'run_failed']}
    stored['plugins']['trigger'] = {'news': {'quiet_seconds': 5, 'routine_every': 900}}
    with pytest.warns(PiloopDeprecationWarning) as warned:
        cfg = CampaignConfig.model_validate(stored)
    assert dropped(warned) == sorted([ROUTINE_OF_0_5, LOW_WATER_OF_0_6, LEAD_OF_0_5, QUIET_OF_0_6])
    lead = role_of(cfg, OrchestratorRole)
    assert (lead.triggers, lead.quiet_seconds) == (['news', 'run_failed'], 5)


def test_the_trigger_tables_of_0_6_load_without_them_and_warn_for_each_that_sets_a_key(campaign_path: Path) -> None:
    """The triggers are the orchestrator's own: a stored config's empty table of each goes without a word."""
    stored = load_campaign_config(campaign_path).model_dump(mode='json')
    stored['plugins']['trigger'] = {'news': {}, 'run_failed': {}, 'soon': {'provider': 'news', 'quiet_seconds': 3}}
    with pytest.warns(PiloopDeprecationWarning) as warned:
        cfg = CampaignConfig.model_validate(stored)
    assert dropped(warned) == [
        "the triggers are the orchestrator's own now, with no table: delete [plugins.trigger.soon]"
    ]
    assert 'trigger' not in cfg.model_dump(mode='json')['plugins']
    assert role_of(cfg, OrchestratorRole).quiet_seconds == 30, 'only [plugins.trigger.news] moves'


def test_the_idea_caps_of_the_orchestrator_of_0_6_load_without_them_and_warn(campaign_path: Path) -> None:
    """The lead only decides: piloop caps none of its ideas, so ``files``, ``plan_words`` and ``max_ideas`` go."""
    toml = campaign_path / 'piloop.toml'
    caps = 'files = "nothing"\nplan_words = 90\nmax_ideas = 2'
    toml.write_text(toml.read_text().replace(LEAD_TRIGGERS, f'{LEAD_TRIGGERS}\n{caps}'))
    with pytest.warns(PiloopDeprecationWarning) as warned:
        cfg = load_campaign_config(campaign_path)
    assert dropped(warned) == [
        f'the lead files ideas with no cap now: delete {key} from [plugins.role.orchestrator]'
        for key in ('files', 'max_ideas', 'plan_words')
    ]
    assert 'max_ideas' not in role_of(cfg, OrchestratorRole).model_dump()


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigMissing, match=re.escape(f'{tmp_path} has no piloop.toml: `piloop init {tmp_path}`')):
        load_campaign_config(tmp_path)


def test_validators(campaign_path: Path) -> None:
    with pytest.raises(
        ValueError, match=re.escape("plugins.harness.use[0] = 'nope': no [plugins.harness.nope]; there")
    ):
        load_campaign_config(campaign_path, plugins={'harness': {'use': ['nope']}})
    with pytest.raises(ValueError, match=re.escape("plugins.role.implementer: no model for the harnesses ['pi']")):
        load_campaign_config(campaign_path, plugins={'harness': {'use': ['script', 'pi']}})
    with pytest.raises(ValueError, match=re.escape('plugins.harness.use: List should have at least 1 item')):
        load_campaign_config(campaign_path, plugins={'harness': {'use': []}})
    with pytest.raises(ValueError, match=re.escape('plugins.runtime: the instances are in the other file')):
        load_campaign_config(campaign_path, plugins={'runtime': {'fast': {'provider': 'subprocess'}}})
    with pytest.raises(ValueError, match='relative path'):
        load_campaign_config(campaign_path, link=['/etc'])
    with pytest.raises(ValueError, match='relative path'):
        load_campaign_config(campaign_path, plugins={'role': {'orchestrator': {'addendum': '../elsewhere.md'}}})
    with pytest.raises(ValueError, match='at least one metric'):
        load_campaign_config(campaign_path, metrics=[{'name': 'a', 'direction': 'higher'}])
    with pytest.raises(ConfigError, match=re.escape("plugins.role.orchestrator.prompt = 'gone.md': no such file")):
        load_campaign_config(campaign_path, plugins={'role': {'orchestrator': {'prompt': 'gone.md'}}})
    with pytest.raises(ValueError, match='plugins.role.janitor.triggers'):
        load_campaign_config(campaign_path, plugins={'role': {'janitor': {'runs': True, 'triggers': []}}})
    with pytest.raises(ValueError, match='plugins.role.implementer.max_ideas'):
        load_campaign_config(campaign_path, plugins={'role': {'implementer': {'max_ideas': 3}}})
    with pytest.raises(ValueError, match='plugins.role.orchestrator.min_ready'):
        load_campaign_config(campaign_path, plugins={'role': {'orchestrator': {'min_ready': 3}}})
    with pytest.raises(
        ValueError, match=re.escape("plugins.role.orchestrator.harnesses[0] = 'nope': no [plugins.harness")
    ):
        load_campaign_config(campaign_path, plugins={'role': {'orchestrator': {'harnesses': ['nope']}}})
    with pytest.raises(
        ValueError, match=re.escape("plugins.role.orchestrator.model = 'nope': no [plugins.harness.nope]")
    ):
        load_campaign_config(campaign_path, plugins={'role': {'orchestrator': {'model': {'nope': 'm'}}}})
    with pytest.raises(ValueError, match=re.escape("plugins.role.orchestrator: no model for the harnesses ['pi']")):
        load_campaign_config(campaign_path, plugins={'role': {'orchestrator': {'harnesses': ['pi']}}})
    with pytest.raises(ValueError, match='plugins.harness.claude.max_concurrent'):
        load_campaign_config(campaign_path, plugins={'harness': {'claude': {'max_concurrent': 0}}})


def test_an_unknown_harness_key_in_the_file_fails_loudly(campaign_path: Path) -> None:
    """A misspelt key such as ``startup_dialog`` fails: dropping it silently would leave the dialogs unanswered."""
    toml = campaign_path / 'piloop.toml'
    toml.write_text(
        toml.read_text()
        + '\n[plugins.harness.codex]\nexecutable = "codex"\nheadless = ["{prompt}"]\nstartup_dialog = ["trust", ["enter"]]\n'
    )
    with pytest.raises(ValueError, match='plugins.harness.codex.startup_dialog'):
        load_campaign_config(campaign_path)


def test_the_limits_of_the_stop_rule_are_optional_and_validated(campaign_path: Path) -> None:
    def rule(cfg: CampaignConfig) -> StopRule:
        return STOP_RULES.selected(cfg.plugins)

    assert rule(load_campaign_config(campaign_path)) == Limits(), 'no limit: open-ended'
    limits = {'target': 0.85, 'max_runs': 200, 'max_hours': 12, 'plateau': 25}
    cfg = load_campaign_config(campaign_path, plugins={'stop_rule': limits})
    assert rule(cfg) == Limits(target=0.85, max_runs=200, max_hours=12.0, plateau=25)
    assert rule(CampaignConfig.model_validate(cfg.model_dump(mode='json'))) == rule(cfg)
    with pytest.raises(ValueError, match='plugins.stop_rule.target'):
        load_campaign_config(campaign_path, plugins={'stop_rule': {'target': 'high'}})
    with pytest.raises(ValueError, match='plugins.stop_rule.max_runs'):
        load_campaign_config(campaign_path, plugins={'stop_rule': {'max_runs': 0}})
    with pytest.raises(ValueError, match='plugins.stop_rule.plateau'):
        load_campaign_config(campaign_path, plugins={'stop_rule': {'plateau': 0}})
    with pytest.raises(ValueError, match='plugins.stop_rule.max_hours'):
        load_campaign_config(campaign_path, plugins={'stop_rule': {'max_hours': 0}})
    with pytest.raises(ValueError, match='plugins.stop_rule.budget'):
        load_campaign_config(campaign_path, plugins={'stop_rule': {'budget': 3}})


def test_an_invalid_piloop_toml_names_the_file_each_key_its_value_and_the_fix(campaign_path: Path) -> None:
    toml = campaign_path / 'piloop.toml'
    text = toml.read_text()
    toml.write_text(
        text.replace('name = "demo"', 'colour = "blue"').replace('direction = "higher"', 'direction = "up"')
    )
    with pytest.raises(ConfigError) as caught:
        load_campaign_config(campaign_path)
    assert str(caught.value) == (
        f'{toml} is not a valid piloop config (3 problem(s)):\n'
        '  name: Field required. Fix: add the key; https://piloop.autoresearch.inc/reference/configuration says what it takes\n'
        "  metrics[0].direction = 'up': Input should be 'higher' or 'lower'. Fix: https://piloop.autoresearch.inc/reference/configuration"
        ' describes the key\n'
        "  colour = 'blue': Extra inputs are not permitted. Fix: delete the key or correct its spelling;"
        ' https://piloop.autoresearch.inc/reference/configuration lists every key'
    )
    toml.write_text(text.replace('role = "primary"', ''))
    with pytest.raises(ConfigError, match=r'\(the file\): at least one metric must be primary\. Fix:'):
        load_campaign_config(campaign_path)
    toml.write_text('name = ')
    with pytest.raises(ConfigError, match=f'{toml} is not valid TOML: '):
        load_campaign_config(campaign_path)


def test_metric_direction() -> None:
    lower = Metric(name='m', direction=Direction.LOWER, role=MetricRole.PRIMARY)
    assert lower.better(1.0, 2.0) and not lower.better(2.0, 1.0)


def test_default_harnesses_have_prompt_placeholder() -> None:
    assert {name: '{prompt}' in h.headless for name, h in each_provider().items()} == snapshot(
        {'pi': True, 'claude': True, 'codex': True}
    )


def test_default_harnesses_need_their_session_id_and_say_where_it_comes_from() -> None:
    found = {n: (h.needs_session_id, h.session_id_pattern) for n, h in each_provider().items()}
    assert found == snapshot(
        {
            'pi': (True, None),
            'claude': (True, None),
            'codex': (True, '"thread_id":"([0-9a-f-]{36})"'),
        }
    ), 'pi and claude are told their id; codex prints its own, which the subprocess runtime reads back'
    for name in ('pi', 'claude'):
        h = each_provider()[name]
        assert '--session-id {session_id}' in ' '.join(h.headless), name
    assert '--session-dir' not in each_provider()['pi'].headless, "usage reads pi's own session store"
    Harness(executable='x', headless=['{prompt}'])
    for resume in (['{prompt}'], ['--resume', '{session_id}']):
        with pytest.raises(ValueError, match=r'resume must include \{session_id\} and \{prompt\}'):
            Harness(executable='x', headless=['{prompt}'], resume=resume)
    assert [' '.join(each_provider()[name].resume[:3]) for name in ('pi', 'claude', 'codex')] == [
        '-p --mode json',
        '-p --output-format json',
        'exec resume --json',
    ], 'each continues the session of its headless run (verified live with pi 0.99.2, Claude Code, codex-cli 0.162.1)'


def test_default_harnesses_take_the_roles_effort_in_every_argv() -> None:
    thinking = {
        'pi': '--thinking {effort}',
        'claude': '--effort {effort}',
        'codex': '-c model_reasoning_effort="{effort}"',
    }
    for name, flags in thinking.items():
        h = each_provider()[name]
        assert all(flags in ' '.join(argv) for argv in (h.headless, h.resume)), name


def test_the_codex_config_table_is_a_c_flag_for_each_key_before_piloop_s_own_so_they_win(
    campaign_path: Path,
) -> None:
    """A gateway for Codex: ``model_provider`` and its ``model_providers`` table, as Codex's own ``-c`` takes them."""
    gateway = {'name': 'gateway', 'base_url': 'https://gw.example.com/v1', 'env_key': 'GATEWAY_KEY', 'retries': 2}
    config = {'model_provider': 'gateway', 'model_providers': {'gateway': gateway}, 'web_search': False, 'p': 0.5}
    cfg = load_campaign_config(
        campaign_path, plugins={'harness': {'use': ['codex'], 'codex': {'model': 'gpt-6', 'config': config}}}
    )
    codex = cfg.harness_for('codex')
    flags = [
        '-c',
        'model_provider="gateway"',
        '-c',
        'model_providers={gateway = {name = "gateway", base_url = "https://gw.example.com/v1", env_key = "GATEWAY_KEY",'
        ' retries = 2}}',
        '-c',
        'web_search=false',
        '-c',
        'p=0.5',
    ]
    for argv in (codex.headless, codex.resume):
        args = codex.expanded(argv)
        at = args.index('{model}') + 1
        assert args[at : at + len(flags)] == flags
        after = args[at + len(flags) + len(codex.isolation()) :]
        assert after[:2] == ['-c', 'model_reasoning_effort="{effort}"'], "piloop's own come last"
    assert each_provider()['codex'].expanded(['exec', '{config}']) == ['exec'], 'no table, no flag'
    assert Harness(executable='x', headless=['{config}']).expanded(['{config}']) == ['{config}'], 'a Codex element'
    with pytest.raises(ValueError, match=r'config: a value of no TOML type: None'):
        load_campaign_config(
            campaign_path, plugins={'harness': {'use': ['codex'], 'codex': {'model': 'x', 'config': {'a': None}}}}
        )
    with pytest.raises(ValueError, match=r'config needs \{config\} in headless and resume'):
        load_campaign_config(
            campaign_path,
            plugins={
                'harness': {'use': ['codex'], 'codex': {'model': 'x', 'headless': ['{prompt}'], 'config': config}}
            },
        )


def test_metric_roles_are_validated(campaign_path: Path) -> None:
    primary = {'name': 'score', 'direction': 'higher', 'role': 'primary'}
    guard = {'name': 'size', 'direction': 'lower', 'role': 'guardrail', 'margin': 3, 'floor': 10}
    cfg = load_campaign_config(campaign_path, metrics=[primary, guard])
    [track] = cfg.all_tracks
    assert (track.name, track.primary.name, track.addendum) == ('score', 'score', None), (
        'one primary is one track named after it, with nothing to configure'
    )
    assert ([m.name for m in cfg.guardrails], [m.name for m in track.guardrails]) == (['size'], ['size'])
    assert not cfg.multitrack and cfg.track(TrackName('score')) == track
    assert cfg.guardrails[0].gain(9, 10) == 1 and track.primary.gain(9, 10) == -1
    with pytest.raises(KeyError, match="no track 'speed' in piloop.toml; tracks: score"):
        cfg.track(TrackName('speed'))
    with pytest.raises(ValueError, match="guardrail 'size' needs a margin or a fraction"):
        load_campaign_config(campaign_path, metrics=[primary, {**guard, 'margin': None}])
    with pytest.raises(ValueError, match='set margin or fraction, not both'):
        load_campaign_config(campaign_path, metrics=[primary, {**guard, 'fraction': 0.1}])
    relative = load_campaign_config(campaign_path, metrics=[primary, {**guard, 'margin': None, 'fraction': 0.1}])
    assert criterion(relative.guardrails[0]).allowed(40) == 4.0, "a fraction of the baseline's, in the metric's units"
    with pytest.raises(ValueError, match="'size': floor, margin and fraction apply to guardrails and primaries only"):
        load_campaign_config(campaign_path, metrics=[primary, {**guard, 'role': 'watch'}])
    with pytest.raises(
        ValueError, match="'score': floor, margin and fraction on a primary apply only with several tracks"
    ):
        load_campaign_config(campaign_path, metrics=[{**primary, 'floor': 1}])
    with pytest.raises(ValueError, match="'size': track applies to primaries only"):
        load_campaign_config(campaign_path, metrics=[primary, {**guard, 'track': 'x'}])
    with pytest.raises(ValueError, match=r'metrics\[1\]\.margin = -1'):
        load_campaign_config(campaign_path, metrics=[primary, {**guard, 'margin': -1}])
    with pytest.raises(
        ValueError, match=r'metrics\[0\]\.primary = True: Extra inputs are not permitted\. Fix: delete the key'
    ):
        load_campaign_config(campaign_path, metrics=[{'name': 'score', 'direction': 'higher', 'primary': True}])


ACCURACY = {'name': 'score', 'direction': 'higher', 'role': 'primary', 'track': 'accuracy', 'margin': 1, 'floor': 2}
COST = {'name': 'size', 'direction': 'lower', 'role': 'primary', 'track': 'serving', 'margin': 5}
ADDENDA = {'accuracy': {'addendum': 'briefs/accuracy.md'}}


def test_tracks_are_primaries_each_with_a_margin_and_a_table_only_for_an_addendum(campaign_path: Path) -> None:
    cfg = load_campaign_config(campaign_path, metrics=[ACCURACY, COST], tracks=ADDENDA)
    assert cfg.multitrack
    assert [(t.name, t.primary.name, t.addendum, [m.name for m in t.guardrails]) for t in cfg.all_tracks] == [
        ('accuracy', 'score', 'briefs/accuracy.md', ['size']),
        ('serving', 'size', None, ['score']),
    ], 'serving needs no table'
    assert CampaignConfig.model_validate(cfg.model_dump(mode='json')) == cfg, 'round-trips through the campaign row'
    tracks = {'metrics': [ACCURACY, COST], 'tracks': ADDENDA}
    refusals = {
        r"tracks \['accuracy'\] have more than one primary": {'metrics': [ACCURACY, {**COST, 'track': 'accuracy'}]},
        r"primaries \['size'\] need a margin or a fraction": {
            **tracks,
            'metrics': [ACCURACY, {**COST, 'margin': None}],
        },
        r"\[tracks\] \['speed'\] name no track a primary declares": {
            **tracks,
            'tracks': {**ADDENDA, 'speed': {}},
        },
        "the stop rule 'limits' limits the best, which needs a campaign of one track": {
            **tracks,
            'plugins': {'stop_rule': {'target': 3}},
        },
        "'briefs/../x' must be a relative path inside the repo": {
            **tracks,
            'tracks': {**ADDENDA, 'serving': {'addendum': 'briefs/../x'}},
        },
    }
    for message, overrides in refusals.items():
        with pytest.raises(ValueError, match=message):
            load_campaign_config(campaign_path, **overrides)
    one = load_campaign_config(campaign_path, tracks={'score': {'addendum': 'briefs/score.md'}})
    assert one.all_tracks[0].addendum == 'briefs/score.md', 'one track may still have its addendum'
    guarded = [
        {'name': 'score', 'direction': 'higher', 'role': 'primary'},
        {**COST, 'role': 'guardrail', 'track': None},
    ]
    assert load_campaign_config(campaign_path, metrics=[ACCURACY, COST], tracks=ADDENDA).evaluator_key == (
        load_campaign_config(campaign_path, metrics=guarded).evaluator_key
    ), 'tracks, margins and floors are the policy of the best: they re-measure nothing'


def test_the_evaluator_is_what_scores_the_judging_metrics_not_how_the_best_rule_reads_them(campaign_path: Path) -> None:
    primary = {'name': 'score', 'direction': 'higher', 'role': 'primary'}
    size = {'name': 'size', 'direction': 'lower', 'role': 'guardrail', 'margin': 3}
    cfg = load_campaign_config(campaign_path, metrics=[primary, size])
    assert cfg.evaluator_definition == {
        'evaluate': cfg.evaluate,
        'link': ['data'],
        'judging': [{'name': 'score', 'direction': 'higher'}, {'name': 'size', 'direction': 'lower'}],
    }

    def key(*metrics: Mapping[str, object], **overrides: object) -> str:
        return load_campaign_config(campaign_path, metrics=list(metrics), **overrides).evaluator_key

    unchanged = {
        'a new margin': key(primary, {**size, 'margin': 9}),
        'a primary that is a guardrail now': key(
            {**primary, 'track': 'a', 'margin': 1},
            {**size, 'role': 'primary', 'track': 'b'},
        ),
        'another order of the metrics': key(size, primary),
        'a best rule': key(primary, size, plugins={'best_rule': {'provider': 'baseline'}}),
        'a new watch metric': key(primary, size, {'name': 'eval_cost_usd', 'direction': 'lower'}),
        'no ladder': key(primary, size, ladder=[]),
    }
    assert unchanged == dict.fromkeys(unchanged, cfg.evaluator_key), 'none changes a score already measured'
    changed = {
        'a watch metric that judges now': key(primary, size, {**size, 'name': 'cost', 'margin': 1}),
        'a guardrail that only watches now': key(primary, {'name': 'size', 'direction': 'lower'}),
        'a new direction': key({**primary, 'direction': 'lower'}, size),
        'a new evaluate command': key(primary, size, evaluate=['true']),
        'a new linked input': key(primary, size, link=['data', 'more']),
        'a ladder': key(primary, size, ladder=[SCREEN]),
        "a rung's command": key(primary, size, ladder=[{**SCREEN, 'evaluate': ['smoke']}]),
    }
    assert key(primary, size, ladder=[SCREEN]) == key(
        primary, size, ladder=[{**SCREEN, 'stop': {'size': {'floor': 9}}}]
    ), "a rung's stop is the policy of the climb"
    assert len({cfg.evaluator_key, *changed.values()}) == len(changed) + 1, 'each can change a score'
    assert (
        load_campaign_config(campaign_path).evaluator_key
        == load_campaign_config(campaign_path, metrics=[primary]).evaluator_key
    ), 'a watch metric is no part of the evaluator'


def test_default_harnesses_keep_agents_out_of_the_harness_memory() -> None:
    """A memory the harness keys by repo would carry one agent's notes into every later agent of the campaign."""
    off = {'claude': ['"autoMemoryEnabled":false'], 'codex': ['-c', 'features.memories=false']}
    for name, flags in off.items():
        h = each_provider()[name]
        assert ' '.join(flags) in ' '.join(h.headless) and ' '.join(flags) in ' '.join(h.resume), name


def test_default_claude_moves_a_long_bash_command_to_the_background() -> None:
    """Claude agents get a 5-minute default and a 10-minute maximum for a foreground Bash command, whatever the user's."""
    claude = each_provider()['claude']
    for argv in (claude.headless, claude.resume):
        settings = json.loads(argv[argv.index('--settings') + 1])
        assert settings['env'] == {'BASH_DEFAULT_TIMEOUT_MS': '300000', 'BASH_MAX_TIMEOUT_MS': '600000'}


def test_default_claude_takes_the_queued_messages_after_each_tool_call(tmp_path: Path) -> None:
    """A headless turn can last an hour, and a message waits for its end. A hook after each tool call reads the queue.

    Without a message, the hook costs a file check: piloop starts only when the queue has one.
    """
    claude = each_provider()['claude']
    commands = set()
    for argv in (claude.headless, claude.resume):
        settings = json.loads(argv[argv.index('--settings') + 1])
        [entry] = settings['hooks']['PostToolUse']
        [hook] = entry['hooks']
        commands.add(hook['command'])
    [command] = commands
    fake = tmp_path / 'piloop'
    fake.write_text('#!/bin/sh\necho "took: $*"\n')
    fake.chmod(0o755)
    queue = tmp_path / 'queued-messages'

    def hook(**env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ['sh', '-c', command],
            env={'PATH': '/usr/bin:/bin', 'PILOOP_BIN': str(fake)} | env,
            capture_output=True,
            text=True,
            check=False,
        )

    assert (hook().returncode, hook().stdout) == (0, ''), 'no queue named'
    assert (hook(PILOOP_MESSAGES=str(queue)).returncode, hook(PILOOP_MESSAGES=str(queue)).stdout) == (0, '')
    queue.write_text('"x"\n')
    assert hook(PILOOP_MESSAGES=str(queue)).stdout == 'took: agent messages --harness claude\n'


def test_a_sweep_of_your_own_and_its_query_load_without_them_and_warn(campaign_path: Path) -> None:
    """Only the orchestrator sweeps: a table of a new name without ``runs = true`` goes, and so does a ``sql`` trigger."""
    (campaign_path / 'reviewer.md').write_text('# Review\n')
    when = 'SELECT 1 FROM ideas WHERE campaign_id = :campaign AND created_at > :since'
    reviewer = {'prompt': 'reviewer.md', 'triggers': ['new_idea'], 'runtime': 'subprocess'}
    plugins = {'role': {'reviewer': reviewer}, 'trigger': {'new_idea': {'provider': 'sql', 'when': when}}}
    with pytest.warns(PiloopDeprecationWarning) as warned:
        cfg = load_campaign_config(campaign_path, plugins=plugins)
    assert dropped(warned) == [
        'a sweep of your own is removed: delete [plugins.role.reviewer]',
        'the sql trigger is removed with the sweeps of your own: delete [plugins.trigger.new_idea]',
    ]
    assert list(cfg.roles.all()) == [IMPLEMENTER, LEAD]
    stored = cfg.model_dump(mode='json')
    stored['plugins']['role']['reviewer'] = reviewer
    with pytest.warns(PiloopDeprecationWarning, match='a sweep of your own is removed'):
        assert CampaignConfig.model_validate(stored) == cfg, 'a stored config goes the same validation: no migration'
    for name in ('run', 'Review Bot'):
        with pytest.raises(ConfigError, match=f'{name!r} is not a role name: use lowercase letters, digits, _ and -'):
            load_campaign_config(campaign_path, plugins={'role': {name: {'runs': True}}})


def test_a_table_with_runs_defines_a_role_that_does_runs_with_the_implementer_defaults(campaign_path: Path) -> None:
    quick = {'runs': True, 'timeout': 60, 'max_concurrent': 1}
    cfg = load_campaign_config(campaign_path, plugins={'role': {'quick': quick}})
    assert list(cfg.roles.all()) == ['implementer', 'orchestrator', 'quick']
    assert list(cfg.roles.doing_runs()) == ['quick', 'implementer'], 'the roles of the file fill their slots first'
    assert list(cfg.roles.starting()) == ['orchestrator']
    role = cfg.roles.doing_runs()[RoleName('quick')]
    assert (role.prompt, role.PROMPT, role.timeout) == (None, ImplementerRole.PROMPT, 60)
    assert cfg.run_slots == 1 + cfg.roles.doing_runs()[IMPLEMENTER].max_concurrent
    assert CampaignConfig.model_validate(cfg.model_dump(mode='json')) == cfg, 'round-trips through the campaign row'

    def fails(roles: dict[str, object]) -> str:
        with pytest.raises(ConfigError) as e:
            load_campaign_config(campaign_path, plugins={'role': roles})
        return str(e.value).split('\n', 1)[1]

    assert 'plugins.role.quick.triggers: Extra inputs are not permitted' in fails(
        {'quick': {'runs': True, 'triggers': []}}
    )
    assert 'plugins.role.orchestrator.runs = True: Input should be False' in fails({'orchestrator': {'runs': True}})
    assert 'plugins.role.implementer.runs = False: Input should be True' in fails({'implementer': {'runs': False}})


SCREEN = {'name': 'screen', 'stop': {'score': {'margin': 0.1}}}


@pytest.mark.parametrize(
    ('ladder', 'error'),
    [
        ([SCREEN, SCREEN], "ladder: rungs ['screen'] appear more than once"),
        ([{**SCREEN, 'stop': {'accuracy': {'floor': 1}}}], "ladder.screen.stop: ['accuracy'] are not in [[metrics]]"),
        (
            [SCREEN, {'name': 'smoke', 'stop': {'score': {'floor': 1}}, 'evaluate': ['smoke']}],
            'ladder: the rungs with their own evaluate come first',
        ),
        ([{**SCREEN, 'stop': {'score': {'margin': 0.1, 'fraction': 0.1}}}], 'set margin or fraction, not both'),
        ([{**SCREEN, 'stop': {'score': {}}}], "stop of ['score']: set floor, margin or fraction"),
        ([{**SCREEN, 'stop': {}}], 'stop'),
        ([{**SCREEN, 'name': 'Bad Name'}], 'name'),
    ],
)
def test_a_ladder_names_its_rungs_once_stops_on_known_metrics_and_runs_its_own_commands_first(
    campaign_path: Path, ladder: list[dict[str, object]], error: str
) -> None:
    with pytest.raises(ConfigError, match=re.escape(error)):
        load_campaign_config(campaign_path, ladder=ladder)


def test_a_rung_may_stop_on_a_floor_and_a_regression_together(campaign_path: Path) -> None:
    rung = {'name': 'smoke', 'evaluate': ['smoke'], 'stop': {'score': {'floor': 1, 'fraction': 0.2}}}
    cfg = load_campaign_config(campaign_path, ladder=[rung, SCREEN])
    assert [r.name for r in cfg.ladder] == ['smoke', 'screen']


def test_a_capability_of_one_instance_written_as_named_tables_says_so(campaign_path: Path) -> None:
    """``[plugins.best_rule.strict]`` reads as a key ``strict`` of the one best rule: the error says what to write."""
    with pytest.raises(ConfigError) as caught:
        load_campaign_config(campaign_path, plugins={'best_rule': {'strict': {'min_gain': 2}}})
    assert str(caught.value).split('\n', 1)[1] == snapshot(
        '  plugins.best_rule: a best rule has one instance, not one for each name (strict): write its keys in [plugins.best_rule], with provider = "<best rule>" to choose one. Fix: https://piloop.autoresearch.inc/reference/configuration describes the key'
    )
    with pytest.raises(ConfigError, match=r"plugins\.best_rule\.confidence = 'high'"):
        load_campaign_config(campaign_path, plugins={'best_rule': {'confidence': 'high'}})
    held = load_campaign_config(campaign_path, plugins={'stop_rule': {'hold_at': {'hours': 5}}})
    assert STOP_RULES.selected(held.plugins) == Limits(hold_at={'hours': 5}), 'a parameter that is a table is one'


def test_a_capability_s_own_settings_are_in_its_table_and_use_names_the_instances_in_use(
    campaign_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``[plugins.harness] use`` is the rotation, ``[plugins.runtime] use`` the runtime: unset, the defaults."""
    claude = {'use': ['script', 'claude'], 'claude': {'model': 'c'}}
    cfg = load_campaign_config(campaign_path, plugins={'harness': claude})
    assert cfg.harnesses == ['script', 'claude'] and cfg.model_dump(mode='json')['plugins']['harness']['use'] == [
        'script',
        'claude',
    ]
    assert CampaignConfig.model_validate(cfg.model_dump(mode='json')) == cfg, 'use round-trips through the row'
    primary = {'name': 'a', 'direction': 'higher', 'role': 'primary'}
    models = {'harness': {'claude': {'model': 'c'}, 'codex': {'model': 'x'}}}
    bare = CampaignConfig.model_validate({'name': 'b', 'evaluate': ['true'], 'metrics': [primary], 'plugins': models})
    assert (bare.harnesses, bare.runtime) == (['claude', 'codex'], 'subprocess'), 'the providers that are on by default'
    monkeypatch.setenv('PILOOP_CAMPAIGN_PLUGINS__HARNESS__USE', '["script"]')
    assert load_campaign_config(campaign_path).harnesses == ['script'], 'a variable holds a JSON list'


def test_an_instance_with_a_table_of_its_own_that_no_name_puts_in_use_is_refused(
    campaign_path: Path,
) -> None:
    """A table that nothing uses is a mistake, such as a source left out of ``use``: piloop says where to add it."""
    lessons = {'provider': 'lessons', 'budget': 800}
    with pytest.raises(ConfigError, match=re.escape('[plugins.section.short] is configured, and nothing uses it: add')):
        load_campaign_config(campaign_path, plugins={'section': {'short': lessons}})
    with pytest.raises(ConfigError, match=re.escape('add it to [plugins.section] use')):
        load_campaign_config(campaign_path, plugins={'section': {'short': lessons}})
    used = load_campaign_config(
        campaign_path,
        plugins={'section': {'short': lessons}, 'role': {'orchestrator': {'sections': ['context', 'short', 'goal']}}},
    )
    assert [type(s).__name__ for s in used.sections_of(RoleName('orchestrator'))] == ['Context', 'Lessons', 'Goal'], (
        'a role names it'
    )
    own = [s for s in used.sections_of(IMPLEMENTER) if isinstance(s, Lessons)]
    assert own == [Lessons()], "the implementer's lessons are the built-in one"
    with pytest.raises(
        ConfigError, match=re.escape('[plugins.harness.codex] is configured, and nothing uses it: add it')
    ):
        load_campaign_config(campaign_path, plugins={'harness': {'codex': {'model': 'x'}}})
    keyed = {'harness': {'codex': {'model': 'x'}}, 'role': {'orchestrator': {'model': {'codex': 'y'}}}}
    with pytest.raises(ConfigError, match=re.escape('add it to [plugins.harness] use')):
        load_campaign_config(campaign_path, plugins=keyed)  # a key of a role's model puts no harness in use
    rotated = {'harness': {'codex': {'model': 'x'}}, 'role': {'orchestrator': {'harnesses': ['codex']}}}
    assert load_campaign_config(campaign_path, plugins=rotated).role_harnesses(RoleName('orchestrator')) == ['codex']
    assert load_campaign_config(campaign_path, plugins={'section': {'goal': {}}}).status_sections()[1] == Goal(), (
        'an empty table changes nothing, and configures nothing'
    )


HOME_DEFAULTS = """
[plugins.fact_source]
use = ["campaign", "optimization", "models"]

[plugins.fact_source.models]
prices = { a = { input = 1.0, output = 2.0, cache_read = 0.1, cache_write = 1.25 } }

[plugins.fact_source.billing]
provider = "command"
command = ["true"]
metrics = ["gpu_usd"]

[plugins.stop_rule]
max_runs = 7
plateau = 3

[plugins.executor.pools.serve]
usd_per_hour = 0.5
"""


def test_config_toml_holds_defaults_of_each_campaign_s_plugins_which_piloop_toml_and_the_environment_override(
    campaign_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The defaults of piloop, then the home's ``config.toml``, then ``piloop.toml``, then ``PILOOP_CAMPAIGN_*``.

    A table merges key by key, and any other value replaces. A default that the campaign does not use is no error.
    """
    home = tmp_path / 'home'
    home.mkdir()
    (home / 'config.toml').write_text(HOME_DEFAULTS)
    toml = campaign_path / 'piloop.toml'
    toml.write_text(
        toml.read_text() + '\n[plugins.stop_rule]\nplateau = 5\n\n[plugins.fact_source.models.prices.b]\n'
        'input = 3.0\noutput = 15.0\ncache_read = 0.3\ncache_write = 3.75\n'
    )
    settings = Settings(home=home)
    monkeypatch.setattr(config_module, 'settings', settings)
    monkeypatch.setenv('PILOOP_CAMPAIGN_PLUGINS__STOP_RULE__MAX_RUNS', '9')
    cfg = load_campaign_config(campaign_path)
    assert FACT_SOURCES.used(cfg.plugins) == ('campaign', 'optimization', 'models')
    models = FACT_SOURCES.instances(cfg.plugins)['models']
    assert isinstance(models, ModelFacts) and sorted(models.prices) == ['a', 'b'], 'a from the home, b from the file'
    rule = STOP_RULES.selected(cfg.plugins)
    assert isinstance(rule, Limits) and (rule.max_runs, rule.plateau) == (9, 5)
    assert 'fact_source' not in settings.providers, "the home's own [plugins] leave the campaigns' defaults out"
    executor = EXECUTORS.selected(settings.providers)
    assert isinstance(executor, Workers) and executor.pools['serve'].usd_per_hour == 0.5


def test_a_campaign_s_finding_grace_is_its_implementer_s_with_a_warning(
    campaign_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    toml = campaign_path / 'piloop.toml'
    toml.write_text(toml.read_text() + '\n[agents]\nfinding_grace = 5\nwrap_up_grace = 7\n')
    with pytest.warns(PiloopDeprecationWarning, match=r'`agents.finding_grace` is deprecated; write `plugins.role'):
        cfg = load_campaign_config(campaign_path)
    assert cfg.roles.doing_runs()[IMPLEMENTER].end_grace == timedelta(seconds=5)
    assert cfg.agents.wrap_up_grace == timedelta(seconds=7), 'wrap_up_grace stays the [agents] of every role'
    monkeypatch.setenv('PILOOP_CAMPAIGN_AGENTS__FINDING_GRACE', 'PT9S')
    with pytest.warns(PiloopDeprecationWarning) as warned:
        cfg = load_campaign_config(campaign_path)
    assert any(str(w.message).startswith('PILOOP_CAMPAIGN_AGENTS__FINDING_GRACE is deprecated; set') for w in warned)
    assert cfg.roles.doing_runs()[IMPLEMENTER].end_grace == timedelta(seconds=9), 'the variable wins'


def test_a_role_s_finding_grace_is_its_end_grace_with_a_warning(campaign_path: Path) -> None:
    """A finding ends no run now: the grace after a worker ends its run is ``end_grace``."""
    toml = campaign_path / 'piloop.toml'
    toml.write_text(
        toml.read_text().replace('[plugins.role.implementer]\n', '[plugins.role.implementer]\nfinding_grace = 5\n')
    )
    lines = toml.read_text().splitlines()
    with pytest.warns(PiloopDeprecationWarning) as warned:
        cfg = load_campaign_config(campaign_path)
    assert [(w.lineno, str(w.message).split('; removed')[0]) for w in warned] == [
        (
            lines.index('finding_grace = 5') + 1,
            '`plugins.role.<name>.finding_grace` is deprecated; write `plugins.role.<name>.end_grace`',
        )
    ]
    assert cfg.roles.doing_runs()[IMPLEMENTER].end_grace == timedelta(seconds=5)


def test_a_scout_table_of_0_5_loads_without_a_scout_and_warns(campaign_path: Path) -> None:
    toml = campaign_path / 'piloop.toml'
    toml.write_text(toml.read_text() + '\n[plugins.role.scout]\ninterval = 3600\nseeds = 2\n')
    with pytest.warns(PiloopDeprecationWarning, match=r'the scout role is removed; delete \[plugins.role.scout\]'):
        cfg = load_campaign_config(campaign_path)
    assert 'scout' not in cfg.roles.all()


def test_the_verdict_keys_of_0_5_load_without_them_and_warn_from_a_file_and_from_a_stored_config(
    campaign_path: Path,
) -> None:
    """A candidate gets no verdict, an evaluation pairs with no bar, and there is no head: piloop drops
    ``[plugins.verdict_rule]``, ``repeats`` and ``follow_head``, and reads the section ``judging`` as ``scoring``."""
    toml = campaign_path / 'piloop.toml'
    metric = '[[metrics]]\nname = "size"\ndirection = "lower"\n'
    implementer = '[plugins.role.implementer]\n'
    lead = f'{LEAD_TRIGGERS}\nsections = ["context", "judging", "goal"]'
    text = toml.read_text().replace(metric, f'{metric}repeats = 3\n').replace(LEAD_TRIGGERS, lead)
    text = text.replace(implementer, f'{implementer}follow_head = "message"\n')
    toml.write_text(f'{text}\n[plugins.verdict_rule]\nprovider = "bootstrap"\nconfidence = 0.9\n')
    with warnings.catch_warnings(record=True) as warned:
        warnings.simplefilter('always')
        cfg = load_campaign_config(campaign_path)
    assert sorted(str(w.message).split('; removed')[0] for w in warned if w.category is PiloopDeprecationWarning) == [
        'a candidate gets no verdict now, so [plugins.verdict_rule] is removed',
        "an evaluation runs next to no bar now: delete repeats of metric 'size'",
        'the section judging is scoring now: rename it in [plugins.role.orchestrator] sections',
        'there is no head to follow now: delete follow_head of [plugins.role.implementer]',
    ]
    assert 'verdict_rule' not in cfg.plugins and best_rule(cfg) == Baseline()
    assert [type(s).__name__ for s in cfg.sections_of(LEAD)] == ['Context', 'Scoring', 'Goal']
    stored = bare_config('p').model_dump(mode='json')
    stored['plugins']['verdict_rule'] = {'provider': 'bootstrap'}
    stored['metrics'][0]['repeats'] = None
    with pytest.warns(PiloopDeprecationWarning, match='verdict_rule'):
        migrated = CampaignConfig.model_validate(stored)
    assert migrated == bare_config('p'), 'a stored config of 0.5 loads, as 0078 pinned it'
    with pytest.raises(ValidationError):  # what is no table is left to the validation
        CampaignConfig.model_validate('not a table')


def test_a_stored_config_of_0_5_with_the_judging_section_table_loads_without_it() -> None:
    """0.5 dumped every section's table, ``judging`` among them: its empty table goes without a word, and one that
    sets a key goes with a warning."""
    stored = bare_config('p').model_dump(mode='json')
    stored['plugins']['section'] = {**stored['plugins'].get('section', {}), 'judging': {}}
    assert CampaignConfig.model_validate(stored) == bare_config('p')
    stored['plugins']['section']['judging'] = {'budget': 3}
    with pytest.warns(
        PiloopDeprecationWarning, match=r'the section judging is removed; delete \[plugins.section.judging\]'
    ):
        assert CampaignConfig.model_validate(stored) == bare_config('p')


def test_a_research_table_of_0_6_loads_without_research_and_warns_once(campaign_path: Path) -> None:
    toml = campaign_path / 'piloop.toml'
    toml.write_text(toml.read_text() + '\n[plugins.role.research]\nmax_ideas = 3\n')
    with pytest.warns(
        PiloopDeprecationWarning, match=r'the research role is removed; delete \[plugins.role.research\]'
    ) as warned:
        cfg = load_campaign_config(campaign_path)
    assert len(warned) == 1 and 'research' not in cfg.roles.all()


SWEEP_VOCABULARY_OF_0_6 = [
    'the cron trigger is removed: delete cron from [plugins.role.orchestrator] triggers',
    'the cron trigger is removed: delete late from [plugins.role.orchestrator] triggers',
    'the cron trigger is removed: delete the table [plugins.trigger.late]',
    'the dead_at_startup trigger is removed: delete dead_at_startup from [plugins.role.orchestrator] triggers',
    'the low_yield trigger is removed: delete the table [plugins.trigger.low_yield]',
    'the stopped trigger is removed: delete stopped from [plugins.role.orchestrator] triggers',
    'the timer and the incident and routine prompts are removed: delete [plugins.role.orchestrator] incident_prompt',
    'the timer and the incident and routine prompts are removed: delete [plugins.role.orchestrator] interval',
    'the timer and the incident and routine prompts are removed: delete [plugins.role.orchestrator] routine_prompt',
    'there is no closing review now: delete [plugins.stop_rule] while_held',
]
"""Each warning of a campaign that names every key of a sweep's timer, closing review and incident prompts."""
LEAD_KEYS = (
    'triggers = ["run_failed", "cron", "late", "dead_at_startup", "stopped"]\ninterval = 600\n'
    'incident_prompt = "incident.md"\nroutine_prompt = "routine.md"'
)
"""The removed keys of the orchestrator's table, for ``LEAD_TRIGGERS`` in the tests' ``piloop.toml``."""
SWEEP_TABLES = """
[plugins.trigger.late]
provider = "cron"

[plugins.trigger.low_yield]
window = 10

[plugins.stop_rule]
while_held = "skip"
max_runs = 9
"""


def test_the_timer_closing_review_and_incident_prompts_of_0_6_load_without_them_and_warn(campaign_path: Path) -> None:
    toml = campaign_path / 'piloop.toml'
    toml.write_text(toml.read_text().replace(LEAD_TRIGGERS, LEAD_KEYS) + SWEEP_TABLES)
    with pytest.warns(PiloopDeprecationWarning) as warned:
        cfg = load_campaign_config(campaign_path)
    assert sorted(str(w.message).partition('; removed')[0] for w in warned) == SWEEP_VOCABULARY_OF_0_6
    assert role_of(cfg, OrchestratorRole).triggers == ['run_failed']
    assert STOP_RULES.selected(cfg.plugins) == Limits(max_runs=9)


def test_a_campaign_that_0_6_stored_with_a_timer_and_a_closing_review_loads_without_them_and_warns(
    campaign_path: Path,
) -> None:
    """The stored copy of ``piloop.toml`` (``campaigns.config``) goes the same validation as the file: no migration."""
    stored = load_campaign_config(campaign_path).model_dump(mode='json')
    tables = tomllib.loads(SWEEP_TABLES)['plugins']
    stored['plugins']['role']['orchestrator'] |= tomllib.loads(LEAD_KEYS)
    stored['plugins']['trigger'] = {**stored['plugins'].get('trigger', {}), **tables['trigger']}
    stored['plugins']['stop_rule'] = tables['stop_rule']
    with pytest.warns(PiloopDeprecationWarning) as warned:
        cfg = CampaignConfig.model_validate(stored)
    assert sorted(str(w.message).partition('; removed')[0] for w in warned) == SWEEP_VOCABULARY_OF_0_6
    assert role_of(cfg, OrchestratorRole).triggers == ['run_failed']


def test_a_watchdog_table_of_0_6_loads_without_a_watchdog_and_warns_once(campaign_path: Path) -> None:
    toml = campaign_path / 'piloop.toml'
    toml.write_text(toml.read_text() + '\n[plugins.role.watchdog]\ninterval = 600\ntriggers = ["cron", "stopped"]\n')
    with pytest.warns(
        PiloopDeprecationWarning, match=r'the watchdog role is removed; delete \[plugins.role.watchdog\]'
    ) as warned:
        cfg = load_campaign_config(campaign_path)
    assert len(warned) == 1 and 'watchdog' not in cfg.roles.all()


@pytest.mark.parametrize('gone', ['idle_queue', 'yields'])
def test_a_section_that_0_6_removed_loads_without_it_and_warns(campaign_path: Path, gone: str) -> None:
    toml = campaign_path / 'piloop.toml'
    sections = f'[plugins.section]\nuse = ["headline", "{gone}", "runs"]\n\n[plugins.section.{gone}]\n'
    implementer = f'[plugins.role.implementer]\nsections = ["context", "{gone}", "history"]\n'
    toml.write_text(toml.read_text().replace('[plugins.role.implementer]\n', implementer) + f'\n{sections}')
    with pytest.warns(PiloopDeprecationWarning, match=f'the {gone} section is removed') as warned:
        cfg = load_campaign_config(campaign_path)
    assert len(warned) == 3, 'the use list, the table and the role list'
    assert SECTIONS.used(cfg.plugins) == ('headline', 'runs')
    assert cfg.roles.doing_runs()[IMPLEMENTER].sections == ['context', 'history']


def test_a_campaign_that_0_6_stored_with_a_watchdog_and_the_idle_queue_loads_and_warns(campaign_path: Path) -> None:
    """The stored copy of ``piloop.toml`` (``campaigns.config``) goes the same validation as the file: no migration."""
    stored = load_campaign_config(campaign_path).model_dump(mode='json')
    stored['plugins']['role']['watchdog'] = {'triggers': ['run_failed', 'cron', 'stopped'], 'interval': 1800}
    stored['plugins']['section']['use'] = [*stored['plugins']['section']['use'], 'idle_queue']
    stored['plugins']['section']['idle_queue'] = {}
    with pytest.warns(PiloopDeprecationWarning) as warned:
        cfg = CampaignConfig.model_validate(stored)
    assert sorted(str(w.message).split(':')[0].split(';')[0] for w in warned) == [
        'the idle_queue section is removed',
        'the idle_queue section is removed',
        'the watchdog role is removed',
    ]
    assert 'watchdog' not in cfg.roles.all() and 'idle_queue' not in SECTIONS.used(cfg.plugins)


def test_a_campaign_that_0_5_stored_with_the_yields_section_loads_without_it() -> None:
    """0.5 dumped every section's table and listed ``yields`` in ``use``: #670 deleted the section."""
    stored = bare_config('p').model_dump(mode='json')
    section = stored['plugins'].setdefault('section', {})
    section['use'], section['yields'] = [*SECTIONS.used(bare_config('p').plugins), 'yields'], {}
    with pytest.warns(PiloopDeprecationWarning, match='the yields section is removed'):
        assert 'yields' not in SECTIONS.used(CampaignConfig.model_validate(stored).plugins)


def test_the_low_water_and_stalled_triggers_of_0_6_load_without_them_and_warn(campaign_path: Path) -> None:
    toml = campaign_path / 'piloop.toml'
    lead = 'triggers = ["run_failed", "stalled", "wet"]'
    text = toml.read_text().replace(LEAD_TRIGGERS, lead)
    toml.write_text(f'{text}\n[plugins.trigger.wet]\nprovider = "low_water"\nbuffer = 1\n')
    with pytest.warns(PiloopDeprecationWarning, match='trigger is removed with the research role') as warned:
        cfg = load_campaign_config(campaign_path)
    assert sorted(str(w.message).split(':')[0] for w in warned) == [
        'the low_water trigger is removed with the research role',
        'the low_water trigger is removed with the research role',
        'the stalled trigger is removed with the research role',
    ], 'the wet table, its name in the list, and stalled in the list'
    assert role_of(cfg, OrchestratorRole).triggers == ['run_failed']


def test_a_campaign_that_0_6_stored_with_research_loads_without_it_and_warns(campaign_path: Path) -> None:
    """The stored copy of ``piloop.toml`` (``campaigns.config``) goes the same validation as the file: no migration."""
    stored = load_campaign_config(campaign_path).model_dump(mode='json')
    stored['plugins']['role']['research'] = {'triggers': ['low_water', 'stalled'], 'max_ideas': 3}
    stored['plugins']['role']['orchestrator']['triggers'] = ['run_failed', 'stalled']
    stored['plugins']['trigger'] = {'low_water': {'buffer': 2}}
    with pytest.warns(PiloopDeprecationWarning) as warned:
        cfg = CampaignConfig.model_validate(stored)
    assert sorted(str(w.message).split(':')[0].split(';')[0] for w in warned) == [
        'the low_water trigger is removed with the research role',
        'the research role is removed',
        'the stalled trigger is removed with the research role',
    ]
    assert 'research' not in cfg.roles.all() and role_of(cfg, OrchestratorRole).triggers == ['run_failed']


def test_the_leaking_funnel_of_0_5_loads_without_the_trigger_and_warns(campaign_path: Path) -> None:
    toml = campaign_path / 'piloop.toml'
    lead = 'triggers = ["run_failed", "leaking_funnel", "leaks"]'
    text = toml.read_text().replace(LEAD_TRIGGERS, lead)
    tables = '[plugins.trigger.leaks]\nprovider = "leaking_funnel"\n\n[plugins.trigger.leaking_funnel]\nwindow = 3\n'
    toml.write_text(f'{text}\n{tables}')
    lines = toml.read_text().splitlines()
    with pytest.warns(PiloopDeprecationWarning, match='the leaking_funnel trigger is removed') as warned:
        cfg = load_campaign_config(campaign_path)
    assert sorted(w.lineno for w in warned) == [
        lines.index(lead) + 1,
        lines.index('[plugins.trigger.leaks]') + 1,
        lines.index('[plugins.trigger.leaking_funnel]') + 1,
    ], 'the line of each'
    assert role_of(cfg, OrchestratorRole).triggers == ['run_failed']


def test_the_keys_that_assigned_ideas_of_0_5_load_without_them_and_warn(campaign_path: Path) -> None:
    toml = campaign_path / 'piloop.toml'
    keys = 'takes = "priority < 3"\nexplore_every = 4\nrecovery_requeues = 1\n'
    toml.write_text(toml.read_text().replace('[plugins.role.implementer]\n', f'[plugins.role.implementer]\n{keys}'))
    lines = toml.read_text().splitlines()
    with pytest.warns(PiloopDeprecationWarning, match='workers claim their ideas now') as warned:
        cfg = load_campaign_config(campaign_path)
    assert sorted(w.lineno for w in warned) == sorted(
        lines.index(line) + 1 for line in ('takes = "priority < 3"', 'explore_every = 4', 'recovery_requeues = 1')
    ), 'the line of each'
    assert IMPLEMENTER in cfg.roles.doing_runs()


def test_a_track_share_of_0_5_loads_without_it_and_warns(campaign_path: Path) -> None:
    """``share`` is removed: workers claim any open idea. A table that it leaves empty goes too."""
    toml = campaign_path / 'piloop.toml'
    text = toml.read_text()
    toml.write_text(f'{text}\n[tracks.score]\nshare = 1.0\n')
    lines = toml.read_text().splitlines()
    with pytest.warns(PiloopDeprecationWarning, match=r'\[tracks\] share is removed') as warned:
        cfg = load_campaign_config(campaign_path)
    assert [w.lineno for w in warned] == [lines.index('share = 1.0') + 1], 'at its line'
    assert cfg.tracks == {}, 'the table that it leaves empty goes'
    toml.write_text(f'{text}\n[tracks.score]\nshare = 1.0\naddendum = "brief.md"\n')
    with pytest.warns(PiloopDeprecationWarning, match=r'\[tracks\] share is removed'):
        cfg = load_campaign_config(campaign_path)
    assert cfg.tracks == {TrackName('score'): TrackConfig(addendum='brief.md')}, 'a table with more keeps the rest'


def test_the_seed_trigger_of_0_5_loads_without_the_trigger_and_warns(campaign_path: Path) -> None:
    toml = campaign_path / 'piloop.toml'
    lead = 'triggers = ["run_failed", "seed"]'
    text = toml.read_text().replace(LEAD_TRIGGERS, lead)
    toml.write_text(f'{text}\n[plugins.trigger.seed]\n')
    lines = toml.read_text().splitlines()
    with pytest.warns(PiloopDeprecationWarning, match='the seed trigger is removed') as warned:
        cfg = load_campaign_config(campaign_path)
    assert sorted(w.lineno for w in warned) == [lines.index(lead) + 1, lines.index('[plugins.trigger.seed]') + 1]
    assert role_of(cfg, OrchestratorRole).triggers == ['run_failed']


def test_the_seeds_and_reviews_of_0_5_load_without_their_keys_and_warn(campaign_path: Path) -> None:
    """``idle_alert`` and the pack's ``seeds`` go: nothing is a seed."""
    toml = campaign_path / 'piloop.toml'
    text = toml.read_text().replace('[plugins.role.implementer]\n', '[plugins.role.implementer]\nidle_alert = 60\n')
    toml.write_text(f'{text}\n[plugins.section]\nuse = ["pack"]\n\n[plugins.section.pack]\nseeds = 5\nideas = 7\n')
    lines = toml.read_text().splitlines()
    with pytest.warns(PiloopDeprecationWarning) as warned:
        cfg = load_campaign_config(campaign_path)
    assert sorted((w.lineno, str(w.message).partition(';')[0]) for w in warned) == sorted(
        [
            (lines.index('idle_alert = 60') + 1, 'an idea waits for no review now, so idle_alert is removed'),
            (lines.index('seeds = 5') + 1, 'the research pack lists no seeds now'),
        ]
    )
    assert SECTIONS.instances(cfg.plugins)['pack'] == Pack(ideas=7)


def test_files_and_plan_words_of_a_role_that_does_runs_of_0_5_load_without_them_and_warn(campaign_path: Path) -> None:
    toml = campaign_path / 'piloop.toml'
    text = toml.read_text().replace('[plugins.role.implementer]\n', '[plugins.role.implementer]\nfiles = "ideas"\n')
    toml.write_text(text + '\n[plugins.role.quick]\nruns = true\nplan_words = 80\n')
    lines = toml.read_text().splitlines()
    with pytest.warns(PiloopDeprecationWarning, match='of a role that does runs is removed') as warned:
        cfg = load_campaign_config(campaign_path)
    assert sorted(w.lineno for w in warned) == [
        lines.index('files = "ideas"') + 1,
        lines.index('plan_words = 80') + 1,
    ], 'the line of each'
    assert ['files' in r.model_dump() for r in cfg.roles.doing_runs().values()] == [False] * 2
