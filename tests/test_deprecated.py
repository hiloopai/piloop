"""The deprecation framework: a bridge defined here, each bridge of ``BRIDGES``, and the ``deprecated.until`` markers.

The marker scan reads the source, so a marker in a branch that never runs counts too. Once ``pyproject.toml`` is at or
past a marker's version, ``test_no_marker_reached_its_release`` fails with the place and the reason of each one, so the
release cannot ship until the code is deleted.
"""

import ast
import importlib
import io
import re
import tokenize
import tomllib
import warnings
from dataclasses import dataclass
from pathlib import Path

import pytest
from covalence import Cid

from piloop import deprecated
from piloop.config import ConfigError, load_campaign_config
from piloop.db.models import Campaign
from piloop.db.session import Database
from piloop.deprecated import PiloopDeprecationWarning
from piloop.plugins.campaign_type.kit.backlog.tables import Idea
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.evaluation.ids import TrackName
from piloop.plugins.campaign_type.kit.evaluation.tables import evaluator_of
from piloop.plugins.campaign_type.kit.runs.ids import IMPLEMENTER
from piloop.plugins.campaign_type.kit.runs.role.watchdog import WatchdogRole
from piloop.plugins.campaign_type.optimization.fact_source.optimization import OptimizationFacts
from piloop.plugins.executor.contract import AgentJob
from piloop.plugins.fact_source.campaign import CampaignFacts
from piloop.plugins.fact_source.command import Command
from piloop.plugins.fact_source.contract import FACT_SOURCES
from piloop.plugins.fact_source.spend import SpendFacts
from piloop.plugins.harness.codex import Codex
from piloop.plugins.harness.contract import each_provider
from piloop.plugins.harness.pi import Pi
from piloop.plugins.mcp_server.http import HttpServer
from piloop.plugins.mcp_server.stdio import StdioServer
from piloop.plugins.memory.contract import MEMORY
from piloop.plugins.memory.local import LocalMemory
from piloop.plugins.section.lessons import Lessons
from piloop.plugins.stop_rule.contract import STOP_RULES, WhileHeld
from piloop.plugins.stop_rule.limits import Limits
from piloop.settings import config_file
from piloop.version import Version
from tests.conftest import AGENT_TIMEOUT, HARNESS_APPEND, role_of, write_toml

TEST_BRIDGES: list[deprecated.Bridge] = []


@deprecated.bridge('9.0.0', 'size is width: delete rename_size', TEST_BRIDGES)
def rename_size(toml: Path, raw: dict[str, object], removed_in: Version) -> None:
    """``size = …`` is ``width = …``."""
    if 'size' in raw:
        raw['width'] = raw.pop('size')
        deprecated.warn(toml, 'size', 'size is deprecated; write width instead', removed_in=removed_in)


def test_a_bridge_rewrites_the_key_and_warns_at_the_line_that_set_it(tmp_path: Path) -> None:
    toml = tmp_path / 'piloop.toml'
    toml.write_text('name = "p"\nsize = 3\n')
    raw: dict[str, object] = {'name': 'p', 'size': 3}
    assert [rename_size] == TEST_BRIDGES, 'the declaration registers it'
    with pytest.warns(PiloopDeprecationWarning) as record:
        deprecated.apply(toml, raw, TEST_BRIDGES)
    assert raw == {'name': 'p', 'width': 3}
    assert [(w.filename, w.lineno, str(w.message)) for w in record] == [
        (str(toml), 2, 'size is deprecated; write width instead; removed in piloop 9.0.0')
    ]


def test_a_key_no_line_sets_warns_at_line_0(tmp_path: Path) -> None:
    toml = tmp_path / 'piloop.toml'
    toml.write_text('box = { size = 3 }\n')
    with pytest.warns(PiloopDeprecationWarning) as record:
        deprecated.warn(toml, 'size', 'size is deprecated', removed_in=Version(9, 0, 0))
    assert [w.lineno for w in record] == [0]


def test_the_cli_shows_each_deprecation_once(tmp_path: Path) -> None:
    toml = tmp_path / 'piloop.toml'
    toml.write_text('size = 3\n')
    with warnings.catch_warnings(record=True) as record:
        warnings.resetwarnings()
        deprecated.show_in_cli()
        for _ in range(2):
            rename_size(toml, {'size': 3})
    assert [(w.filename, w.lineno) for w in record] == [(str(toml), 1)], 'a file loaded twice warns once'
    assert warnings.formatwarning(record[0].message, PiloopDeprecationWarning, str(toml), 1).startswith(
        f'{toml}:1: PiloopDeprecationWarning: size is deprecated'
    )


def test_a_dotted_key_warns_at_its_table_header_or_at_the_line_that_sets_its_last_part(tmp_path: Path) -> None:
    toml = tmp_path / 'piloop.toml'
    toml.write_text('[roles.research]\nbuffer = 3\n\n[roles.research.scout]\ninterval = 60\n')
    with pytest.warns(PiloopDeprecationWarning) as record:
        deprecated.warn(toml, 'roles.research.scout', 'old', removed_in=Version(9, 0, 0))
        deprecated.warn(toml, 'roles.research.buffer', 'old', removed_in=Version(9, 0, 0))
    assert [w.lineno for w in record] == [4, 2]


def deprecated_config(campaign_path: Path, tables: str) -> Path:
    """Write the test campaign's piloop.toml with ``tables`` in place of its research table."""
    write_toml(campaign_path, HARNESS_APPEND)
    toml = campaign_path / 'piloop.toml'
    text = toml.read_text().replace(
        f'[plugins.role.research]\ntimeout = {AGENT_TIMEOUT}\nmax_ideas = 0\nmax_concurrent = 1\n', ''
    )
    toml.write_text(text + tables)
    return toml


