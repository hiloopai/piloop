"""``piloop.capabilities``: the one module that a plugin imports, complete and without stale names.

Complete: every capability that piloop declares, its built-in providers, and every type of piloop that a contract
names. The types of ``piloop.db`` are the database's own contract, so the search stops there.
"""

import ast
import inspect
import re
import subprocess
import sys
import types
import typing
from annotationlib import Format
from collections.abc import Iterable
from importlib.metadata import EntryPoint, entry_points
from pathlib import Path
from typing import get_type_hints

import piloop_inference.min_speedup
import piloop_inline
import piloop_keep_cost
import piloop_min_gain
import piloop_nice
import piloop_sqlite_cas
import piloop_stop_after_keeps
from sqlalchemy.orm import Session

from piloop import capabilities
from piloop.db import models
from piloop.plugins import metric
from piloop.plugins.fact_source import facts
from piloop.plugins.host import BUILT_IN, CAPABILITIES, Capability, Parameters, Tier
from piloop.plugins.runtime.contract import NotSteerable
from tests.fixtures import route_plugin

HELPERS: tuple[object, ...] = (  # a tuple: some are values that do not hash, such as GO
    capabilities.EventModel,  # what a provider's events subclass, as an executor's capacity_events
    capabilities.Base,  # what a package's tables subclass (piloop.schemas)
    capabilities.Schema,  # what a package declares its tables in (piloop.schemas)
    capabilities.Parameters,  # what every contract subclasses
    capabilities.MissingProvider,
    capabilities.Live,
    NotSteerable,
    capabilities.check_cas,
    capabilities.ContractBroken,
    capabilities.integrity,
    capabilities.UNBOUNDED,
    capabilities.NoCredential,
    capabilities.NotEnrolled,
    capabilities.StartRefused,
    capabilities.check_provisioning,
    capabilities.McpError,  # what a harness's MCP renderer raises
    capabilities.NotDelivered,  # what a notifier's send raises
    capabilities.ToHuman,  # what an escalation provider's reach returns: a union of contract types
    capabilities.Listens,  # a notifier's optional protocol, what it is given, and what it hears and answers
    capabilities.ListenHost,
    capabilities.Inbound,
    capabilities.Act,
    capabilities.Reply,
    capabilities.Ack,
    capabilities.FileSeed,
    capabilities.Edit,
    capabilities.Receipt,
    capabilities.Accepted,
    capabilities.Again,
    capabilities.Declined,
    capabilities.Foreign,
    capabilities.SocketListener,  # what slack_app's listen returns, for a provider that extends it
    capabilities.UsageError,  # what a harness's usage reader raises
    capabilities.JsonlSessions,  # the kit: a JSONL session file that a harness provider may read
    capabilities.Counts,
    capabilities.Record,
    capabilities.Tally,
    capabilities.Said,
    capabilities.LocalProvider,  # what a local tier's provider and store are, and what eviction sees
    capabilities.LocalCas,
    capabilities.Evictable,
    capabilities.Held,
    capabilities.DiskCas,
    capabilities.ObjectsCas,  # the kit: a store of objects that a provider may use
    capabilities.StreamCas,
    capabilities.Objects,
    capabilities.Dir,
    capabilities.CachedCas,
    capabilities.Upkeep,  # a store's optional protocol, and what it returns
    capabilities.TierUsage,
    capabilities.Flushed,
    capabilities.Integrity,
    capabilities.BlobAddress,  # the type of a blob's address in the data that contracts pass
    capabilities.number,
    capabilities.GO,
    capabilities.key_of,
    capabilities.Settled,
    capabilities.AgentReport,  # type aliases, which the search of classes does not find by itself
    capabilities.Key,
    capabilities.RepoId,  # a NewType, which neither
    capabilities.AgentJob,  # core's own kind of job, and its report: Job and Report are their bases
    capabilities.AgentStart,
    capabilities.Resume,
    capabilities.AgentFinished,
    capabilities.AgentNotStarted,
    capabilities.MissingBlobs,
    capabilities.local_site,
    capabilities.Machine,
    capabilities.run_agent,
    capabilities.job_kinds,  # what an executor dispatches the jobs of another kind with
    capabilities.kind_of,
    capabilities.EvaluationJob,  # the evaluation job kind's, which an executor of 0.3.1 named
    capabilities.EvaluationReport,
    capabilities.Sample,
    capabilities.Bar,
    capabilities.Rung,
    capabilities.Tolerance,
    capabilities.Gate,
    capabilities.run_evaluation,
    capabilities.kill_group,
    capabilities.Leases,  # the kit: helpers that an executor may use
    capabilities.Timing,
    capabilities.Grant,
    capabilities.Answer,
    capabilities.StaleLease,
    capabilities.WorkersExecutor,  # what a built-in provider's open makes, which a plugin's may wrap
    capabilities.SubprocessRuntime,
    capabilities.TriggerName,  # the type of a role's triggers, which a plugin's role declares
    capabilities.SectionName,  # the type of a list of sections, which a plugin's role declares
    capabilities.Block,  # type aliases of the blocks that a section returns
    capabilities.Value,
    capabilities.FactValue,  # a type alias
    capabilities.HttpsNetwork,
    capabilities.Revision,  # a NewType
    capabilities.Fetch,  # type aliases
    capabilities.VcsError,  # what a vcs provider raises
    capabilities.NotARepository,
    capabilities.UnknownRevision,
    capabilities.CloneFailed,
    capabilities.GitRepository,  # what a built-in provider's open makes
    capabilities.job_secrets,  # what an executor calls at an agent's start, and what it answers
    capabilities.JobSecrets,
    capabilities.executor_router,  # what an executor's contribution routes its workers' calls with
    capabilities.Route,
    capabilities.JobKind,  # what a package's piloop.job_kinds name, and what its run and apply get
    capabilities.Control,
    capabilities.Site,
    capabilities.Applying,
    capabilities.Owner,  # a type alias
    capabilities.FactSource,  # what a provider that supplies facts implements, which no signature names
    capabilities.HEAD,  # the facts that a rule reads, and the ratios over them
    capabilities.RUNS,
    capabilities.UNSETTLED,
    capabilities.HOURS,
    capabilities.TOKENS,
    capabilities.WITHOUT_KEEP,
    capabilities.KEEPS,
    capabilities.EVALUATIONS,
    capabilities.METRIC_SUMS,
    capabilities.ATTEMPTS,
    capabilities.FUNNEL,
    capabilities.COMPUTE_USD,
    capabilities.MODELS_USD,
    capabilities.SPEND,
    capabilities.CAMPAIGN,  # the facts of each built-in source
    capabilities.OPTIMIZATION,
    capabilities.reading,
    capabilities.since_keep,
    capabilities.per_keep,
    capabilities.per_evaluation,
    capabilities.worker_spend,
    capabilities.per_run,
    capabilities.Price,  # the types of the built-in spend sources' keys and of the spend's route
    capabilities.Spend,
    capabilities.SpendPart,
    capabilities.reader,  # what a contributed route depends on, as the API's own routes do
    capabilities.actor_access,
    capabilities.Acting,
    capabilities.Addressed,
    capabilities.Viewed,
    capabilities.Reading,
    capabilities.Writing,
    capabilities.REFUSALS_DOCUMENTED,
    capabilities.NodeLink,  # a type alias of the links that a lineage node holds
    capabilities.NodeId,  # NewTypes of the names that a graph declares and refers to
    capabilities.KindName,
    capabilities.StatusName,
    capabilities.TrackKey,
)
"""What a provider raises, calls or returns that no signature of a contract names."""


