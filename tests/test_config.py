import json
import re
import subprocess
from collections.abc import Mapping
from datetime import timedelta
from pathlib import Path
from unittest.mock import ANY

import pytest
from inline_snapshot import snapshot

from piloop import config as config_module
from piloop.agents.roles import filed
from piloop.config import CampaignConfig, ConfigError, ConfigMissing, load_campaign_config
from piloop.deprecated import PiloopDeprecationWarning
from piloop.ids import RoleName
from piloop.plugins.campaign_type.kit.backlog.role.research import ResearchRole
from piloop.plugins.campaign_type.kit.backlog.trigger.low_water import LowWaterTrigger
from piloop.plugins.campaign_type.kit.evaluation.config import Metric, MetricRole, TrackConfig
from piloop.plugins.campaign_type.kit.evaluation.ids import TrackName
from piloop.plugins.campaign_type.kit.runs.ids import IMPLEMENTER
from piloop.plugins.campaign_type.kit.runs.role.watchdog import WatchdogRole
from piloop.plugins.campaign_type.kit.runs.trigger.stalled import StalledTrigger
from piloop.plugins.campaign_type.kit.verdicts.judge import criterion
from piloop.plugins.campaign_type.optimization.section.pack import Pack
from piloop.plugins.executor.contract import EXECUTORS
from piloop.plugins.executor.workers import Workers
from piloop.plugins.fact_source.contract import FACT_SOURCES
from piloop.plugins.fact_source.models import ModelFacts
from piloop.plugins.harness.contract import Harness, each_provider
from piloop.plugins.metric import Direction
from piloop.plugins.role.contract import Files, ImplementerRole
from piloop.plugins.role.orchestrator import OrchestratorRole
from piloop.plugins.section.contract import SECTIONS
from piloop.plugins.section.goal import Goal
from piloop.plugins.section.lessons import Lessons
from piloop.plugins.stop_rule.contract import STOP_RULES, StopRule
from piloop.plugins.stop_rule.limits import Limits
from piloop.plugins.trigger.sql import SqlTrigger
from piloop.settings import Settings
from tests.conftest import AGENT_TIMEOUT, RESEARCH, WATCHDOG, role_of


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
        RoleName('orchestrator'): OrchestratorRole(),
        RESEARCH: ResearchRole(timeout=AGENT_TIMEOUT, max_ideas=0, max_concurrent=1),
        WATCHDOG: WatchdogRole(harnesses=['script'], model={'script': 'watchdog-model'}, timeout=AGENT_TIMEOUT),
    }, 'every role, in the order of its entry point, with its table over its defaults'
    assert cfg.role(RESEARCH) is role_of(cfg, ResearchRole)
    assert cfg.role_harnesses(RESEARCH) == ['script'], 'unset: the campaign rotation'
    assert cfg.role_models(WATCHDOG) == {'script': 'watchdog-model'}, "role map over each harness's model"
    assert cfg.role_models(RESEARCH) == {'script': 'test-model', 'claude': 'claude-opus-5-5', 'codex': 'gpt-6.1-sol'}
    assert CampaignConfig.model_validate(cfg.model_dump(mode='json')) == cfg, 'round-trips through the campaign row'
    dumped = cfg.model_dump(mode='json')['plugins']
    assert dumped['role']['orchestrator'] == {}, 'a role without a table is stored by its name alone'
    assert dumped['harness']['script'] == {'model': 'test-model', 'executable': ANY, 'headless': ['{prompt}']}
    defaults = CampaignConfig(
        name='bare',
        metrics=[Metric(name='a', direction=Direction.HIGHER, role=MetricRole.PRIMARY)],
        evaluate=['true'],
        plugins={'harness': {'pi': {'model': 'm'}}},
    )
    assert defaults.context == [], 'no analysis artifacts unless the campaign names them'
    implementer, watchdog = role_of(defaults, ImplementerRole), role_of(defaults, WatchdogRole)
    research = role_of(defaults, ResearchRole)
    assert (implementer.timeout, implementer.max_concurrent) == (1800, 4)
    assert (watchdog.harnesses, watchdog.model) == (None, None), 'the campaign rotation and models'
    assert (watchdog.timeout, watchdog.interval) == (600, 1800)
    assert (research.timeout, research.max_ideas) == (900, 3), 'a sweep defines a ranked few ideas'
    assert research.triggers == ['low_water', 'stalled']
    assert defaults.triggers_of(RESEARCH) == [LowWaterTrigger(buffer=2), StalledTrigger()]
    assert research.max_concurrent == 1, 'concurrent sweeps duplicate each other and race on seeds'


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
        load_campaign_config(campaign_path, plugins={'role': {'research': {'addendum': '../elsewhere.md'}}})
    with pytest.raises(ValueError, match='at least one metric'):
        load_campaign_config(campaign_path, metrics=[{'name': 'a', 'direction': 'higher'}])
    with pytest.raises(ValueError, match='plugins.role.janitor'):
        load_campaign_config(campaign_path, plugins={'role': {'janitor': {}}})
    with pytest.raises(ValueError, match='plugins.role.implementer.max_ideas'):
        load_campaign_config(campaign_path, plugins={'role': {'implementer': {'max_ideas': 3}}})
    with pytest.raises(ValueError, match='plugins.role.research.min_ready'):
        load_campaign_config(campaign_path, plugins={'role': {'research': {'min_ready': 3}}})
    with pytest.raises(ValueError, match=re.escape("plugins.role.research.harnesses[0] = 'nope': no [plugins.harness")):
        load_campaign_config(campaign_path, plugins={'role': {'research': {'harnesses': ['nope']}}})
    with pytest.raises(ValueError, match=re.escape("plugins.role.research.model = 'nope': no [plugins.harness.nope]")):
        load_campaign_config(campaign_path, plugins={'role': {'research': {'model': {'nope': 'm'}}}})
    with pytest.raises(ValueError, match=re.escape("plugins.role.research: no model for the harnesses ['pi']")):
        load_campaign_config(campaign_path, plugins={'role': {'research': {'harnesses': ['pi']}}})
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
    assert criterion(relative.guardrails[0]).allowed(40) == 4.0, "a fraction of the bar, in the metric's units"
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
    with pytest.raises(ValueError, match='plugins.verdict_rule.confidence'):
        load_campaign_config(campaign_path, plugins={'verdict_rule': {'confidence': 1}})


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
        "the stop rule 'limits' limits the head, which needs a campaign of one track": {
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
    ), 'tracks, margins and floors are verdict policy: they re-measure nothing'