def without_plugin_tables(text: str) -> str:
    """``text`` without its ``[plugins.<…>]`` tables, so that a test can set ``plugins`` to something else."""
    return re.sub(r'^\[plugins\.[^\]]*\]\n(?:[^\[\n].*\n)*', '', text, flags=re.MULTILINE)


TEMPLATES = Path(__file__).parent / 'fixtures' / 'templates'
"""The campaign template that ``piloop init`` wrote in 0.1.0, and in this release before the scout was a role."""


ROOT = Path(__file__).parents[1]
NOTE = re.compile(
    r'\b(?:TODO|FIXME|XXX)\b.*\d+\.\d+'  # a TODO tied to a version
    r'|\b(?:remov|delet|drop)\w*\b.*\b(?:release|\d+\.\d+)',  # "delete it in the release after 0.2.0"
    re.IGNORECASE,
)
"""A comment or docstring line that says to delete code at a release. That code takes a ``deprecated.until`` marker."""
DEPRECATED = re.compile(r'(?<![\w.])deprecated(?![\w.])', re.IGNORECASE)
"""A comment that calls code deprecated (but not one that names ``piloop.deprecated``). A docstring may: it describes a
deprecated key to the reader."""


def test_wake_budget_is_the_budget_of_the_lessons_section_of_each_role(campaign_path: Path) -> None:
    write_toml(campaign_path, HARNESS_APPEND)
    toml = campaign_path / 'piloop.toml'
    text = toml.read_text().replace('[plugins.role.research]\n', '[plugins.role.research]\nwake_budget = 0\n')
    toml.write_text(text + '\n[memory]\nwake_budget = 200\n')
    with pytest.warns(PiloopDeprecationWarning, match='wake_budget is deprecated') as record:
        cfg = load_campaign_config(campaign_path)
    assert len([w for w in record if 'wake_budget' in str(w.message)]) == 2
    lessons = {
        name: next((s.budget for s in cfg.sections_of(name) if isinstance(s, Lessons)), None)
        for name in cfg.roles.all()
    }
    assert lessons == {'implementer': 200, 'orchestrator': None, 'watchdog': 400, 'research': None, 'scout': 200}, (
        "[memory]'s budget for every role but the watchdog, whose old default was its own; 0 leaves them out"
    )


def test_memory_documents_are_the_documents_of_plugins_memory_whose_own_value_wins(campaign_path: Path) -> None:
    write_toml(campaign_path, HARNESS_APPEND)
    toml = campaign_path / 'piloop.toml'
    text = toml.read_text()
    toml.write_text(text + '\n[memory]\ndocuments = ["docs/*.md"]\nwake_budget = 400\n')
    with pytest.warns(PiloopDeprecationWarning) as record:
        cfg = load_campaign_config(campaign_path)
    assert MEMORY.selected(cfg.plugins) == LocalMemory(documents=['docs/*.md']), 'the [memory] table goes with its keys'
    line = toml.read_text().splitlines().index('[memory]') + 2
    assert [(str(w.message).partition(';')[0], w.lineno) for w in record] == [
        ('wake_budget is deprecated', line + 1),
        ('[memory] documents is deprecated', line),
    ]
    toml.write_text(text + '\n[plugins.memory]\ndocuments = ["notes/*.md"]\n\n[memory]\ndocuments = ["docs/*.md"]\n')
    with pytest.warns(PiloopDeprecationWarning, match=r'\[memory\] documents is deprecated'):
        cfg = load_campaign_config(campaign_path)
    assert MEMORY.selected(cfg.plugins) == LocalMemory(documents=['notes/*.md'])


def test_the_mcp_and_usage_of_a_harness_table_are_its_provider(campaign_path: Path) -> None:
    write_toml(campaign_path, HARNESS_APPEND)
    toml = campaign_path / 'piloop.toml'
    toml.write_text(
        toml.read_text()
        + '\n[plugins.harness.claude]\nmodel = "c"\nmcp = "claude"\n'
        + '[plugins.harness.claude.usage]\nfile = "{env:CLAUDE_CONFIG_DIR:~/.claude}/x"\n'
        + '\n[plugins.harness.local]\nmodel = "l"\nexecutable = "local"\nheadless = ["--id", "{session_id}", "{mcp}", "{prompt}"]\n'
        + 'mcp = "codex"\n[plugins.harness.local.usage]\ntally = "pi"\n'
    )
    toml.write_text(toml.read_text().replace('use = ["script"]', 'use = ["script", "claude", "local"]', 1))
    with pytest.warns(PiloopDeprecationWarning) as record:
        cfg = load_campaign_config(campaign_path)
    assert cfg.harness_for('claude') == each_provider()['claude'].model_copy(update={'model': 'c'}), (
        'a built-in table drops them'
    )
    assert isinstance(cfg.harness_for('local'), Pi), "a new name takes its tally's provider"
    assert [str(w.message).partition(';')[0] for w in record] == [
        'mcp and usage of [plugins.harness.claude] are deprecated: the harness renders its servers and reads its usage itself,'
        ' under its home',
        'mcp and usage of [plugins.harness.local] are deprecated',
    ]


def test_the_anomaly_trigger_is_the_two_incident_triggers_in_its_place(campaign_path: Path) -> None:
    write_toml(campaign_path, HARNESS_APPEND)
    toml = campaign_path / 'piloop.toml'
    old = 'triggers = [{ provider = "cron" }, { provider = "anomaly" }]\n'
    toml.write_text(toml.read_text().replace('[plugins.role.watchdog]\n', f'[plugins.role.watchdog]\n{old}'))
    with pytest.warns(PiloopDeprecationWarning) as record:
        cfg = load_campaign_config(campaign_path)
    assert [str(w.message).partition(';')[0] for w in record] == [
        'the anomaly trigger is deprecated',
        '`plugins.role.<name>.triggers` of inline entries is deprecated',
    ]
    assert role_of(cfg, WatchdogRole).triggers == ['cron', 'run_failed', 'dead_at_startup']