def _own(group: str) -> list[EntryPoint]:
    return [ep for ep in entry_points(group=group) if ep.dist is not None and ep.dist.name == BUILT_IN]


def _declared() -> list[Capability[Parameters]]:
    loaded = [ep.load() for ep in _own(CAPABILITIES)]
    found = [c for c in loaded if isinstance(c, Capability)]
    assert len(found) == len(loaded)
    return found


def _types(hint: object) -> set[type]:
    """The classes of piloop that a type hint names, outside ``piloop.db``."""
    if isinstance(hint, typing.TypeAliasType):  # ``type Artifact = ...``: what it stands for
        return _types(hint.__value__)
    if isinstance(hint, type):
        ours = hint.__module__.startswith('piloop.') and not hint.__module__.startswith('piloop.db')
        return {hint} if ours else set()
    if isinstance(hint, list):  # the arguments of a ``Callable``
        return {t for part in hint for t in _types(part)}
    parts = [*typing.get_args(hint), *filter(None, [typing.get_origin(hint)])]
    return {t for part in parts for t in _types(part)}


ROWS = {**vars(models), 'Session': Session}
"""The rows that a contract names in annotations only (the trigger contract loads below the database)."""


def _named(cls: type) -> set[type]:
    """The classes that ``cls`` names in its fields, and in the signatures of its own methods and properties.

    With the protocols of piloop that it extends, whose methods it has too: the executor's ``Contributes``.
    """
    hints: list[object] = [b for b in cls.__mro__[1:] if typing.is_protocol(b) and b.__module__.startswith('piloop.')]
    hints += get_type_hints(cls, localns=ROWS, format=Format.FORWARDREF).values()
    for name, member in vars(cls).items():
        if name.startswith('_') and name != '__init__':
            continue
        function = member.fget if isinstance(member, property) else getattr(member, '__func__', member)
        if inspect.isfunction(function):
            hints += get_type_hints(function, localns=ROWS, format=Format.FORWARDREF).values()
    return {t for hint in hints for t in _types(hint)}


