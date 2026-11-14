"""The plugin host: an installed package mounts providers through it, and a composition that fails registers nothing."""

import re
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from dataclasses import dataclass, replace
from importlib.metadata import EntryPoint
from pathlib import Path
from typing import IO, Any

import piloop_min_gain
import pytest
from piloop_nice import Nice, NiceRuntime
from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy.orm import Session

from piloop.cli import common, doctor
from piloop.cli.main import app
from piloop.cli.plugins import selections
from piloop.config import CampaignConfig, ConfigError, load_campaign_config
from piloop.daemon.main import Daemon
from piloop.db.models import Campaign
from piloop.db.session import Database
from piloop.plugins import host as plugins
from piloop.plugins.campaign_type.kit.pool.best_rule.baseline import Baseline
from piloop.plugins.campaign_type.kit.pool.best_rule.contract import BEST_RULES, BestRule, Case, Criterion, Scores
from piloop.plugins.cas.contract import CAS
from piloop.plugins.executor.workers import Workers
from piloop.plugins.harness.claude import Claude
from piloop.plugins.harness.contract import HARNESSES, HarnessName
from piloop.plugins.host import (
    Capability,
    Check,
    ContractBroken,
    Instances,
    Many,
    MissingProvider,
    One,
    Parameters,
    Pick,
    PluginError,
    PythonSource,
    Reference,
    Scope,
    Tier,
    Use,
    compose,
    data_errors,
    is_default,
    refs,
)
from piloop.plugins.metric import Direction
from piloop.plugins.role.contract import ROLES
from piloop.plugins.runtime.contract import RUNTIMES, RuntimeName, make_runtime, runtimes
from piloop.plugins.runtime.subprocess import Subprocess
from piloop.plugins.section.contract import SECTIONS
from piloop.settings import HOME_PLUGINS, Settings
from tests.conftest import PLUGIN, example, forget_loaded, install_plugins, package
from tests.fixtures.plugin import AsMeasured, Checked, Needy, Typed
from tests.test_docs import BASE_CONFIG


@pytest.fixture(autouse=True)
def unloaded() -> Iterator[None]:
    """Start and end each test with nothing loaded, so that the entry points of the test are the ones loaded."""
    forget_loaded()
    yield
    forget_loaded()