def test_the_evaluator_is_what_scores_the_judging_metrics_not_how_the_verdict_reads_them(campaign_path: Path) -> None:
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
        'a verdict rule': key(primary, size, plugins={'verdict_rule': {'confidence': 0.9}}),
        'a new watch metric': key(primary, size, {'name': 'eval_cost_usd', 'direction': 'lower'}),
        'no ladder': key(primary, size, ladder=[]),
    }
    assert unchanged == dict.fromkeys(unchanged, cfg.evaluator_key), 'none changes a verdict already given'
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
    assert len({cfg.evaluator_key, *changed.values()}) == len(changed) + 1, 'each can change a verdict'
    assert (
        load_campaign_config(campaign_path).evaluator_key
        == load_campaign_config(campaign_path, metrics=[primary]).evaluator_key
    ), 'a watch metric is no part of the evaluator'


def test_repeats_marks_a_noisy_metric_and_a_track_pairs_with_the_largest_repeats_of_its_deciding_metrics(
    campaign_path: Path,
) -> None:
    timing = {'name': 'seconds', 'direction': 'lower', 'role': 'guardrail', 'margin': 1, 'repeats': 3}
    cpu = {'name': 'cpu', 'direction': 'lower', 'role': 'guardrail', 'margin': 1, 'repeats': 5}
    primary = {'name': 'score', 'direction': 'higher', 'role': 'primary'}
    assert load_campaign_config(campaign_path).all_tracks[0].repeats is None, 'no noisy metric: one sample'
    noisy = load_campaign_config(campaign_path, metrics=[primary, timing, cpu])
    assert noisy.all_tracks[0].repeats == 5
    assert (
        noisy.evaluator_key
        == load_campaign_config(
            campaign_path, metrics=[primary, {**timing, 'repeats': None}, {**cpu, 'repeats': None}]
        ).evaluator_key
    ), 'repeats is verdict policy: it re-measures nothing'
    serving = load_campaign_config(campaign_path, metrics=[ACCURACY, {**COST, 'repeats': 3}], tracks=ADDENDA)
    assert [t.repeats for t in serving.all_tracks] == [3, 3], 'the primary of another track is a guardrail'
    with pytest.raises(ValueError, match="'size': repeats applies to primaries and guardrails only"):
        load_campaign_config(campaign_path, metrics=[primary, {'name': 'size', 'direction': 'lower', 'repeats': 3}])
    with pytest.raises(ValueError, match=r'metrics\[1\]\.repeats = 0'):
        load_campaign_config(campaign_path, metrics=[primary, {**timing, 'repeats': 0}])


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