OLD_SHAPE = """
name = "demo"
evaluate = ["true"]
harnesses = ["script"]
model = { script = "test-model", pi = "p" }
max_concurrent = { script = 2 }

[[metrics]]
name = "score"
direction = "higher"
role = "primary"

[harness.script]
executable = "agent"
headless = ["{prompt}"]

[roles.implementer]
max_concurrent = 3

[plugins.role.research]
max_ideas = 0
harnesses = ["script", "pi"]
"""
"""A piloop.toml of 0.3: the harnesses, their models and caps, and the roles at the top. Research is in its new table."""
NEW_SHAPE = """
name = "demo"
evaluate = ["true"]

[plugins.harness]
use = ["script"]

[[metrics]]
name = "score"
direction = "higher"
role = "primary"

[plugins.harness.script]
model = "test-model"
max_concurrent = 2
executable = "agent"
headless = ["{prompt}"]

[plugins.harness.pi]
model = "p"

[plugins.role.implementer]
max_concurrent = 3

[plugins.role.research]
max_ideas = 0
harnesses = ["script", "pi"]
"""


def test_a_move_moves_each_key_that_its_old_path_matches_and_the_new_path_keeps_its_own_values() -> None:
    raw: dict[str, object] = {
        'model': {'claude': 'c', 'pi': 'p'},
        'roles': {'research': {'timeout': 9, 'max_ideas': 2}, 'scout': {}},
        'plugins': {'role': {'research': {'timeout': 5}}, 'harness': {'claude': {'model': 'mine'}}},
    }
    moved = [m.apply(raw) for m in deprecated.PLUGIN_TABLES]
    assert moved == [[], ['model.claude', 'model.pi'], [], ['roles.research', 'roles.scout'], [], []]
    assert raw == {
        'plugins': {
            'role': {'research': {'timeout': 5, 'max_ideas': 2}, 'scout': {}},
            'harness': {'claude': {'model': 'mine'}, 'pi': {'model': 'p'}},
        }
    }, 'a table merges into the new one key by key, a value there wins, and an emptied old table goes'
    back: dict[str, object] = {'plugins': {'harness': {'pi': {'model': 'p', 'env': {}}}}, 'harnesses': ['pi']}
    for move in reversed(deprecated.PLUGIN_TABLES):
        move.inverse().apply(back)
    assert back == {'harness': {'pi': {'env': {}}}, 'model': {'pi': 'p'}, 'harnesses': ['pi']}
    empty: dict[str, object] = {'model': {}, 'harness': {}}
    assert [m.apply(empty) for m in deprecated.PLUGIN_TABLES] == [['harness'], ['model'], [], [], [], []]
    assert empty == {}, 'an empty table of the old key goes, with its warning'
    scalar: dict[str, object] = {'roles': 3}
    assert deprecated.Move('roles.*', 'plugins.role.*').apply(scalar) == [], 'no table, no names to move'
    assert scalar == {'roles': 3}, 'left for the validation to refuse'
    lost: dict[str, object] = {'roles': {'a': {}}, 'plugins': 3}
    deprecated.Move('roles.*', 'plugins.role.*').apply(lost)
    assert lost == {'plugins': 3}, 'a value on the way that is not a table is left for the validation to refuse'


def test_a_move_rebases_a_path_under_its_old_key() -> None:
    roles, model = deprecated.Move('roles.*', 'plugins.role.*'), deprecated.Move('model.*', 'plugins.harness.*.model')
    assert roles.rebased(['roles', 'implementer', 'max_concurrent']) == [
        'plugins',
        'role',
        'implementer',
        'max_concurrent',
    ]
    assert model.rebased(['model', 'claude']) == ['plugins', 'harness', 'claude', 'model']
    assert roles.rebased(['roles']) is None, 'shorter than the old path'
    assert roles.rebased(['model', 'claude']) is None, 'another key'


def test_the_harness_and_role_tables_of_0_3_are_their_plugin_tables_with_a_warning_at_each_old_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    toml = tmp_path / 'piloop.toml'
    toml.write_text(NEW_SHAPE)
    new = load_campaign_config(tmp_path)
    toml.write_text(OLD_SHAPE)
    with pytest.warns(PiloopDeprecationWarning) as record:
        old = load_campaign_config(tmp_path)
    assert old == new
    lines = OLD_SHAPE.splitlines()
    assert [(str(w.message).partition(';')[0], w.lineno) for w in record] == [
        ('`harness.<name>` is deprecated', lines.index('[harness.script]') + 1),
        ('`model.<name>` is deprecated', lines.index('model = { script = "test-model", pi = "p" }') + 1),
        ('`max_concurrent.<name>` is deprecated', lines.index('max_concurrent = { script = 2 }') + 1),
        ('`roles.<name>` is deprecated', lines.index('[roles.implementer]') + 1),
        ('`harnesses` is deprecated', lines.index('harnesses = ["script"]') + 1),
    ], 'an inline table warns at the key that holds it'
    assert str(record[0].message) == (
        '`harness.<name>` is deprecated; write `plugins.harness.<name>`; removed in piloop 0.5.0'
    )
    toml.write_text(NEW_SHAPE)
    monkeypatch.setenv('PILOOP_CAMPAIGN_ROLES__IMPLEMENTER__MAX_CONCURRENT', '7')
    with pytest.warns(
        PiloopDeprecationWarning,
        match='PILOOP_CAMPAIGN_ROLES__IMPLEMENTER__MAX_CONCURRENT is deprecated; set PILOOP_CAMPAIGN_PLUGINS__ROLE__IMPLEMENTER__MAX_CONCURRENT',
    ):
        cfg = load_campaign_config(tmp_path)
    assert cfg.roles.doing_runs()[IMPLEMENTER].max_concurrent == 7, 'the old variable wins over the file'