def test_an_installed_package_mounts_its_runtime_and_shows_where_it_comes_from(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    example(tmp_path, 'nice-runtime')
    monkeypatch.syspath_prepend(tmp_path)
    assert RUNTIMES.get('nice').model is Nice
    assert isinstance(make_runtime('nice'), NiceRuntime)
    assert RUNTIMES.sources()['nice'] == PythonSource('piloop-nice', '0.1.0', 'nice = piloop_nice:Nice')
    built_in = RUNTIMES.sources()['subprocess']
    assert (built_in.release.split()[0], built_in.built_in) == ('piloop', True)
    assert next(c for c in doctor.checks(Settings(home=tmp_path)) if c.name == 'plugins').found.endswith(
        '; from piloop-nice 0.1.0'
    )
    monkeypatch.setattr(common, 'settings', Settings(home=tmp_path))
    printed: list[str] = []
    monkeypatch.setattr(common.console, 'print', lambda text, **_: printed.append(text))
    app(['plugins'], result_action='return_value', exit_on_error=False)
    lines = printed[0].splitlines()
    runtimes = lines.index('runtimes (host tier, piloop.runtimes):')
    assert lines[runtimes : runtimes + 4] == [
        'runtimes (host tier, piloop.runtimes):',
        f'  docker      {built_in}',
        f'  subprocess  {built_in}',
        '  nice        piloop-nice 0.1.0, entry point nice = piloop_nice:Nice',
    ]
    assert lines[-1] == 'selected:', 'no campaign and no remote: nothing is selected'


def test_the_runtimes_of_config_toml_hold_every_runtime_with_its_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    example(tmp_path, 'nice-runtime')
    monkeypatch.syspath_prepend(tmp_path)
    plugins = HOME_PLUGINS.validate_python({'runtime': {'nice': {'niceness': 5}}})
    tables = RUNTIMES.instances(plugins)
    assert tables == {'subprocess': Subprocess(), 'nice': Nice(niceness=5)}, 'every runtime, over its defaults'
    assert HOME_PLUGINS.validate_python(plugins) == plugins
    assert HOME_PLUGINS.dump_python(plugins) == {
        'runtime': {'subprocess': {}, 'nice': {'niceness': 5}},
    }
    home = Settings(home=tmp_path, plugins={'runtime': {'fast': {'provider': 'nice', 'niceness': 1}}})
    assert runtimes(home)['fast'] == Nice(niceness=1), 'a table with a new name and a provider'
    fast = make_runtime('fast', home)
    assert isinstance(fast, NiceRuntime) and fast.name == 'fast', 'the runtime reports the name of its instance'
    with pytest.raises(MissingProvider, match="no runtime 'slow' on this machine; there are: subprocess, nice, fast"):
        make_runtime('slow', home)
    with pytest.raises(ValidationError, match=r'\nruntime\.nice\.niceness\n  Input should be a valid integer'):
        HOME_PLUGINS.validate_python({'runtime': {'nice': {'niceness': 'low'}}})
    with pytest.raises(ValidationError, match='a table of tables'):
        HOME_PLUGINS.validate_python({'runtime': 3})


def test_two_packages_with_the_same_name_fail_before_the_daemon_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package(tmp_path / 'a', 'piloop-a', '1.0', f'[piloop.runtimes]\nshown = {PLUGIN}:Shown\n')
    package(tmp_path / 'b', 'piloop-b', '2.0', f'[piloop.runtimes]\nshown = {PLUGIN}:Shown\n')
    monkeypatch.syspath_prepend(tmp_path / 'b')
    monkeypatch.syspath_prepend(tmp_path / 'a')
    with pytest.raises(
        SystemExit,
        match=f"^the daemon does not start: entry point shown = {PLUGIN}:Shown in piloop.runtimes: 'shown'"
        f' is a runtime already, from piloop-a 1.0, entry point shown = {PLUGIN}:Shown$',
    ):
        Daemon(Settings(home=tmp_path / 'home'))
    assert not (tmp_path / 'home').exists(), 'it failed before it opened anything'
    assert not any(c.active for c in plugins.declared())
    found = doctor.checks(Settings(home=tmp_path / 'home'))
    names = [c.name.split(' ')[0] for c in found]
    assert names[:4] == ['cli', 'daemon', 'database', 'plugins'] and set(names[4:]) <= {'skills', 'git'}, (
        'the checks of campaigns, harnesses and runtimes, which use the plugins, do not run'
    )
    found = [c for c in found if c.name == 'plugins']
    assert found[-1].fix is not None and "'shown' is a runtime already" in found[-1].found
    monkeypatch.setattr(common, 'settings', Settings(home=tmp_path / 'home'))
    with pytest.raises(SystemExit, match="'shown' is a runtime already"):
        app(['plugins'], result_action='return_value', exit_on_error=False)


@pytest.mark.parametrize(
    ('config', 'error'),
    [
        (
            '[plugins.runtime.tmux]\nx = 1\n',
            r'config.toml: 1 validation error for \[plugins\]\nruntime\.tmux\n  Value error, no runtime has that name: add'
            r' provider = "<runtime>"; registered runtimes: docker, subprocess',
        ),
        (
            '[plugins.cas]\nprovider = "azure"\n',
            r"config.toml: 1 validation error for \[plugins\]\ncas\n  Value error, provider = 'azure': unknown CAS"
            r" provider 'azure'; registered CAS providers",
        ),
        (
            '[plugins.cas]\nprovider = "s3"\n',
            r'config.toml: 1 validation error for \[plugins\]\ncas.url\n  Field required',
        ),
    ],
)
def test_config_toml_that_binds_a_provider_that_there_is_not_fails_before_the_daemon_starts(
    tmp_path: Path, config: str, error: str
) -> None:
    (tmp_path / 'config.toml').write_text(config)
    with pytest.raises(SystemExit, match=f'^the daemon does not start: .*{error}'):
        Daemon(Settings(home=tmp_path))
    assert not (tmp_path / 'piloop.db').exists()
    assert next(c for c in doctor.checks(Settings(home=tmp_path)) if c.name == 'plugins').fix is not None


def test_an_entry_point_that_does_not_load_fails_with_its_name_and_the_cause(monkeypatch: pytest.MonkeyPatch) -> None:
    install_plugins(monkeypatch, [EntryPoint('gone', 'tests.fixtures.no_such_module:Gone', RUNTIMES.group)])
    with pytest.raises(
        PluginError,
        match=r'^entry point gone = tests.fixtures.no_such_module:Gone in piloop.runtimes does not load:'
        r" ModuleNotFoundError: No module named 'tests.fixtures.no_such_module'$",
    ) as failed:
        RUNTIMES.providers()
    assert isinstance(failed.value.__cause__, ModuleNotFoundError)


def test_a_failed_composition_unregisters_what_it_activated_and_keeps_what_was_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken role unwinds the runtimes and the sections of the same call, and leaves the remotes, active before."""
    typed = EntryPoint('typed', f'{PLUGIN}:Typed', RUNTIMES.group)
    install_plugins(monkeypatch, [typed, EntryPoint('auditor', f'{PLUGIN}:NOT_A_ROLE', ROLES.group)])
    CAS.providers()
    with pytest.raises(PluginError, match='is not a subclass of piloop.plugins.role.contract.RoleConfig'):
        compose(*declared('runtime', 'section', 'role'))
    assert [c.active for c in (RUNTIMES, SECTIONS, ROLES, CAS)] == [False, False, False, True]
    install_plugins(monkeypatch, [typed])
    compose(*declared('runtime', 'section', 'role'))
    assert [c.active for c in (RUNTIMES, SECTIONS, ROLES)] == [True, True, True]
    assert RUNTIMES.get('typed').model is Typed
    plugins.close()
    plugins.close()
    assert not any(c.active for c in plugins.declared()), 'closing twice is harmless'
    assert 'typed' in RUNTIMES.providers(), 'the next use activates again'


def declared(*names: str) -> list[Capability[Parameters]]:
    """The capabilities called ``names``, in that order."""
    by_name = {c.name: c for c in plugins.declared()}
    return [by_name[name] for name in names]


def test_a_broken_package_of_a_capability_that_nothing_binds_is_a_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no ``[plugins.extra]``, the daemon starts without the extras that a plugin declares: one that does not load
    warns."""
    install_plugins(
        monkeypatch,
        [
            EntryPoint('extra', f'{__name__}:EXTRAS', plugins.CAPABILITIES),
            EntryPoint('gone', 'tests.fixtures.no_such_module:Gone', EXTRAS.group),
        ],
    )
    home = Settings(home=tmp_path)
    Daemon(home).shutdown(timeout=5)
    found = next(c for c in doctor.checks(home) if c.name == 'plugins')
    assert found.fix is None
    assert found.found.partition('; all built in; ')[2] == (
        'warning: nothing selects a extra, and they do not load:'
        ' entry point gone = tests.fixtures.no_such_module:Gone in piloop.example.extras does not load: ModuleNotFoundError:'
        " No module named 'tests.fixtures.no_such_module'"
    )
    monkeypatch.setattr(common, 'settings', home)
    printed: list[str] = []
    monkeypatch.setattr(common.console, 'print', lambda text, **_: printed.append(text))
    app(['plugins'], result_action='return_value', exit_on_error=False)
    lines = printed[0].splitlines()
    shown = lines.index('extras (host tier, piloop.example.extras):') + 1
    assert lines[shown].startswith('  not loaded, and nothing selects a extra: entry point gone = ')
    (tmp_path / 'config.toml').write_text('[plugins.extra]\nprovider = "gone"\n')
    with pytest.raises(SystemExit, match='the daemon does not start: entry point gone = '):
        Daemon(Settings(home=tmp_path))


def test_a_name_that_no_provider_has_lists_the_ones_there_are() -> None:
    with pytest.raises(MissingProvider, match="^unknown runtime 'tmux'; registered runtimes: docker, subprocess$"):
        RUNTIMES.get('tmux')


class Reentrant(EntryPoint):
    """An entry point whose import needs the sections that it is one of."""

    def load(self) -> object:
        return SECTIONS.providers()


def test_an_entry_point_that_needs_its_own_capability_while_it_loads_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    install_plugins(monkeypatch, [Reentrant('loop', f'{PLUGIN}:Typed', SECTIONS.group)])
    with pytest.raises(
        PluginError,
        match=rf'^the sections are needed while they load: an entry point of {re.escape(SECTIONS.group)} uses its own'
        r' capability, or one that uses it, when it is imported$',
    ):
        SECTIONS.providers()
    assert not any(c.active for c in plugins.declared())


class Fake(Capability[Parameters]):
    """A capability that records when it activates and when its registration is undone."""

    def __init__(self, name: str, log: list[str], *, fails: bool = False) -> None:
        super().__init__(name, Parameters)
        self.log = log
        self.fails = fails
        self.on = False

    @property
    def active(self) -> bool:
        return self.on

    def activate(self, stack: ExitStack) -> None:
        if self.fails:
            raise PluginError(f'{self.name} fails')
        self.on = True
        self.log.append(f'+{self.name}')
        stack.callback(self._undo)

    def _undo(self) -> None:
        self.on = False
        self.log.append(f'-{self.name}')


def test_activation_goes_in_order_teardown_newest_first_and_a_failure_undoes_only_its_own_call() -> None:
    log: list[str] = []
    store, runtime, planner = Fake('store', log), Fake('runtime', log), Fake('planner', log)
    compose(store, runtime, planner)
    plugins.close()
    assert log == ['+store', '+runtime', '+planner', '-planner', '-runtime', '-store']
    log.clear()
    compose(store)
    with pytest.raises(PluginError, match='broken fails'):
        compose(runtime, Fake('broken', log, fails=True))
    assert log == ['+store', '+runtime', '-runtime'], 'only what the failed call activated is undone'
    plugins.close()
    assert log[-1] == '-store'


CASE = Case(
    Criterion('score', Direction.HIGHER),
    (),
    Scores('candidate#2', {'score': 13.0}, 1, None),
    Scores('candidate#1', {'score': 10.0}, 1, None),
    None,
)
"""A case of the best rules, the pure capability that these tests install a provider of."""


def test_a_pure_plugin_is_installed_selected_by_config_and_answers_with_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, db: Database, campaign: Campaign
) -> None:
    """The example ``examples/plugins/min-gain`` provides a best rule, and ``[plugins.best_rule]`` selects it."""
    campaign_path = Path(campaign.path)
    example(tmp_path / 'site', 'min-gain')
    monkeypatch.syspath_prepend(tmp_path / 'site')
    forget_loaded()  # the campaign's config loaded the best rules before the example was installed
    assert BEST_RULES.tier is Tier.PURE and data_errors(BestRule) == []
    cfg = load_campaign_config(campaign_path, plugins={'best_rule': {'provider': 'min_gain', 'min_gain': 2}})
    rule = BEST_RULES.selected(cfg.plugins)
    assert rule == piloop_min_gain.MinGain(min_gain=2)
    assert rule.refuse(CASE) is None, 'a gain of 3'
    assert piloop_min_gain.MinGain(min_gain=5).refuse(CASE) == 'its gain 3 is below min_gain 5'
    assert BEST_RULES.sources()['min_gain'].release == 'piloop-min-gain 0.1.0'
    home = Settings(home=tmp_path)
    assert not any(line.startswith('demo: best rule min_gain') for line in selections(home)), 'not selected yet'
    with db.session() as s:
        s.get_one(Campaign, campaign.id).config = cfg
        s.commit()
    assert (
        'demo: best rule min_gain, from piloop-min-gain 0.1.0 ([plugins.best_rule] provider, in the'
        f' config of demo that the last `piloop init` stored from {campaign_path}/piloop.toml)'
    ) in selections(home)
    plain = load_campaign_config(campaign_path, plugins={'best_rule': {'provider': 'baseline'}})
    assert plain.plugins['best_rule'] == Baseline(), 'a rule without fields, given none'
    assert BEST_RULES.selected({}) == Baseline(), 'the default'
    for tables, error in [
        (
            {'best_rule': {'provider': 'nope'}},
            "plugins.best_rule: provider = 'nope': unknown best rule 'nope'",
        ),
        ({'best_rule': {'provider': 'min_gain', 'min_gain': 'x'}}, "min_gain = 'x': Input should be a valid number"),
        ({'best_rule': {'provider': 'min_gain'}}, 'min_gain: Field required'),
        (
            {'planner': {'provider': 'min_gain'}},
            'plugins.planner: no capability has that name; there is best_rule, escalation, fact_source, harness, harness_choice, lineage, liveness, mcp_server, notifier, price_list, role, runtime, section, stop_rule, vcs',
        ),
        (
            {'best_rule': {'provider': 'baseline', 'min_gain': 2}},
            'plugins.best_rule.min_gain = 2: Extra inputs are not permitted',
        ),
        ({'best_rule': 1}, 'plugins.best_rule = 1: not a table'),
    ]:
        with pytest.raises(ConfigError, match=re.escape(error)):
            load_campaign_config(campaign_path, plugins=tables)


