"""The deprecation framework: a bridge defined here, each bridge of ``BRIDGES``, and the ``deprecated.until`` markers.

The marker scan reads the source, so a marker in a branch that never runs counts too. Once ``pyproject.toml`` is at or
past a marker's version, ``test_no_marker_reached_its_release`` fails with the place and the reason of each one, so the
release cannot ship until the code is deleted.
"""

import ast
import io
import re
import subprocess
import sys
import tokenize
import tomllib
import warnings
from dataclasses import dataclass
from importlib.metadata import EntryPoint
from pathlib import Path

import pytest

from piloop import deprecated
from piloop.deprecated import PiloopDeprecationWarning
from piloop.ids import AgentSessionId, CampaignId, Revision, TargetRef
from piloop.plugins.campaign_type.kit.runs.ids import RUN
from piloop.plugins.executor.contract import AgentJob, RepoId, Source
from piloop.plugins.host import PluginError, Scope
from piloop.plugins.vcs.git import Git
from piloop.version import Version
from tests.conftest import AGENT_TIMEOUT, HARNESS_APPEND, write_toml
from tests.daemon.test_roles import installed
from tests.test_lease import BRIEF, START


@deprecated.bridge('9.0.0', 'size is width: delete rename_size')
def rename_size(toml: Path, raw: dict[str, object], removed_in: Version) -> None:
    """``size = …`` is ``width = …``."""
    if 'size' in raw:
        raw['width'] = raw.pop('size')
        deprecated.warn(toml, 'size', 'size is deprecated; write width instead', removed_in=removed_in)


def test_a_bridge_rewrites_the_key_and_warns_at_the_line_that_set_it(tmp_path: Path) -> None:
    toml = tmp_path / 'piloop.toml'
    toml.write_text('name = "p"\nsize = 3\n')
    raw: dict[str, object] = {'name': 'p', 'size': 3}
    with pytest.warns(PiloopDeprecationWarning) as record:
        deprecated.apply(toml, raw, [rename_size])
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


TABLES = (
    deprecated.Move('harness.*', 'plugins.harness.*'),
    deprecated.Move('model.*', 'plugins.harness.*.model'),
    deprecated.Move('max_concurrent.*', 'plugins.harness.*.max_concurrent'),
    deprecated.Move('roles.*', 'plugins.role.*'),
    deprecated.Move('harnesses', 'plugins.harness.use'),
    deprecated.Move('runtime', 'plugins.runtime.use'),
)
"""Moves of tables, of keys of each table, and of single keys, as 0.4 bridged the tables of 0.3."""


def test_a_move_moves_each_key_that_its_old_path_matches_and_the_new_path_keeps_its_own_values() -> None:
    raw: dict[str, object] = {
        'model': {'claude': 'c', 'pi': 'p'},
        'roles': {'research': {'timeout': 9, 'max_ideas': 2}, 'scout': {}},
        'plugins': {'role': {'research': {'timeout': 5}}, 'harness': {'claude': {'model': 'mine'}}},
    }
    moved = [m.apply(raw) for m in TABLES]
    assert moved == [[], ['model.claude', 'model.pi'], [], ['roles.research', 'roles.scout'], [], []]
    assert raw == {
        'plugins': {
            'role': {'research': {'timeout': 5, 'max_ideas': 2}, 'scout': {}},
            'harness': {'claude': {'model': 'mine'}, 'pi': {'model': 'p'}},
        }
    }, 'a table merges into the new one key by key, a value there wins, and an emptied old table goes'
    back: dict[str, object] = {'plugins': {'harness': {'pi': {'model': 'p', 'env': {}}}}, 'harnesses': ['pi']}
    for move in reversed(TABLES):
        move.inverse().apply(back)
    assert back == {'harness': {'pi': {'env': {}}}, 'model': {'pi': 'p'}, 'harnesses': ['pi']}
    empty: dict[str, object] = {'model': {}, 'harness': {}}
    assert [m.apply(empty) for m in TABLES] == [['harness'], ['model'], [], [], [], []]
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


@dataclass(frozen=True)
class Marker:
    where: str
    version: Version
    reason: str


def _is_marker(node: ast.Call, module: str) -> bool:
    """Whether ``node`` calls ``until`` or ``bridge``: ``x.until(…)`` or ``x.bridge(…)`` under any name ``x``, or
    ``until(…)`` or ``bridge(…)`` in their own module."""
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr in ('until', 'bridge')
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
    match node.args, node.keywords:  # a bridge's file and order are keywords
        case [ast.Constant(value=str(version)), ast.Constant(value=str(reason)), *_], _ if reason.strip():
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