def test_entries_name_their_instances_and_an_entry_with_parameters_gets_a_table_of_its_own() -> None:
    triggers = deprecated.Entries('plugins.role.*.triggers', 'plugins.role.*.triggers', 'plugins.trigger', '*')
    research = [{'provider': 'low_water', 'buffer': 4}, {'provider': 'seed'}, 'stalled', 3]
    raw: dict[str, object] = {
        'plugins': {
            'role': {'research': {'triggers': research}, 'scout': {'triggers': ['cron']}},
            'trigger': {'research_low_water': {'provider': 'low_water', 'buffer': 9}},
        }
    }
    assert triggers.apply(raw) == ['plugins.role.research.triggers'], 'a list of names is new already'
    assert raw == {
        'plugins': {
            'role': {
                'research': {'triggers': ['research_low_water_2', 'seed', 'stalled', 3]},
                'scout': {'triggers': ['cron']},
            },
            'trigger': {
                'research_low_water': {'provider': 'low_water', 'buffer': 9},
                'research_low_water_2': {'provider': 'low_water', 'buffer': 4},
            },
        }
    }, 'a name that a table with other keys took gets _2; a name or anything else that is no entry stays'
    facts = deprecated.Entries('facts', 'plugins.fact_source.use', 'plugins.fact_source', None)
    spend = {'provider': 'command', 'command': ['true']}
    listed: dict[str, object] = {'facts': [{'provider': 'campaign'}, spend, dict(spend)]}
    assert facts.apply(listed) == ['facts']
    assert listed == {
        'plugins': {'fact_source': {'command': {'command': ['true']}, 'use': ['campaign', 'command', 'command']}}
    }, 'without a prefix, the provider names the table, and an entry like one before shares it'
    unset: dict[str, object] = {'facts': None, 'status': {'sections': None}}
    status = deprecated.Entries('status.sections', 'plugins.section.use', 'plugins.section', 'status')
    assert (facts.apply(unset), status.apply(unset)) == (['facts'], ['status.sections'])
    assert unset == {}, 'a list that is not set goes, and its table with it'
    tables = listed['plugins']
    assert isinstance(tables, dict)
    back: dict[str, object] = {'plugins': {**tables, 'role': {'scout': {'triggers': ['seed', 'nope']}}}}
    for each in (facts, triggers):
        each.inverse().apply(back)
    assert back == {
        'facts': [{'provider': 'campaign'}, spend, spend],
        'plugins': {
            'fact_source': {'command': {'command': ['true']}},
            'role': {
                'scout': {
                    'triggers': [
                        {'provider': 'seed'},
                        {'provider': 'nope'},
                    ]
                }
            },
        },
    }, 'each name is its entry again: its table with its provider, else the provider of its name; the tables stay'
    odd: dict[str, object] = {'plugins': {'fact_source': {'use': 'campaign'}}}
    assert facts.inverse().apply(odd) == ['plugins.fact_source.use']
    assert odd == {}, 'a use that is no list goes, and writes no facts: the validation would have refused it'


LISTS_0_3 = """
name = "demo"
evaluate = ["true"]

[plugins.harness]
use = ["script"]

[plugins.harness.script]
model = "m"
executable = "agent"
headless = ["{prompt}"]

[[metrics]]
name = "score"
direction = "higher"
role = "primary"

[[facts]]
provider = "campaign"

[[facts]]
provider = "optimization"

[[facts]]
provider = "command"
command = ["true"]
metrics = ["spend"]

[status]
sections = [{ provider = "headline" }, { provider = "lessons", budget = 900 }]

[roles.research]
triggers = [{ provider = "low_water", buffer = 4 }, { provider = "seed" }]
sections = [{ provider = "goal" }]
"""
"""A piloop.toml of 0.3.1 with lists of inline entries: the facts, the status's sections, research's triggers and
sections."""
LISTS_0_4 = """
name = "demo"
evaluate = ["true"]

[plugins.harness]
use = ["script"]

[plugins.harness.script]
model = "m"
executable = "agent"
headless = ["{prompt}"]

[[metrics]]
name = "score"
direction = "higher"
role = "primary"

[plugins.fact_source]
use = ["campaign", "optimization", "command"]

[plugins.fact_source.command]
command = ["true"]
metrics = ["spend"]

[plugins.section]
use = ["headline", "status_lessons"]

[plugins.section.status_lessons]
provider = "lessons"
budget = 900

[plugins.role.research]
triggers = ["research_low_water", "seed"]
sections = ["goal"]

[plugins.trigger.research_low_water]
provider = "low_water"
buffer = 4
"""


def test_the_lists_of_0_3_name_instances_with_a_warning_at_each_list_that_the_file_writes(tmp_path: Path) -> None:
    toml = tmp_path / 'piloop.toml'
    toml.write_text(LISTS_0_4)
    new = load_campaign_config(tmp_path)
    toml.write_text(LISTS_0_3)
    with pytest.warns(PiloopDeprecationWarning) as record:
        old = load_campaign_config(tmp_path)
    assert old == new
    lines = LISTS_0_3.splitlines()
    assert [(str(w.message).partition(' of ')[0], w.lineno) for w in record if 'inline entries' in str(w.message)] == [
        (
            '`plugins.role.<name>.triggers`',
            lines.index('triggers = [{ provider = "low_water", buffer = 4 }, { provider = "seed" }]') + 1,
        ),
        # a dotted key warns at the first line that sets its last part: here, that of [status]
        ('`plugins.role.<name>.sections`', lines.index('[status]') + 2),
        ('`status.sections`', lines.index('[status]') + 2),
        ('`facts`', lines.index('[[facts]]') + 1),
    ]
    assert [type(s).__name__ for s in old.status_sections()] == ['Headline', 'Lessons']