@pytest.mark.parametrize(
    ('value', 'error'),
    [
        (
            f'{PLUGIN}:NOT_A_ROLE',
            'is not a subclass of piloop.plugins.campaign_type.kit.pool.best_rule.contract.BestRule$',
        ),
        (
            f'{PLUGIN}:Unrefusing',
            'does not implement piloop.plugins.campaign_type.kit.pool.best_rule.contract.BestRule: it has no refuse$',
        ),
        (
            'piloop.plugins.stop_rule.limits:Limits',
            'is not a subclass of piloop.plugins.campaign_type.kit.pool.best_rule.contract',
        ),
    ],
)
def test_a_provider_is_a_class_that_implements_its_contract(
    monkeypatch: pytest.MonkeyPatch, value: str, error: str
) -> None:
    install_plugins(monkeypatch, [EntryPoint('odd', value, BEST_RULES.group)])
    with pytest.raises(PluginError, match=f'^entry point odd = {value} in piloop.best_rules {error}'):
        BEST_RULES.providers()


@dataclass(frozen=True)
class Loop:
    next: Loop | None


class Open(BaseModel):
    """A record that takes any extra field, of any type."""

    model_config = ConfigDict(extra='allow')
    x: int


class Plain:
    """A class that is not a record."""


class Leaky(Parameters, ABC):
    """A contract that passes live objects, and so cannot be served out of process."""

    @property
    @abstractmethod
    def session(self) -> Session: ...

    @staticmethod
    @abstractmethod
    def helper(s: Session) -> None: ...

    @classmethod
    @abstractmethod
    def build(cls, s: Session) -> None: ...

    @abstractmethod
    def run(
        self, thing: object, anything: list[Any], then: Callable[[int], int], log: IO[str], plain: Plain
    ) -> None: ...

    @abstractmethod
    def nested(self, open: Open) -> Loop: ...

    @abstractmethod
    def loose(self, x, *args: int) -> int: ...

    @abstractmethod
    def untyped(self, x: int): ...