def _contracts() -> set[type]:
    """Every type of piloop that the contract of one of its capabilities names, and what those types name.

    With the classes between a built-in provider and its contract, which a plugin's provider subclasses too.
    """
    found: set[type] = set()
    for capability in _declared():
        found |= {type(capability), capability.contract}
        found |= {type(c) for c in (capability.table, capability.entry, capability.ref) if c}
        for provider in capability.builtins().values():
            found |= {b for b in provider.__mro__[1:] if capability.contract in b.__mro__}
    queue = list(found)
    while queue:
        new = _named(queue.pop()) - found
        found |= new
        queue += new
    return found


def _exports() -> dict[str, object]:
    """The public names of the module: its re-exports."""
    return {
        n: v for n, v in vars(capabilities).items() if not n.startswith('_') and not isinstance(v, types.ModuleType)
    }


def _ids(objects: Iterable[object]) -> set[int]:
    return {id(o) for o in objects}


def test_every_capability_of_piloop_and_its_built_in_providers_are_exported_by_the_name_of_the_entry_point() -> None:
    for ep in [*_own(CAPABILITIES), *(p for c in _own(CAPABILITIES) for p in _own(c.load().group))]:
        assert getattr(capabilities, ep.attr, None) is ep.load(), f'{ep.group} {ep.name} = {ep.value}'
    exported = [v for v in _exports().values() if isinstance(v, Capability)]
    assert _ids(exported) == _ids(_declared()), 'an exported capability that piloop no longer declares is stale'


def test_every_type_that_a_contract_names_is_exported_and_nothing_else_is() -> None:
    exported = _exports().values()
    contracts = _contracts()
    missing = sorted(f'{t.__module__}.{t.__qualname__}' for t in contracts if id(t) not in _ids(exported))
    assert missing == []
    providers = [p.load() for c in _own(CAPABILITIES) for p in _own(c.load().group)]
    known = _ids(contracts) | _ids(providers) | _ids(_declared()) | _ids(HELPERS)
    assert [n for n, v in _exports().items() if id(v) not in known] == [], 'stale exports'


def test_a_fresh_interpreter_imports_the_module_first_without_a_cycle() -> None:
    """piloop's own code never imports the module, so nothing that it imports can import it back half done."""
    imported = subprocess.run([sys.executable, '-c', 'import piloop.capabilities'], capture_output=True, text=True)
    assert (imported.returncode, imported.stderr) == (0, '')
    root = Path(capabilities.__file__).parent
    importers = [p for p in root.rglob('*.py') if 'piloop.capabilities' in _imports(p) and p.stem != 'capabilities']
    assert importers == []


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    return {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names} | {
        n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module is not None
    }


def test_each_capability_is_a_package_of_its_contract_its_built_in_providers_and_its_kit() -> None:
    """``plugins/<capability>/``: ``contract.py`` declares the capability, ``facts.py`` the facts that it supplies
    (pure, so that a pure provider reads them), ``consumers.py`` its machinery in the daemon (``piloop.contributors``),
    ``api.py`` the routes that its providers share (``lineage``), ``wire.py`` their client for a UI, ``bridges.py`` the
    bridges of its old keys (``piloop.bridges``), every other module is a built-in provider by the name
    of its entry point, ``kit/`` holds what any provider of the capability may use, and a directory without Python holds the
    providers' resources (``role/prompts/``).

    A campaign type's package, and each component of the campaign type kit, has the same layout for what only the
    campaigns that use it read, in ``<package>/<capability>/``: a capability that it declares (``verdict_rule``), and
    its own providers of the others."""
    plugins = Path(capabilities.__file__).parent / 'plugins'
    kit = plugins / 'campaign_type' / 'kit'
    places = {'piloop.plugins': plugins} | {
        f'piloop.plugins.campaign_type.{p.relative_to(plugins / "campaign_type").as_posix().replace("/", ".")}': p
        for p in (*(plugins / 'campaign_type').iterdir(), *kit.iterdir())
        if p != kit and (p / '__init__.py').is_file()
    }
    declared = _own(CAPABILITIES)
    assert {p.name for p in plugins.iterdir() if p.is_dir() and p.name != '__pycache__'} == {
        c.name for c in declared if c.module == f'piloop.plugins.{c.name}.contract'
    }
    for capability in declared:
        assert capability.module in {f'{place}.{capability.name}.contract' for place in places}, capability.name
        providers = _own(capability.load().group)
        for place, root in places.items():
            here = [p for p in providers if p.module == f'{place}.{capability.name}.{p.name}']
            package = root / capability.name
            if not package.is_dir():
                assert here == [], capability.name
                continue
            others = {
                '__init__.py',
                '__pycache__',
                'contract.py',
                'consumers.py',
                'facts.py',
                'api.py',
                'wire.py',
                'bridges.py',
                'kit',
            }
            resources = {p.name for p in package.iterdir() if p.is_dir() and not any(p.rglob('*.py'))}
            modules = {p.name.removesuffix('.py') for p in package.iterdir() if p.name not in others | resources}
            assert modules == {p.name for p in here}, f'{place}.{capability.name}'
        located = {p.name for p in providers for place in places if p.module == f'{place}.{capability.name}.{p.name}'}
        assert located == {p.name for p in providers}, capability.name