def test_the_runtimes_of_config_toml_are_its_runtime_plugin_tables(tmp_path: Path) -> None:
    (tmp_path / 'config.toml').write_text('[runtimes.subprocess]\n')
    with pytest.warns(
        PiloopDeprecationWarning, match=r'`runtimes.<name>` is deprecated; write `plugins.runtime.<name>`'
    ):
        raw = config_file(tmp_path)
    assert raw == {'plugins': {'runtime': {'subprocess': {}}}}


def test_a_tag_gives_each_table_the_value_of_its_first_marker_unless_it_sets_the_key() -> None:
    tag = deprecated.Tag('plugins.mcp_server.*', 'provider', (('url', 'http'), ('command', 'stdio')))
    raw: dict[str, object] = {
        'plugins': {
            'mcp_server': {
                'exa': {'url': 'https://x'},
                'xai': {'command': ['x']},
                'mine': {'provider': 'search', 'url': 'https://y'},
                'odd': {'env': []},
                'scalar': 3,
            }
        }
    }
    assert tag.apply(raw) == ['plugins.mcp_server.exa', 'plugins.mcp_server.xai']
    assert raw['plugins'] == {
        'mcp_server': {
            'exa': {'url': 'https://x', 'provider': 'http'},
            'xai': {'command': ['x'], 'provider': 'stdio'},
            'mine': {'provider': 'search', 'url': 'https://y'},
            'odd': {'env': []},
            'scalar': 3,
        }
    }, 'a table that sets the key keeps it, one without a marker and a value that is no table are left'
    assert tag.inverse().apply(raw) == ['plugins.mcp_server.exa', 'plugins.mcp_server.xai']
    assert raw['plugins'] == {
        'mcp_server': {
            'exa': {'url': 'https://x'},
            'xai': {'command': ['x']},
            'mine': {'provider': 'search', 'url': 'https://y'},
            'odd': {'env': []},
            'scalar': 3,
        }
    }, 'the inverse drops only the values that the tag sets'


def test_a_prune_drops_the_keys_that_hold_nothing_and_has_nothing_to_undo() -> None:
    """A stored config of 0.3 dumped every key of a server, those of the other transport empty."""
    prune = deprecated.Prune('plugins.mcp_server.*')
    raw: dict[str, object] = {
        'plugins': {
            'mcp_server': {
                'xai': {'url': None, 'headers_env': {}, 'bearer_token_env': None, 'command': ['x'], 'env': []},
                'exa': {'url': 'https://x'},
                'scalar': 3,
            }
        }
    }
    assert prune.apply(raw) == ['plugins.mcp_server.xai']
    assert raw['plugins'] == {'mcp_server': {'xai': {'command': ['x']}, 'exa': {'url': 'https://x'}, 'scalar': 3}}
    assert prune.inverse() is None, 'each key that it dropped is a default'
    stored: dict[str, object] = {
        'mcp': {'xai': {'url': None, 'headers_env': {}, 'bearer_token_env': None, 'command': ['x'], 'env': []}}
    }
    for step in deprecated.MCP_TABLES:
        step.apply(stored)
    assert stored == {'plugins': {'mcp_server': {'xai': {'command': ['x'], 'provider': 'stdio'}}}}