def test_a_pure_contract_takes_and_returns_data_only() -> None:
    anything = 'has a part of any type (Any, object, or a model that allows extra fields)'
    no_schema = 'is not data: Unable to generate pydantic-core schema for'
    assert data_errors(Leaky) == [
        'session is not a method: a pure contract has methods only, as a WIT interface',
        f"build(s): <class 'sqlalchemy.orm.session.Session'> {no_schema} <class 'sqlalchemy.orm.session.Session'>."
        ' Set `arbitrary_types_allowed=True` in the model_config to ignore this error or implement'
        ' `__get_pydantic_core_schema__` on your type to fully support it.',
        f"helper(s): <class 'sqlalchemy.orm.session.Session'> {no_schema} <class 'sqlalchemy.orm.session.Session'>."
        ' Set `arbitrary_types_allowed=True` in the model_config to ignore this error or implement'
        ' `__get_pydantic_core_schema__` on your type to fully support it.',
        'loose(x) has no type',
        'loose(args) takes any number of arguments',
        f'nested(open): {Open!r} {anything}',
        f'nested returns {Loop!r} contains itself (Loop → Loop), and WIT types are not recursive',
        f"run(thing): <class 'object'> {anything}",
        f'run(anything): list[typing.Any] {anything}',
        'run(then): collections.abc.Callable[[int], int] is not data: Cannot generate a JsonSchema for'
        ' core_schema.CallableSchema',
        f'run(log): typing.IO[str] {no_schema} typing.IO[str]. Set `arbitrary_types_allowed=True` in the model_config'
        ' to ignore this error or implement `__get_pydantic_core_schema__` on your type to fully support it.',
        f'run(plain): {Plain!r} {no_schema} {Plain!r}. Set'
        ' `arbitrary_types_allowed=True` in the model_config to ignore this error or implement'
        ' `__get_pydantic_core_schema__` on your type to fully support it.',
        'untyped has no return type',
    ]
    with pytest.raises(TypeError, match=r'^Leaky is not a pure contract: session is not a method'):
        Capability('leak', Leaky, tier=Tier.PURE)


