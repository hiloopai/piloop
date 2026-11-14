import json
import re
import subprocess
from collections.abc import Mapping
from pathlib import Path
from unittest.mock import ANY

import pytest
from inline_snapshot import snapshot

from piloop import config as config_module
from piloop.config import CampaignConfig, ConfigError, ConfigMissing, load_campaign_config
from piloop.ids import RoleName
from piloop.plugins.campaign_type.kit.evaluation.config import Metric, MetricRole
from piloop.plugins.campaign_type.kit.evaluation.ids import TrackName
from piloop.plugins.campaign_type.kit.pool.pool import criterion
from piloop.plugins.campaign_type.kit.runs.ids import IMPLEMENTER
from piloop.plugins.campaign_type.optimization.section.history import History
from piloop.plugins.executor.contract import EXECUTORS
from piloop.plugins.executor.workers import Workers
from piloop.plugins.fact_source.contract import FACT_SOURCES
from piloop.plugins.fact_source.models import ModelFacts
from piloop.plugins.harness.contract import Harness, each_provider
from piloop.plugins.metric import Direction
from piloop.plugins.role.contract import Continuity, ImplementerRole
from piloop.plugins.section.goal import Goal
from piloop.plugins.stop_rule.contract import STOP_RULES, StopRule
from piloop.plugins.stop_rule.limits import Limits
from piloop.settings import Settings
from tests.conftest import (
    AGENT_TIMEOUT,
    AUDITOR,
    role_of,
)
from tests.fixtures.plugin import Auditor


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


@pytest.mark.usefixtures('auditor')
def test_roles_are_typed_per_kind(campaign_path: Path) -> None:
    cfg = load_campaign_config(campaign_path)
    assert cfg.roles.all() == {
        IMPLEMENTER: ImplementerRole(timeout=AGENT_TIMEOUT, max_concurrent=1),
        AUDITOR: Auditor(
            continuity=Continuity.FRESH,
            harnesses=['script'],
            model={'script': 'auditor-model'},
            timeout=AGENT_TIMEOUT,
        ),
    }, 'every role, in the order of its entry point, with its table over its defaults'
    assert cfg.role(AUDITOR) is role_of(cfg, Auditor)
    assert cfg.role_harnesses(IMPLEMENTER) == ['script'], 'unset: the campaign rotation'
    assert cfg.role_models(AUDITOR) == {'script': 'auditor-model'}, "role map over each harness's model"
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
    assert list(defaults.roles.all()) == [IMPLEMENTER, AUDITOR], (
        'the installed roles: no research, watchdog or orchestrator'
    )


def dropped(warned: pytest.WarningsRecorder) -> list[str]:
    return sorted(str(w.message).partition(';')[0] for w in warned)


def said(warned: pytest.WarningsRecorder) -> list[str]:
    """Each warning in full, but the release that removes its key."""
    return sorted(str(w.message).partition('; removed')[0] for w in warned)


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
        load_campaign_config(campaign_path, plugins={'role': {'implementer': {'addendum': '../elsewhere.md'}}})
    with pytest.raises(ValueError, match='at least one metric'):
        load_campaign_config(campaign_path, metrics=[{'name': 'a', 'direction': 'higher'}])
    with pytest.raises(ConfigError, match=re.escape("plugins.role.implementer.prompt = 'gone.md': no such file")):
        load_campaign_config(campaign_path, plugins={'role': {'implementer': {'prompt': 'gone.md'}}})
    with pytest.raises(ValueError, match='plugins.role.janitor.triggers'):
        load_campaign_config(campaign_path, plugins={'role': {'janitor': {'runs': True, 'triggers': []}}})
    with pytest.raises(ValueError, match='plugins.role.implementer.max_ideas'):
        load_campaign_config(campaign_path, plugins={'role': {'implementer': {'max_ideas': 3}}})
    with pytest.raises(ValueError, match='plugins.role.implementer.min_ready'):
        load_campaign_config(campaign_path, plugins={'role': {'implementer': {'min_ready': 3}}})
    with pytest.raises(
        ValueError, match=re.escape("plugins.role.implementer.harnesses[0] = 'nope': no [plugins.harness")
    ):
        load_campaign_config(campaign_path, plugins={'role': {'implementer': {'harnesses': ['nope']}}})
    with pytest.raises(
        ValueError, match=re.escape("plugins.role.implementer.model = 'nope': no [plugins.harness.nope]")
    ):
        load_campaign_config(campaign_path, plugins={'role': {'implementer': {'model': {'nope': 'm'}}}})
    with pytest.raises(ValueError, match=re.escape("plugins.role.implementer: no model for the harnesses ['pi']")):
        load_campaign_config(campaign_path, plugins={'role': {'implementer': {'harnesses': ['pi']}}})
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
        campaign_path, plugins={'harness': {'use': ['script', 'codex'], 'codex': {'model': 'gpt-6', 'config': config}}}
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


@pytest.mark.usefixtures('auditor')
def test_a_table_with_runs_defines_a_role_that_does_runs_with_the_implementer_defaults(campaign_path: Path) -> None:
    quick = {'runs': True, 'timeout': 60, 'max_concurrent': 1}
    cfg = load_campaign_config(campaign_path, plugins={'role': {'quick': quick}})
    assert list(cfg.roles.all()) == ['implementer', 'auditor', 'quick']
    assert list(cfg.roles.doing_runs()) == ['quick', 'implementer'], 'the roles of the file fill their slots first'
    assert list(cfg.roles.starting()) == ['auditor']
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
    assert 'plugins.role.auditor.runs = True: Input should be False' in fails({'auditor': {'runs': True}})
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