STILL_IMPORT_THE_TYPE_KIT = frozenset(
    {
        'agents/launcher.py',
        'agents/results.py',
        'agents/roles.py',
        'api/views.py',
        'campaign/campaigns.py',
        'campaign/inbox.py',
        'campaign/ops.py',
        'campaign/stop.py',
        'cli/campaign.py',
        'config.py',
        'daemon/api.py',
        'daemon/main.py',
        'daemon/scheduler.py',
        'db/models.py',
        'db/triggers/changelog.py',
        'plugins/campaign_type/contract.py',
        'plugins/contribution.py',
        'reports/feed.py',
        'reports/overview.py',
        'watch/app.py',
        'watch/panels.py',
    }
)
"""The core modules that still reach the campaign type kit's components directly. Each goes when the campaign type
contract carries what it reads, and the list must then shrink with it."""


def test_core_imports_no_helper_of_a_kit() -> None:
    """``plugins/<capability>/kit`` holds helpers that providers may use: only providers, and this module, import it."""
    root = Path(capabilities.__file__).parent
    plugins = root / 'plugins'
    providers = [p for p in plugins.glob('*/*') if p.name not in {'__init__.py', 'contract.py'}]
    core = [p for p in root.rglob('*.py') if not any(p.is_relative_to(provider) for provider in providers)]
    importers = {
        str(p.relative_to(root)): {m for m in _imports(p) if re.match(r'piloop\.plugins\.\w+\.kit\b', m)}
        for p in core
        if p.stem != 'capabilities'
    }
    found = {p for p, kits in importers.items() if kits}
    assert {p for p in found if p not in STILL_IMPORT_THE_TYPE_KIT} == set()
    assert {
        p for p in found if any(not k.startswith('piloop.plugins.campaign_type.kit') for k in importers[p])
    } == set()
    assert found == STILL_IMPORT_THE_TYPE_KIT, 'a module no longer imports the kit: take it off the list'


def test_the_example_plugins_import_piloop_only_from_the_module() -> None:
    plugins = [
        piloop_sqlite_cas,
        piloop_inline,
        piloop_min_gain,
        piloop_nice,
        piloop_stop_after_keeps,
        piloop_keep_cost,
        piloop_inference.min_speedup,
        route_plugin,
    ]
    for plugin in plugins:
        own = plugin.__name__.split('.')[0]
        outside = {m for m in _imports(Path(plugin.__file__ or '')) if m.split('.')[0] not in sys.stdlib_module_names}
        # covalence is the library of piloop's CAS: a provider may return its stores as they are. pydantic's
        # SecretStr is what a contract passes a secret's value in, and a contributed route is a FastAPI router
        assert {m for m in outside if m.split('.')[0] not in {own, 'covalence', 'pydantic', 'fastapi'}} == {
            'piloop.capabilities'
        }, plugin.__name__


def test_a_pure_example_plugin_uses_only_pure_contracts_and_pure_providers() -> None:
    """A pure provider still imports only data and other pure providers through the one module: a pure contract, the
    metric types, the facts, or a pure built-in provider."""
    pure = [c for c in _declared() if c.tier is Tier.PURE]
    modules = {c.contract.__module__ for c in pure} | {p.__module__ for c in pure for p in c.builtins().values()}
    # a pure contract re-exports data of the component that holds it, such as the verdict rule's Outcome
    contracts = [sys.modules[c.contract.__module__] for c in pure]
    modules |= {
        v.__module__
        for m in contracts
        for v in vars(m).values()
        if isinstance(v, type) and v.__module__.startswith('piloop.plugins.campaign_type.kit.')
    }
    modules |= {Capability.__module__, metric.__name__, facts.__name__}
    plugins = [piloop_min_gain, piloop_stop_after_keeps, piloop_keep_cost, piloop_inference.min_speedup]
    for plugin in plugins:
        tree = ast.parse(Path(plugin.__file__ or '').read_text())
        imports = [n for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module == 'piloop.capabilities']
        used = {getattr(capabilities, a.name).__module__ for n in imports for a in n.names}
        assert used <= modules, plugin.__name__