def test_a_plugin_declares_a_capability_calls_its_providers_and_another_package_provides_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, campaign: Campaign
) -> None:
    """A package declares ``best_check`` and calls its providers from its best rule; another provides one.
    piloop lists, loads and checks it as one of its own."""
    campaign_path = Path(campaign.path)
    install_plugins(
        monkeypatch,
        [
            EntryPoint('best_check', f'{PLUGIN}:BEST_CHECKS', plugins.CAPABILITIES),
            EntryPoint('as_measured', f'{PLUGIN}:AsMeasured', 'piloop.best_checks'),
            EntryPoint('checked', f'{PLUGIN}:Checked', BEST_RULES.group),
        ],
    )
    checks = next(c for c in plugins.declared() if c.name == 'best_check')
    assert (checks.tier, checks.group, checks.table) == (Tier.PURE, 'piloop.best_checks', None)
    with pytest.raises(ConfigError, match=re.escape("unknown best check 'none'; registered best checks: as_measured")):
        none = {'provider': 'checked', 'vetoes': [{'provider': 'none'}]}
        load_campaign_config(campaign_path, plugins={'best_rule': none})
    table = {'provider': 'checked', 'vetoes': [{'provider': 'as_measured'}]}
    rule = BEST_RULES.selected(load_campaign_config(campaign_path, plugins={'best_rule': table}).plugins)
    assert rule == Checked(vetoes=[AsMeasured()])
    assert rule.refuse(CASE) is None, 'as many evaluations as the baseline'
    fewer = replace(CASE, baseline=replace(CASE.baseline, measured=3))
    assert rule.refuse(fewer) == 'as_measured: candidate#2 has 1 evaluations, the baseline 3'
    floored = replace(fewer, primary=Criterion('score', Direction.HIGHER, floor=20))
    assert rule.refuse(floored) == 'floor: score 13 past its floor 20', "baseline's reason first"
    monkeypatch.setattr(common, 'settings', Settings(home=tmp_path))
    printed: list[str] = []
    monkeypatch.setattr(common.console, 'print', lambda text, **_: printed.append(text))
    app(['plugins'], result_action='return_value', exit_on_error=False)
    lines = printed[0].splitlines()
    listed = lines.index('best checks (pure tier, piloop.best_checks):')
    assert lines[listed + 1].split()[:3] == ['as_measured', 'an', 'unknown']