@pytest.mark.usefixtures('auditor')
def test_an_instance_with_a_table_of_its_own_that_no_name_puts_in_use_is_refused(
    campaign_path: Path,
) -> None:
    """A table that nothing uses is a mistake, such as a source left out of ``use``: piloop says where to add it."""
    short = {'provider': 'history', 'budget': 800}
    with pytest.raises(ConfigError, match=re.escape('[plugins.section.short] is configured, and nothing uses it: add')):
        load_campaign_config(campaign_path, plugins={'section': {'short': short}})
    with pytest.raises(ConfigError, match=re.escape('add it to [plugins.section] use')):
        load_campaign_config(campaign_path, plugins={'section': {'short': short}})
    used = load_campaign_config(
        campaign_path,
        plugins={'section': {'short': short}, 'role': {'auditor': {'sections': ['context', 'short', 'goal']}}},
    )
    assert [type(s).__name__ for s in used.sections_of(AUDITOR)] == ['Context', 'History', 'Goal'], 'a role names it'
    own = [s for s in used.sections_of(IMPLEMENTER) if isinstance(s, History)]
    assert own == [History()], "the implementer's history is the built-in one"
    with pytest.raises(
        ConfigError, match=re.escape('[plugins.harness.codex] is configured, and nothing uses it: add it')
    ):
        load_campaign_config(campaign_path, plugins={'harness': {'codex': {'model': 'x'}}})
    keyed = {'harness': {'codex': {'model': 'x'}}, 'role': {'auditor': {'model': {'codex': 'y'}}}}
    with pytest.raises(ConfigError, match=re.escape('add it to [plugins.harness] use')):
        load_campaign_config(campaign_path, plugins=keyed)  # a key of a role's model puts no harness in use
    rotated = {'harness': {'codex': {'model': 'x'}}, 'role': {'auditor': {'harnesses': ['codex']}}}
    assert load_campaign_config(campaign_path, plugins=rotated).role_harnesses(AUDITOR) == ['codex']
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


def reviewer(keys: str) -> str:
    """``[plugins.role.reviewer]``, a sweep of the file's own, with ``keys``: the lines to append to a ``piloop.toml``."""
    return f'\n[plugins.role.reviewer]\n{keys}\n'


@pytest.mark.parametrize('name', ['orchestrator', 'Night Shift'])
def test_a_role_of_your_own_needs_a_role_name_that_signs_no_writes_already(campaign_path: Path, name: str) -> None:
    with pytest.raises(ConfigError, match=re.escape(f'{name!r} is not a role name')):
        load_campaign_config(campaign_path, plugins={'role': {name: {'runs': True}}})


def test_a_role_of_your_own_that_does_no_runs_is_refused(campaign_path: Path) -> None:
    with pytest.raises(
        ConfigError, match=re.escape('a role of your own does runs: set runs = true in [plugins.role.nightly]')
    ):
        load_campaign_config(campaign_path, plugins={'role': {'nightly': {'timeout': 60}}})


REMOVED_IN_0_8 = [
    ('\n[evaluator]\nscoring = "submit"\n', 'scoring = "submit"', '[evaluator] scoring', 'piloop scores what an'),
    ('\n[plugins.memory]\ndocuments = ["docs/*.md"]\n', '[plugins.memory]', '[plugins.memory]', 'research memory'),
    (
        '\n[plugins.section.lessons]\nbudget = 900\n',
        '[plugins.section.lessons]',
        '[plugins.section.lessons]',
        'research',
    ),
    (
        '\n[plugins.section.short]\nprovider = "lessons"\n',
        '[plugins.section.short]',
        '[plugins.section.short]',
        'research memory',
    ),
    (
        '\n[plugins.section]\nuse = ["headline", "lessons", "runs"]\n',
        'use = ["headline", "lessons", "runs"]',
        "'lessons' in [plugins.section] use",
        'research memory',
    ),
]


@pytest.mark.parametrize(('appended', 'line', 'what', 'why'), REMOVED_IN_0_8)
def test_a_key_that_0_8_removed_fails_with_one_line_that_names_it_and_its_line(
    campaign_path: Path, appended: str, line: str, what: str, why: str
) -> None:
    toml = campaign_path / 'piloop.toml'
    text = toml.read_text()
    toml.write_text(
        text.replace('noise_runs = 1', 'noise_runs = 1\nscoring = "submit"', 1)
        if 'scoring' in line
        else text + appended
    )
    with pytest.raises(ConfigError) as refused:
        load_campaign_config(campaign_path)
    [message] = str(refused.value).splitlines()
    lineno = toml.read_text().splitlines().index(line) + 1
    assert message.startswith(f'{toml}:{lineno}: delete {what}: piloop 0.8.0 removed it ({why}')
    assert message.endswith('). See https://piloop.autoresearch.inc/guides/upgrading')


def test_lessons_in_a_role_s_sections_is_named_at_that_role_s_line(campaign_path: Path) -> None:
    toml = campaign_path / 'piloop.toml'
    toml.write_text(
        toml.read_text().replace('[plugins.role.implementer]\n', '[plugins.role.implementer]\nsections = ["lessons"]\n')
    )
    with pytest.raises(ConfigError, match=r":\d+: delete 'lessons' in \[plugins.role.implementer\] sections: "):
        load_campaign_config(campaign_path)