def test_a_table_with_a_new_name_defines_a_role_that_needs_a_prompt_file_and_triggers(campaign_path: Path) -> None:
    (campaign_path / 'reviewer.md').write_text('# Review\n')
    when = 'SELECT 1 FROM ideas WHERE campaign_id = :campaign AND created_at > :since'
    good = {'prompt': 'reviewer.md', 'triggers': ['new_idea'], 'runtime': 'subprocess'}
    new_idea: dict[str, object] = {'new_idea': {'provider': 'sql', 'when': when}}
    cfg = load_campaign_config(campaign_path, plugins={'role': {'reviewer': good}, 'trigger': new_idea})
    assert list(cfg.roles.all()) == ['implementer', 'orchestrator', 'research', 'watchdog', 'reviewer']
    assert list(cfg.roles.sweeping()) == ['orchestrator', 'research', 'watchdog', 'reviewer'], (
        'the implementer does runs, not sweeps'
    )
    assert cfg.role_runtime(RoleName('reviewer')) == 'subprocess'
    assert cfg.triggers_of('reviewer') == [SqlTrigger(when=when)] and cfg.triggers_of(IMPLEMENTER) == []
    assert CampaignConfig.model_validate(cfg.model_dump(mode='json')) == cfg, 'round-trips through the campaign row'

    def fails(roles: dict[str, object], triggers: dict[str, object] = new_idea) -> str:
        with pytest.raises(ConfigError) as e:
            load_campaign_config(campaign_path, plugins={'role': roles, 'trigger': triggers})
        return str(e.value).split('\n', 1)[1]

    def reviewer(**keys: object) -> dict[str, object]:
        return {'reviewer': {**good, **keys}}

    assert fails({'reviewer': {'triggers': []}}, {}) == snapshot(
        '  plugins.role.reviewer: a new role needs a prompt: the file that starts its brief. Fix:'
        ' https://piloop.autoresearch.inc/reference/configuration describes the key'
    )
    assert fails({'reviewer': {'prompt': 'reviewer.md'}}, {}) == snapshot(
        '  plugins.role.reviewer.triggers: Field required. Fix: add the key; https://piloop.autoresearch.inc/reference/configuration says what it takes'
    )
    assert "plugins.role.reviewer.prompt = 'missing.md': no such file" in fails(reviewer(prompt='missing.md'))
    assert "plugins.role.reviewer.triggers[0] = 'nope': no [plugins.trigger.nope]; there are" in fails(
        reviewer(triggers=['nope'])
    )
    assert 'plugins.trigger.timer.every = 3: Extra inputs are not permitted' in fails(
        reviewer(triggers=['timer']), {'timer': {'provider': 'cron', 'every': 3}}
    )
    assert '[plugins.trigger.new_idea] is configured, and nothing uses it: add it to a list that names it' in fails(
        reviewer(triggers=['stalled'])
    )
    filler = load_campaign_config(
        campaign_path,
        plugins={'role': reviewer(triggers=['low_water'], files='ideas'), 'trigger': {'low_water': {'buffer': 1}}},
    )
    assert cfg.roles.sweeping()[RoleName('reviewer')].max_ideas == 1, 'any sweeping role can fill the backlog'
    assert filler.triggers_of('reviewer') == [LowWaterTrigger(buffer=1)]
    for query, error in {
        'DELETE FROM ideas': 'not authorized',
        'SELECT nope FROM ideas': 'no such column: nope',
        'SELECT 1 FROM ideas WHERE id = :idea': 'You did not supply a value for binding parameter :idea',
        'SELECT 1; SELECT 2': 'You can only execute one statement at a time',
    }.items():
        assert f'the query does not run on the piloop database: {error}' in fails(
            reviewer(), {'new_idea': {'provider': 'sql', 'when': query}}
        ), query
    for name in ('run', 'Review Bot'):
        assert f'{name!r} is not a role name: use lowercase letters, digits, _ and -' in fails({name: good})