@deprecated.bridge('9.0.0', 'shade is tint: delete shade', after=('campaign_finding_grace',))
def shade(toml: Path, raw: dict[str, object], removed_in: Version) -> None:
    """A package's bridge of ``piloop.toml``: ``[plugins.role.<name>] shade`` is ``tint``."""
    for role in (deprecated.table_at(raw, 'plugins', 'role') or {}).values():
        if isinstance(role, dict) and 'shade' in role:
            role['tint'] = role.pop('shade')


@deprecated.bridge('9.0.0', 'tint is hue: delete early', before=('shade',))
def early(toml: Path, raw: dict[str, object], removed_in: Version) -> None:
    """A package's bridge that runs before ``shade``."""


@deprecated.bridge('9.0.0', 'a cycle: delete first', after=('second',))
def first(toml: Path, raw: dict[str, object], removed_in: Version) -> None:
    """Runs after ``second``."""


@deprecated.bridge('9.0.0', 'a cycle: delete second', after=('first',))
def second(toml: Path, raw: dict[str, object], removed_in: Version) -> None:
    """Runs after ``first``."""


def test_a_package_s_bridge_runs_on_its_file_after_the_bridges_it_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It reads ``[plugins.role]``, which ``campaign_finding_grace`` writes to; a cycle or a missing name is refused."""
    installed(monkeypatch, [EntryPoint('shade', f'{__name__}:shade', deprecated.BRIDGES.group)])
    order = deprecated.bridges(Scope.CAMPAIGN)
    assert order.index(shade) > order.index(deprecated.campaign_finding_grace)
    assert shade not in deprecated.bridges(Scope.HOME), 'config.toml has no roles'
    raw: dict[str, object] = {'plugins': {'role': {'research': {'shade': 2}}}}
    toml = tmp_path / 'piloop.toml'
    toml.write_text('[plugins.role.research]\nshade = 2\n')
    deprecated.apply(toml, raw, deprecated.bridges(Scope.CAMPAIGN))
    assert deprecated.table_at(raw, 'plugins', 'role', 'research') == {'tint': 2}
    installed(monkeypatch, [EntryPoint(n, f'{__name__}:{n}', deprecated.BRIDGES.group) for n in ('shade', 'early')])
    order = deprecated.bridges(Scope.CAMPAIGN)
    assert order.index(early) < order.index(shade), 'a bridge can name one that runs after it'
    cycle = [EntryPoint(n, f'{__name__}:{n}', deprecated.BRIDGES.group) for n in ('first', 'second')]
    installed(monkeypatch, cycle)
    with pytest.raises(PluginError, match='in a cycle: first -> second -> first|in a cycle: second -> first -> second'):
        deprecated.bridges(Scope.CAMPAIGN)
    installed(monkeypatch, cycle[:1])
    with pytest.raises(PluginError, match='the bridge first runs after or before no bridge called second'):
        deprecated.bridges(Scope.CAMPAIGN)


def test_the_bridges_load_without_any_provider() -> None:
    """The bridges run before the providers load: a capability's bridges module imports only piloop's machinery."""
    code = (
        'import sys; from piloop import deprecated; from piloop.plugins.host import Scope;'
        ' [deprecated.bridges(s) for s in Scope];'
        " print(*sorted(m for m in sys.modules if m.startswith('piloop')))"
    )
    loaded = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, check=True).stdout.split()
    machinery = {'piloop', 'piloop.deprecated', 'piloop.version', 'piloop.plugins.host'}
    others = [m for m in loaded if m not in machinery and not m.endswith('.bridges') and not _package(m)]
    assert others == [], 'a bridges module imports a provider'


def _package(module: str) -> bool:
    return (ROOT / module.replace('.', '/') / '__init__.py').is_file()


def test_a_job_that_a_0_4_daemon_queued_captures_for_its_run_by_the_run_s_id() -> None:
    """An agent of 0.4 that is live at the upgrade keeps its job: its capture is the run's id, not a ref."""
    job = AgentJob(
        agent_session=AgentSessionId(4),
        runtime='subprocess',
        artifact=Source(vcs=Git(), repo=RepoId(CampaignId(1)), revision=Revision('c1')),
        place='runs/4',
        timeout=60,
        brief_cid=BRIEF,
        spec=START,
        capture=TargetRef(RUN, 4),
    )
    of_0_4 = job.model_dump(mode='json') | {'capture': 4}
    assert AgentJob.model_validate(of_0_4) == job
    assert AgentJob.model_validate(job.model_dump(mode='json')) == job, 'a job of this piloop is read as it is'