STORES_AGAIN = Capability('odd', BestRule, group='piloop.cas')
"""A capability whose provider group the CAS providers have already."""


class Extra(Parameters):
    """The contract of a capability of the home that a plugin declares."""


EXTRAS = Capability('extra', Extra, table=One(Scope.HOME), group='piloop.example.extras')
"""A capability of the home that a plugin declares, which nothing selects by default."""


class Bell(Parameters):
    """The contract of a capability of the home with instances, and a ``use`` in ``config.toml`` too."""


class Ding(Bell):
    """A bell that needs no keys."""


BELLS = Capability(
    'bell', Bell, table=Many(Scope.HOME, use=Use(Scope.HOME, Pick.ORDERED)), group='piloop.example.bells'
)


def test_config_toml_refuses_an_instance_that_no_use_names_and_a_use_that_names_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_plugins(
        monkeypatch,
        [
            EntryPoint('bell', f'{__name__}:BELLS', plugins.CAPABILITIES),
            EntryPoint('ding', f'{__name__}:Ding', BELLS.group),
        ],
    )
    unused = Settings(home=tmp_path, plugins={'bell': {'loud': {'provider': 'ding'}}})
    with pytest.raises(ValueError, match=r'\[plugins.bell.loud\] is configured, and nothing uses it: add it to'):
        _ = unused.providers
    typo = Settings(home=tmp_path, plugins={'bell': {'use': ['dong']}})
    with pytest.raises(ValueError, match=r"bell.use\[0\] = 'dong': no \[plugins.bell.dong\]; there are ding"):
        _ = typo.providers
    assert BELLS.used(Settings(home=tmp_path, plugins={'bell': {'use': ['ding']}}).providers) == ('ding',)