def test_a_table_with_runs_defines_a_role_that_does_runs_with_the_implementer_defaults(campaign_path: Path) -> None:
    quick = {'runs': True, 'timeout': 60, 'max_concurrent': 1}
    cfg = load_campaign_config(campaign_path, plugins={'role': {'quick': quick}})
    assert list(cfg.roles.all()) == ['implementer', 'orchestrator', 'research', 'watchdog', 'quick']
    assert list(cfg.roles.doing_runs()) == ['quick', 'implementer'], 'the roles of the file fill their slots first'
    assert list(cfg.roles.sweeping()) == ['orchestrator', 'research', 'watchdog']
    role = cfg.roles.doing_runs()[RoleName('quick')]
    assert (role.prompt, role.PROMPT, filed(role), role.timeout) == (None, ImplementerRole.PROMPT, Files.NOTHING, 60)
    assert cfg.run_slots == 1 + cfg.roles.doing_runs()[IMPLEMENTER].max_concurrent
    assert CampaignConfig.model_validate(cfg.model_dump(mode='json')) == cfg, 'round-trips through the campaign row'

    def fails(roles: dict[str, object]) -> str:
        with pytest.raises(ConfigError) as e:
            load_campaign_config(campaign_path, plugins={'role': roles})
        return str(e.value).split('\n', 1)[1]

    assert 'plugins.role.quick.triggers: Extra inputs are not permitted' in fails(
        {'quick': {'runs': True, 'triggers': []}}
    )
    assert 'plugins.role.watchdog.runs = True: Input should be False' in fails({'watchdog': {'runs': True}})
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
    """``[plugins.verdict_rule.strict]`` reads as a key ``strict`` of the one verdict rule: the error says what to write."""
    with pytest.raises(ConfigError) as caught:
        load_campaign_config(campaign_path, plugins={'verdict_rule': {'strict': {'confidence': 0.99}}})
    assert str(caught.value).split('\n', 1)[1] == snapshot(
        '  plugins.verdict_rule: a verdict rule has one instance, not one for each name (strict): write its keys in [plugins.verdict_rule], with provider = "<verdict rule>" to choose one. Fix: https://piloop.autoresearch.inc/reference/configuration describes the key'
    )
    with pytest.raises(ConfigError, match=r"plugins\.verdict_rule\.confidence = 'high'"):
        load_campaign_config(campaign_path, plugins={'verdict_rule': {'confidence': 'high'}})
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
    pi = {'harness': {'pi': {'model': 'm'}}}
    bare = CampaignConfig.model_validate({'name': 'b', 'evaluate': ['true'], 'metrics': [primary], 'plugins': pi})
    assert (bare.harnesses, bare.runtime) == (['pi'], 'subprocess'), 'the providers that are on by default'
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
        plugins={'section': {'short': lessons}, 'role': {'research': {'sections': ['context', 'short', 'goal']}}},
    )
    assert [type(s).__name__ for s in used.sections_of(RESEARCH)] == ['Context', 'Lessons', 'Goal'], 'a role names it'
    watchdogs = [s for s in used.sections_of(WATCHDOG) if isinstance(s, Lessons)]
    assert watchdogs == [Lessons()], "the watchdog's own lessons are the built-in one"
    with pytest.raises(
        ConfigError, match=re.escape('[plugins.harness.codex] is configured, and nothing uses it: add it')
    ):
        load_campaign_config(campaign_path, plugins={'harness': {'codex': {'model': 'x'}}})
    keyed = {'harness': {'codex': {'model': 'x'}}, 'role': {'research': {'model': {'codex': 'y'}}}}
    with pytest.raises(ConfigError, match=re.escape('add it to [plugins.harness] use')):
        load_campaign_config(campaign_path, plugins=keyed)  # a key of a role's model puts no harness in use
    rotated = {'harness': {'codex': {'model': 'x'}}, 'role': {'research': {'harnesses': ['codex']}}}
    assert load_campaign_config(campaign_path, plugins=rotated).role_harnesses(RESEARCH) == ['codex']
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