def test_an_mcp_table_of_0_3_is_its_mcp_server_plugin_table_with_the_provider_of_its_keys(
    campaign_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    toml = campaign_path / 'piloop.toml'
    scout = '\n[plugins.role.scout]\nharnesses = ["claude"]\nmodel = { claude = "m" }\nmcp = ["exa", "xai"]\n'
    text = toml.read_text() + scout
    new = (
        '\n[plugins.mcp_server.exa]\nprovider = "http"\nurl = "https://mcp.exa.ai/mcp"\n'
        '\n[plugins.mcp_server.xai]\nprovider = "stdio"\ncommand = ["xai"]\nenv = ["XAI_API_KEY"]\n'
    )
    toml.write_text(text + new)
    expected = load_campaign_config(campaign_path)
    old = '\n[mcp.exa]\nurl = "https://mcp.exa.ai/mcp"\n\n[mcp.xai]\ncommand = ["xai"]\nenv = ["XAI_API_KEY"]\n'
    toml.write_text(text + old)
    with pytest.warns(PiloopDeprecationWarning) as record:
        assert load_campaign_config(campaign_path) == expected
    assert [str(w.message).partition(';')[0] for w in record] == ['`mcp.<name>` is deprecated']
    toml.write_text(text + new)
    monkeypatch.setenv('PILOOP_CAMPAIGN_MCP__EXA__URL', 'https://other.example/mcp')
    with pytest.warns(PiloopDeprecationWarning, match='PILOOP_CAMPAIGN_MCP__EXA__URL is deprecated'):
        server = load_campaign_config(campaign_path).mcp_servers['exa']
    assert server == expected.mcp_servers['exa'].model_copy(update={'url': 'https://other.example/mcp'})


@dataclass(frozen=True)
class Marker:
    where: str
    version: Version
    reason: str


def _is_marker(node: ast.Call, module: str) -> bool:
    """Whether ``node`` calls ``until``: ``x.until(…)`` under any name ``x``, or ``until(…)`` or ``bridge(…)`` in its own
    module."""
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr == 'until'
    return module == 'piloop/deprecated.py' and isinstance(func, ast.Name) and func.id in ('until', 'bridge')


def _hides_until(node: ast.ImportFrom, alias: ast.alias) -> bool:
    """Whether the import gives ``until`` another name than ``deprecated.until``."""
    if node.module == 'piloop.deprecated':
        return alias.name == 'until'
    return node.module == 'piloop' and alias.name == 'deprecated' and alias.asname not in (None, 'deprecated')


def _marker(node: ast.Call, where: str) -> Marker | str:
    """Return the marker that ``node`` calls, or why it is malformed."""
    if isinstance(node.func, ast.Attribute) and not (
        isinstance(node.func.value, ast.Name) and node.func.value.id == 'deprecated'
    ):
        return f'{where}: write deprecated.until(...), so that the scan finds it'
    match node.args, node.keywords:
        case [ast.Constant(value=str(version)), ast.Constant(value=str(reason)), *_], [] if reason.strip():
            try:
                return Marker(where, Version.parse(version), reason)
            except ValueError as e:
                return f'{where}: {e}'
    return f'{where}: write deprecated.until(<"X.Y.Z">, <"reason">), with two literal strings'


def _notes(source: str, tree: ast.Module) -> list[tuple[int, str]]:
    """Return ``(line number, text)`` of each comment and docstring line of ``source`` that reads as a compat note."""
    comments = [
        (tok.start[0], tok.string.lstrip('#').strip())
        for tok in tokenize.generate_tokens(io.StringIO(source).readline)
        if tok.type == tokenize.COMMENT
    ]
    docstrings = [
        (node.lineno + n, line.strip())
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
        for n, line in enumerate(node.value.value.splitlines())
    ]
    return [(n, t) for n, t in comments if NOTE.search(t) or DEPRECATED.search(t)] + [
        (n, t) for n, t in docstrings if NOTE.search(t)
    ]


def scan(root: Path) -> tuple[list[Marker], list[str]]:
    """Return the markers under ``root / 'piloop'``, and the problems: a malformed marker, a free-text compat note."""
    markers: list[Marker] = []
    problems: list[str] = []
    for path in sorted((root / 'piloop').rglob('*.py')):
        module = path.relative_to(root).as_posix()
        source = path.read_text()
        tree = ast.parse(source, module)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _is_marker(node, module):
                found = _marker(node, f'{module}:{node.lineno}')
                if isinstance(found, Marker):
                    markers.append(found)
                else:
                    problems.append(found)
            elif isinstance(node, ast.ImportFrom) and any(_hides_until(node, alias) for alias in node.names):
                problems.append(
                    f'{module}:{node.lineno}: write `from piloop import deprecated`, then deprecated.until(...)'
                )
        problems += [
            f'{module}:{n}: mark this with deprecated.until(<"X.Y.Z">, <"reason">): {text}'
            for n, text in _notes(source, tree)
        ]
    return markers, problems


def expired(markers: list[Marker], version: Version) -> list[Marker]:
    """Return the markers whose release ``version`` has reached."""
    return [m for m in markers if m.version <= version]


def piloop_version(root: Path = ROOT) -> Version:
    """The version in ``pyproject.toml``. A pre-release of a version counts as that version (``Version.parse``)."""
    return Version.parse(tomllib.loads((root / 'pyproject.toml').read_text())['project']['version'])


def test_no_marker_reached_its_release() -> None:
    markers, problems = scan(ROOT)
    assert not problems, '\n'.join(problems)
    version = piloop_version()
    due = '\n'.join(f'{m.where}: {m.reason}' for m in expired(markers, version))
    assert not due, f'piloop {version} removes this compat code; delete it and its tests:\n{due}'


def write(root: Path, name: str, source: str) -> None:
    path = root / 'piloop' / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source)


def test_the_scan_finds_a_marker_in_a_branch_and_on_a_function(tmp_path: Path) -> None:
    write(
        tmp_path,
        'a.py',
        'from piloop import deprecated\n'
        '\n'
        "@deprecated.until('0.3.0', 'the old key')\n"
        'def f(x):\n'
        '    if x:\n'
        "        deprecated.until('0.4.0', 'the old env' ' variable')\n",
    )
    assert scan(tmp_path) == (
        [
            Marker('piloop/a.py:3', Version(0, 3, 0), 'the old key'),
            Marker('piloop/a.py:6', Version(0, 4, 0), 'the old env variable'),
        ],
        [],
    )


@pytest.mark.parametrize(
    'call',
    [
        "deprecated.until('0.3', 'r')",
        "deprecated.until('v0.3.0', 'r')",
        "deprecated.until(V, 'r')",
        "deprecated.until('0.3.0', '')",
        "deprecated.until('0.3.0')",
        "deprecated.until('0.3.0', reason='r')",
    ],
)
def test_a_malformed_marker_is_a_problem(tmp_path: Path, call: str) -> None:
    write(tmp_path, 'a.py', f'from piloop import deprecated\n{call}\n')
    markers, problems = scan(tmp_path)
    assert markers == []
    assert len(problems) == 1 and problems[0].startswith('piloop/a.py:2: '), problems


@pytest.mark.parametrize(
    'source',
    [
        'from piloop.deprecated import until\n',
        'from piloop import deprecated as d\n',
        "import piloop.deprecated\npiloop.deprecated.until('0.3.0', 'r')\n",
    ],
)
def test_until_under_another_name_is_a_problem(tmp_path: Path, source: str) -> None:
    write(tmp_path, 'a.py', source)
    markers, problems = scan(tmp_path)
    assert markers == []
    assert [p.partition(': ')[0] for p in problems] == [f'piloop/a.py:{len(source.splitlines())}'], problems