@pytest.mark.parametrize(
    ('value', 'error'),
    [
        ('tests.fixtures.no_such_module:X', 'does not load: ModuleNotFoundError'),
        (f'{PLUGIN}:NOT_A_ROLE', 'is not a piloop.plugins.host.Capability$'),
        (f'{__name__}:STORES_AGAIN', 'the CAS providers have the provider group piloop.cas already'),
        (f'{PLUGIN}:BEST_CHECKS', "names the capability 'best_check': the names must be the same$"),
    ],
)
def test_a_declared_capability_loads_is_a_capability_and_has_a_group_of_its_own(
    monkeypatch: pytest.MonkeyPatch, value: str, error: str
) -> None:
    """Each entry point of ``piloop.capabilities`` names one capability; the next one with its group is refused."""
    install_plugins(monkeypatch, [EntryPoint('odd', value, plugins.CAPABILITIES)])
    with pytest.raises(PluginError, match=f'^entry point odd = {re.escape(value)} in piloop.capabilities.*{error}'):
        plugins.declared()


class Unregistered(Baseline):
    """A best rule that no entry point names."""


def test_a_capability_selects_the_table_of_its_file_else_its_default_else_nothing() -> None:
    workers = Workers(renew_seconds=5, grace_seconds=40)
    assert HOME_PLUGINS.validate_python({'executor': workers}) == {
        'executor': workers,
        'runtime': Instances(RUNTIMES, (), {'subprocess': Subprocess()}),
    }, 'a capability of several instances has those of its providers that need no keys, and no use in config.toml'
    with pytest.raises(ValidationError, match=r'\[plugins\] is a table of tables'):
        HOME_PLUGINS.validate_python(1)
    with pytest.raises(MissingProvider, match=r'^Unregistered is no best rule of an entry point$'):
        BEST_RULES.name_of(Unregistered())
    chosen = Capability('odd', Parameters, table=One(Scope.CAMPAIGN))
    with pytest.raises(MissingProvider, match=r'^no \[plugins.odd\] chooses a odd, and there is no default$'):
        chosen.selected({})
    with pytest.raises(ValueError, match='^provider: missing; registered odds: $'):
        chosen.validated({})