def test_the_leaking_funnel_of_0_5_loads_without_the_trigger_and_warns(campaign_path: Path) -> None:
    toml = campaign_path / 'piloop.toml'
    watchdog = 'triggers = ["run_failed", "leaking_funnel", "leaks"]'
    text = (
        toml.read_text()
        .replace('[plugins.role.watchdog]\n', f'[plugins.role.watchdog]\n{watchdog}\n')
        .replace('[plugins.role.research]\n', '[plugins.role.research]\ntriggers = ["stalled"]\n')
    )
    tables = '[plugins.trigger.leaks]\nprovider = "leaking_funnel"\n\n[plugins.trigger.leaking_funnel]\nwindow = 3\n'
    toml.write_text(f'{text}\n{tables}')
    lines = toml.read_text().splitlines()
    with pytest.warns(PiloopDeprecationWarning, match='the leaking_funnel trigger is removed') as warned:
        cfg = load_campaign_config(campaign_path)
    assert sorted(w.lineno for w in warned) == [
        lines.index(watchdog) + 1,
        lines.index('[plugins.trigger.leaks]') + 1,
        lines.index('[plugins.trigger.leaking_funnel]') + 1,
    ], 'the line of each'
    assert [type(t).__name__ for t in cfg.triggers_of(WATCHDOG)] == ['RunFailed']
    assert [type(t).__name__ for t in cfg.triggers_of(RESEARCH)] == ['StalledTrigger'], 'its list is left as it is'


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
    research = 'triggers = ["low_water", "seed", "stalled"]'
    text = toml.read_text().replace('[plugins.role.research]\n', f'[plugins.role.research]\n{research}\n')
    toml.write_text(f'{text}\n[plugins.trigger.seed]\n')
    lines = toml.read_text().splitlines()
    with pytest.warns(PiloopDeprecationWarning, match='the seed trigger is removed') as warned:
        cfg = load_campaign_config(campaign_path)
    assert sorted(w.lineno for w in warned) == [lines.index(research) + 1, lines.index('[plugins.trigger.seed]') + 1]
    assert [type(t).__name__ for t in cfg.triggers_of(RESEARCH)] == ['LowWaterTrigger', 'StalledTrigger']


def test_the_seeds_and_reviews_of_0_5_load_without_their_keys_and_warn(campaign_path: Path) -> None:
    """``files = "seeds"`` files nothing now, and ``idle_alert`` and the pack's ``seeds`` go: nothing is a seed."""
    toml = campaign_path / 'piloop.toml'
    text = (
        toml.read_text()
        .replace('[plugins.role.implementer]\n', '[plugins.role.implementer]\nidle_alert = 60\n')
        .replace('[plugins.role.watchdog]\n', '[plugins.role.watchdog]\nfiles = "seeds"\n')
    )
    toml.write_text(f'{text}\n[plugins.section]\nuse = ["pack"]\n\n[plugins.section.pack]\nseeds = 5\nideas = 7\n')
    lines = toml.read_text().splitlines()
    with pytest.warns(PiloopDeprecationWarning) as warned:
        cfg = load_campaign_config(campaign_path)
    assert sorted((w.lineno, str(w.message).partition(';')[0]) for w in warned) == sorted(
        [
            (lines.index('idle_alert = 60') + 1, 'an idea waits for no review now, so idle_alert is removed'),
            (lines.index('files = "seeds"') + 1, 'a seed is an open idea now, so files = "seeds" files nothing'),
            (lines.index('seeds = 5') + 1, 'the research pack lists no seeds now'),
        ]
    )
    assert filed(role_of(cfg, WatchdogRole)) is Files.NOTHING
    assert SECTIONS.instances(cfg.plugins)['pack'] == Pack(ideas=7)


def test_files_and_plan_words_of_a_role_that_does_runs_of_0_5_load_without_them_and_warn(campaign_path: Path) -> None:
    toml = campaign_path / 'piloop.toml'
    text = (
        toml.read_text()
        .replace('[plugins.role.implementer]\n', '[plugins.role.implementer]\nfiles = "ideas"\n')
        .replace('[plugins.role.research]\n', '[plugins.role.research]\nplan_words = 90\n')
    )
    toml.write_text(text + '\n[plugins.role.quick]\nruns = true\nplan_words = 80\n')
    lines = toml.read_text().splitlines()
    with pytest.warns(PiloopDeprecationWarning, match='of a role that does runs is removed') as warned:
        cfg = load_campaign_config(campaign_path)
    assert sorted(w.lineno for w in warned) == [
        lines.index('files = "ideas"') + 1,
        lines.index('plan_words = 80') + 1,
    ], 'the line of each'
    assert [filed(r) for r in cfg.roles.doing_runs().values()] == [Files.NOTHING, Files.NOTHING]
    assert role_of(cfg, ResearchRole).plan_words == 90, "a sweep's stays"