def test_deprecated_py_calls_until_by_its_name(tmp_path: Path) -> None:
    write(tmp_path, 'deprecated.py', "until('0.3.0', 'r')\n")
    assert scan(tmp_path) == ([Marker('piloop/deprecated.py:1', Version(0, 3, 0), 'r')], [])


@pytest.mark.parametrize(
    'text',
    [
        '# TODO(0.3.0): delete it',
        '# TODO: delete this fallback in the release after 0.2.0',
        '# removed in piloop 0.3.0',
        '# the old key is deprecated',
        '"""Return x.\n\nTODO: delete it with the hint one minor release after 0.2.0."""',
    ],
)
def test_a_free_text_compat_note_is_a_problem(tmp_path: Path, text: str) -> None:
    write(tmp_path, 'a.py', f'x = 1\n{text}\n')
    assert [p.partition(': ')[0] for p in scan(tmp_path)[1]] == [f'piloop/a.py:{1 + len(text.splitlines())}']


@pytest.mark.parametrize(
    'text',
    [
        '# TODO: remove it once the harness reports that state',
        '# the directories keep their 0.1 name',
        '# see piloop.deprecated.old_env',
        "warn('the key is deprecated; removed in piloop 0.3.0')",
    ],
)
def test_other_comments_and_strings_are_not_problems(tmp_path: Path, text: str) -> None:
    write(tmp_path, 'a.py', f'{text}\n')
    assert scan(tmp_path) == ([], [])


def test_a_marker_expires_at_its_release_and_its_pre_releases() -> None:
    marker = Marker('piloop/a.py:1', Version(0, 3, 0), 'r')
    assert expired([marker], Version.parse('0.2.9')) == []
    assert expired([marker], Version.parse('0.3.0')) == [marker]
    assert expired([marker], Version.parse('0.3.0rc1')) == [marker]
    assert expired([marker], Version.parse('1.0.0')) == [marker]


@pytest.mark.parametrize('text', ['0.3', 'v0.3.0', '0.3.0.1', '0.3.0-dev', ''])
def test_a_version_that_is_not_x_y_z_fails_to_parse_and_until_refuses_it(text: str) -> None:
    with pytest.raises(ValueError, match=rf'^{re.escape(repr(text))} is not a version X\.Y\.Z$'):
        Version.parse(text)
    with pytest.raises(ValueError, match='is not a version X.Y.Z'):
        deprecated.until(text, 'r')


def test_until_leaves_what_it_marks_as_it_is() -> None:
    def f() -> int:
        return 1

    assert deprecated.until('0.3.0', 'r')(f) is f


def test_spend_is_a_command_source_whose_fact_is_all_of_the_spend_and_closing_review_is_while_held(
    campaign_path: Path, tmp_path: Path
) -> None:
    """``[spend]`` is the ``command`` source ``billing``, and the ``spend`` source sums only its ``billing_usd``.

    So ``hold_at = { spend = … }`` holds on what it held on. Both sources come after the campaign's own.
    """
    (tmp_path / 'bill').write_text('12.5\n')
    toml = deprecated_config(
        campaign_path,
        f'[spend]\ncommand = ["cat", "{tmp_path / "bill"}"]\nnotices = [50]\ninterval = 60\nclosing_review = "wait"\n',
    )
    with pytest.warns(
        PiloopDeprecationWarning, match=r'\[spend\] is deprecated; write a \[plugins.fact_source.<name>\] source'
    ):
        cfg = load_campaign_config(campaign_path)
    [campaign, optimization, billing, spend] = cfg.fact_sources()
    command = [*deprecated.SPEND_COMMAND, 'cat', str(tmp_path / 'bill')]
    assert (campaign, optimization, billing, spend) == (
        CampaignFacts(),
        OptimizationFacts(),
        Command(command=command, metrics=['billing_usd'], notices={'billing_usd': [50]}, interval=60),
        SpendFacts(facts=['billing_usd']),
    ), "after piloop's own sources"
    assert isinstance(billing, Command) and billing.read(tmp_path) == {'billing_usd': 12.5}, (
        'the old command, which prints one number, still reads'
    )
    assert STOP_RULES.selected(cfg.plugins).while_held is WhileHeld.WAIT
    assert 'closing_review = "wait"' in toml.read_text(), 'the file is not rewritten'


@pytest.mark.parametrize(
    ('sources', 'used'),
    [
        (
            '[plugins.fact_source]\nuse = ["optimization", "campaign"]\n',
            ['optimization', 'campaign', 'billing', 'spend'],
        ),
        (
            '[[facts]]\nprovider = "optimization"\n\n[[facts]]\nprovider = "campaign"\n',
            ['optimization', 'campaign', 'billing', 'spend'],
        ),
    ],
)
def test_spend_joins_the_sources_that_the_file_lists(campaign_path: Path, sources: str, used: list[str]) -> None:
    """A ``use`` of the file, or a 0.3.1 ``[[facts]]``, keeps its sources, and the two come after them."""
    deprecated_config(campaign_path, f'[spend]\ncommand = ["true"]\n\n{sources}')
    with pytest.warns(PiloopDeprecationWarning):
        cfg = load_campaign_config(campaign_path)
    assert list(FACT_SOURCES.used(cfg.plugins)) == used


@pytest.mark.parametrize('plugins', ['plugins = 3\n', '[plugins.fact_source]\nuse = "campaign"\n'])
def test_spend_leaves_a_plugins_table_or_a_use_that_is_no_list_to_the_validation(
    campaign_path: Path, plugins: str
) -> None:
    toml = deprecated_config(campaign_path, '[spend]\ncommand = ["true"]\nclosing_review = "skip"\n')
    toml.write_text(
        plugins + without_plugin_tables(toml.read_text())
        if plugins.startswith('plugins')
        else toml.read_text() + plugins
    )
    with pytest.warns(PiloopDeprecationWarning), pytest.raises(ConfigError, match='plugins'):
        load_campaign_config(campaign_path)