def test_only_the_class_that_sets_default_is_a_default_and_a_campaign_has_one_default_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A subclass of a default provider inherits its code, not its place among the defaults."""
    install_plugins(monkeypatch, [EntryPoint('typed', f'{PLUGIN}:Typed', RUNTIMES.group)])
    assert (is_default(Subprocess), is_default(Typed)) == (True, False)
    assert RUNTIMES.used({}) == ('subprocess',), 'Typed subclasses Subprocess, and is no second default'
    assert HARNESSES.used({}) == ('claude', 'codex')
    install_plugins(monkeypatch, [EntryPoint('also', f'{PLUGIN}:AlsoDefault', RUNTIMES.group)])
    with pytest.raises(ContractBroken, match='^one installed runtime is DEFAULT, not 2: subprocess, also$'):
        RUNTIMES.used({})
    install_plugins(monkeypatch, [])
    monkeypatch.setattr(Subprocess, 'DEFAULT', False)
    with pytest.raises(ContractBroken, match='^one installed runtime is DEFAULT, not 0: none$'):
        RUNTIMES.used({})
    forget_loaded()


class Inner(BaseModel):
    runtime: RuntimeName


class Referring(BaseModel):
    """A model with references at every depth that ``refs`` walks."""

    one: HarnessName
    maybe: HarnessName | None = None
    rotation: tuple[HarnessName, ...] = ()
    by: dict[HarnessName, list[RuntimeName]] = {}
    inner: Inner | None = None
    plain: str = 'not a reference'


def test_refs_finds_each_name_of_an_instance_and_where_it_is() -> None:
    model = Referring(
        one='pi', rotation=('claude', 'codex'), by={'pi': ['subprocess']}, inner=Inner(runtime='subprocess')
    )
    found = [(r.capability.name, r.name, r.at) for r in refs(model)]
    assert found == [
        ('harness', 'pi', ('one',)),
        ('harness', 'claude', ('rotation', 0)),
        ('harness', 'codex', ('rotation', 1)),
        ('harness', 'pi', ('by',)),
        ('runtime', 'subprocess', ('by', 'pi', 0)),
        ('runtime', 'subprocess', ('inner', 'runtime')),
    ]
    plugins = {
        'harness': {'claude': {'model': 'm'}, 'codex': {'model': 'm'}},
        'role': {'implementer': {'model': {'claude': 'w'}}},
    }
    roles = refs(CampaignConfig.model_validate({**BASE_CONFIG, 'plugins': plugins}))
    assert ('harness', 'claude', ('plugins', 'role', 'implementer', 'model')) in [
        (r.capability.name, r.name, r.at) for r in roles
    ], 'the instances of [plugins] too'


def test_the_instances_of_a_capability_of_several_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    eu = HARNESSES.validated_instances({'eu': {'provider': 'claude', 'model': 'm'}, 'claude': Claude(model='c')})
    assert (type(eu['eu']), eu['eu'].model, eu['claude'].model) == (Claude, 'm', 'c'), (
        'a new name with a provider is that provider; an instance is itself'
    )
    assert HARNESSES.dumped_instance('eu', eu['eu'], 'json') == {'provider': 'claude', 'model': 'm'}
    assert HARNESSES.dumped_instance('claude', eu['claude'], 'json') == {'model': 'c'}, 'its own name: no provider'
    plain = HARNESSES.validated_instances({'plain': {'executable': 'x', 'headless': ['{prompt}']}})['plain']
    assert HARNESSES.dumped_instance('plain', plain, 'json') == {'executable': 'x', 'headless': ['{prompt}']}
    with pytest.raises(ValueError, match='^a table of tables, one for each instance by its name$'):
        HARNESSES.validated_instances(3)
    with pytest.raises(ValidationError, match=r'\nx\n  Value error, a harness provider is a table'):
        HARNESSES.validated_instances({'x': 3})
    with pytest.raises(ContractBroken, match=r'^\[plugins.harness\] is not a table of harness providers$'):
        HARNESSES.instances({'harness': 3})
    assert list(HARNESSES.instances({})) == ['claude', 'codex', 'pi'], 'unvalidated: the providers with their defaults'
    with pytest.raises(ContractBroken, match=r'^the roles have no \[plugins.role\] use$'):
        ROLES.used({})
    with pytest.raises(ContractBroken, match='^the best rules are not of several instances$'):
        BEST_RULES.instances({})
    with pytest.raises(ValidationError, match='use of the runtimes is not set in this file'):
        HOME_PLUGINS.validate_python({'runtime': {'use': 'subprocess'}})
    install_plugins(monkeypatch, [EntryPoint('needy', f'{PLUGIN}:Needy', RUNTIMES.group)])
    assert list(RUNTIMES.instances(HOME_PLUGINS.validate_python({}))) == ['subprocess'], (
        'a provider that needs keys is an instance only through a table'
    )
    needy = RUNTIMES.instances(HOME_PLUGINS.validate_python({'runtime': {'needy': {'level': 2}}}))
    assert needy['needy'] == Needy(level=2)
    forget_loaded()


def test_a_missing_extra_says_how_to_install_it() -> None:
    assert plugins.extra('modal', 'modal', 'modal') == Check('modal', 'modal is installed')
    assert plugins.extra('gcs', 'no_such_package.storage', 'gcs') == Check(
        'gcs', 'no_such_package.storage is not installed', "`uv tool install --reinstall 'piloop[gcs]'`"
    )


def test_in_use_is_the_one_instance_each_named_instance_and_what_they_hold() -> None:
    home = HOME_PLUGINS.validate_python(
        {'cas': {'provider': 'cached', 'remote': {'provider': 'http', 'url': 'https://cas.example.com'}}}
    )
    found = [type(p).__name__ for p in plugins.in_use(home, Scope.HOME, [])]
    assert found[found.index('Cached') :][:3] == ['Cached', 'Http', 'Disk'], 'the remote and the local tier follow it'
    assert 'Subprocess' not in found, 'no campaign names the runtime'
    named = [Reference(RUNTIMES, 'subprocess', ('plugins', 'runtime', 'use'))]
    assert 'Subprocess' in [type(p).__name__ for p in plugins.in_use(home, Scope.HOME, named)]