def test_max_spend_is_hold_at_of_the_spend(campaign_path: Path) -> None:
    deprecated_config(
        campaign_path,
        '[plugins.fact_source]\nuse = ["campaign", "optimization", "spend"]\n\n[plugins.fact_source.spend]\n'
        'provider = "command"\ncommand = ["true"]\nmetrics = ["spend"]\n\n[plugins.stop_rule]\nmax_spend = 300\n',
    )
    with pytest.warns(PiloopDeprecationWarning, match=r'max_spend is deprecated; write hold_at = \{ spend = \.\.\. \}'):
        cfg = load_campaign_config(campaign_path)
    assert STOP_RULES.selected(cfg.plugins) == Limits(hold_at={'spend': 300})


def test_a_job_that_a_0_3_daemon_queued_names_its_harness_by_its_renderer_or_tally() -> None:
    old = {
        'agent_session': 1,
        'runtime': 'subprocess',
        'artifact': {'repo': 1, 'revision': 'c1', 'vcs': {'provider': 'git'}},
        'place': 'w',
        'timeout': 60,
        'brief_cid': str(Cid.of(b'brief')),
        'spec': {'executable': 'true', 'args': []},
    }
    usage = {'reader': 'jsonl', 'file': 'x', 'tally': 'codex'}
    assert isinstance(AgentJob.model_validate(old | {'mcp': 'claude', 'usage': usage}).harness, Codex)
    assert isinstance(AgentJob.model_validate(old | {'mcp': 'pi', 'usage': None}).harness, Pi)
    assert AgentJob.model_validate(old | {'mcp': None, 'usage': None}).harness is None, 'a harness of no provider'
    exa = {'url': 'https://x', 'headers_env': {}, 'bearer_token_env': None, 'command': None, 'env': []}
    xai = {'url': None, 'headers_env': {}, 'bearer_token_env': None, 'command': ['x'], 'env': ['K']}
    job = AgentJob.model_validate(old | {'servers': {'exa': exa, 'xai': xai}})
    assert job.servers == {'exa': HttpServer(url='https://x'), 'xai': StdioServer(command=['x'], env=['K'])}, (
        'the servers of a job of protocol 13 get the provider of their keys'
    )
    assert AgentJob.model_validate(old | {'servers': {'exa': {'provider': 'http', 'url': 'https://x'}}}).servers == {
        'exa': HttpServer(url='https://x')
    }


def test_a_harness_or_a_server_of_0_3_that_nothing_uses_warns_until_0_5_0_and_one_of_the_new_shape_is_refused(
    campaign_path: Path,
) -> None:
    """A 0.3 file set a model for a harness that only ideas pinned: it loads, and says to add it to ``use``."""
    toml = campaign_path / 'piloop.toml'
    base = 'name = "demo"\nevaluate = ["true"]\n\n[[metrics]]\nname = "s"\ndirection = "higher"\nrole = "primary"\n'
    toml.write_text(
        'harnesses = ["pi"]\nmodel = { pi = "p", codex = "x" }\n' + base + '\n[mcp.exa]\nurl = "https://e"\n'
    )
    with pytest.warns(PiloopDeprecationWarning) as record:
        cfg = load_campaign_config(campaign_path)
    assert cfg.harnesses == ['pi'] and cfg.harness['codex'].model == 'x'
    unused = sorted(str(w.message) for w in record if 'nothing uses it' in str(w.message))
    assert unused == [
        '[plugins.harness.codex] is configured, and nothing uses it: add it to [plugins.harness] use; a file of 0.3 only'
        ' gets this warning; removed in piloop 0.5.0',
        '[plugins.mcp_server.exa] is configured, and nothing uses it: add it to a list that names it; a file of 0.3 only'
        ' gets this warning; removed in piloop 0.5.0',
    ]
    toml.write_text(
        base
        + '\n[plugins.harness]\nuse = ["pi"]\n\n[plugins.harness.pi]\nmodel = "p"\n\n[plugins.harness.codex]\nmodel = "x"\n'
    )
    with pytest.raises(ConfigError, match=re.escape('[plugins.harness.codex] is configured, and nothing uses it')):
        load_campaign_config(campaign_path)


def test_a_table_that_moved_to_optimization_still_imports_from_db_models_with_a_warning() -> None:
    with pytest.warns(PiloopDeprecationWarning, match=r'piloop\.db\.models\.Candidate moved'):
        from piloop.db.models import Candidate as Old
    assert Old is Candidate
    with pytest.raises(ModuleNotFoundError):  # the import system asks for __path__, which is no table
        importlib.import_module('piloop.db.models.Candidate')


def test_a_campaign_still_reads_its_ideas_and_its_evaluator_with_a_warning(db: Database, campaign: Campaign) -> None:
    with db.session() as s:
        row = s.get_one(Campaign, campaign.id)
        s.add(Idea(campaign_id=row.id, title='t', hypothesis='h', created_by='human', track=TrackName('score')))
        s.flush()
        with pytest.warns(PiloopDeprecationWarning, match=r'Campaign\.evaluator moved'):
            assert row.evaluator is evaluator_of(s, row) is not None
        with pytest.warns(PiloopDeprecationWarning, match=r'Campaign\.ideas moved'):
            assert [i.title for i in row.ideas] == ['t']
    with pytest.raises(ValueError, match='in no session'), pytest.warns(PiloopDeprecationWarning):
        _ = Campaign(name='gone').ideas
