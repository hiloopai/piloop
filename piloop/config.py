"""Campaign configuration: ``piloop.toml`` at the repo root.

``piloop init`` loads the file and stores it in ``campaigns.config``, and the daemon schedules from that copy.
``PILOOP_CAMPAIGN_*`` environment variables override the file when it is loaded, so they apply to ``piloop init`` and
``piloop doctor``, not to the daemon.

``docs/reference/configuration.mdx`` is generated from these models. Run ``make docs`` after you change them.
"""

import hashlib
import json
import math
import tomllib
from dataclasses import dataclass
from datetime import timedelta
from operator import attrgetter
from pathlib import Path
from typing import Annotated, Any, Self, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)
from pydantic_settings import (
    BaseSettings,
    InitSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from piloop import deprecated
from piloop.ids import EnvName, RoleName
from piloop.plugins.campaign_type.kit.evaluation.config import Metric, MetricRole, Rung, Track, TrackConfig
from piloop.plugins.campaign_type.kit.evaluation.ids import TrackName
from piloop.plugins.campaign_type.kit.runs.ids import IMPLEMENTER
from piloop.plugins.fact_source.contract import FACT_SOURCES, FactSource
from piloop.plugins.harness.contract import HARNESSES, Harness
from piloop.plugins.host import Plugins, PluginTables, Scope, Unreached, refs, unknown, unreached
from piloop.plugins.mcp_server.contract import MCP_SERVERS, McpServer, env_names
from piloop.plugins.role.contract import ROLES, ImplementerRole, RoleConfig, SweepRole
from piloop.plugins.runtime.contract import RUNTIMES
from piloop.plugins.section.contract import SECTIONS, Section
from piloop.plugins.stop_rule.contract import STOP_RULES, Shape
from piloop.plugins.trigger.contract import TRIGGERS, Trigger
from piloop.settings import AgentSettings, MovedEnv, settings

CONFIG_FILE = 'piloop.toml'
BRIEF_FILE = 'brief.md'
TRANSCRIPT = 'transcript.txt'
"""The file in an agent's log directory with the end of its screen or output, written when it ends."""


class CampaignAgents(BaseModel):
    """``[agents]``: how long the campaign's agents get to wrap up, and its brief's budget, over the home's ``[agents]``."""

    model_config = ConfigDict(extra='forbid', frozen=True, use_attribute_docstrings=True)

    wrap_up_grace: timedelta | None = Field(default=None, ge=timedelta(0))
    """How long an agent that was told to wrap up, because the campaign stopped or someone cancelled it, has to save
    its work before the daemon ends it. Unset: the home's. Seconds, or an ISO 8601 duration such as ``PT20M``."""
    brief_words: int | None = Field(default=None, ge=1)
    """How many words ``brief.md`` may have before ``piloop init`` and ``piloop doctor`` warn. Unset: the home's."""

    def over(self, home: AgentSettings) -> AgentSettings:
        """The home's settings, with what the campaign sets."""
        return home.model_copy(update=self.model_dump(exclude_none=True))


@dataclass(frozen=True)
class Roles:
    """The campaign's roles: one instance of ``ROLES`` for each, by its name (``[plugins.role.<name>]``).

    Every role is a provider: piloop's (implementer, watchdog, research, scout) and those that packages add, each with
    its table over its defaults, then the roles that the file defines. A table with a new name defines a new role: a
    role that does runs (``ImplementerRole``) with ``runs = true``, else a sweeping role (``SweepRole``).
    """

    by_name: dict[str, RoleConfig]

    def all(self) -> dict[RoleName, RoleConfig]:
        """Every role by name: the providers, then the roles that the file defines, in its order.

        ``new_role`` makes each key a ``RoleName``.
        """
        return {RoleName(name): role for name, role in self.by_name.items()}

    def sweeping(self) -> dict[RoleName, SweepRole]:
        """The sweeping roles by name, in the order of ``all``."""
        return {name: role for name, role in self.all().items() if isinstance(role, SweepRole)}

    def doing_runs(self) -> dict[RoleName, ImplementerRole]:
        """The roles that do runs by name, in the order in which they fill their slots: the implementer last."""
        mine = {name: role for name, role in self.all().items() if isinstance(role, ImplementerRole)}
        implementer = mine.pop(IMPLEMENTER)
        return {**mine, IMPLEMENTER: implementer}


class CampaignConfig(BaseModel):
    """Everything piloop needs to know about a campaign repo."""

    model_config = ConfigDict(extra='forbid', frozen=True, use_attribute_docstrings=True)

    name: str
    """A short name for the campaign: letters, digits, ``.``, ``_`` and ``-``, not all digits. A campaign gets it when
    it is registered, unless the registration names another, and piloop names its git refs after it."""
    metrics: list[Metric] = Field(min_length=1)
    """Every metric that the evaluate command reports, each with its ``role``. Each track has one ``primary``, so a campaign
    with one track has exactly one."""
    tracks: dict[TrackName, TrackConfig] = {}
    """``[tracks.<name>]``: the share and addendum of each track. Needed only with several primaries."""
    evaluate: list[str] = Field(min_length=1)
    """The command (argv) that scores a commit. It runs in a clean worktree of the commit and writes one JSON line,
    ``{"metrics": {"<name>": <value>}}``, to ``$PILOOP_RESULT_FILE``."""
    evaluate_timeout: float = 600
    """The number of seconds before the evaluate command is stopped. The evaluation is then a crash."""
    ladder: list[Rung] = []
    """``[[ladder]]``: the rungs that a run's evaluation of its own version climbs before the full panel, cheapest
    first. The climb stops at the first rung whose ``stop`` holds. A baseline, a head, a bar, a reference and a
    remeasure climb every rung and never stop."""
    link: list[str] = []
    """Repo-relative paths, ignored by git, that piloop puts into every worktree read-only, for example data or virtual
    environments. Each job gets them as they are when it is made."""
    context: list[str] = []
    """Repo-relative files or globs (for example ``docs/error-analysis-*.md``) that hold the campaign's analysis:
    error analyses, comparisons with a reference, evaluation reports. The research pack lists each one with its
    outline, newest first, so that every sweep that reads the pack reads them before it proposes work."""
    agents: CampaignAgents = CampaignAgents()
    """``[agents]``: how long the campaign's agents get to wrap up, and its brief's budget."""
    plugins: Annotated[Plugins, PluginTables(Scope.CAMPAIGN)] = Field(default={}, validate_default=True)
    """``[plugins]``: the campaign's plugins, a table for each capability (``PluginTables``)."""

    @model_validator(mode='after')
    def _check(self) -> Self:
        self._check_tracks()
        self._check_ratios()
        self._check_ladder()
        self._check_facts()
        self._check_stop_rule()
        self._check_refs(Scope.CAMPAIGN, self.plugins)
        roles = self.roles.all()
        harnesses = self.harness
        models = {name: model for name, h in harnesses.items() if (model := h.model) is not None}
        for name, role in roles.items():
            rotation = role.harnesses or self.harnesses
            if missing := [h for h in rotation if h not in models and h not in (role.model or {})]:
                raise ValueError(
                    f'plugins.role.{name}: no model for the harnesses {missing}: set [plugins.harness.<name>] model'
                )
            self._check_mcp(name, role, [harnesses[h] for h in rotation])
        addenda = [a for role in roles.values() if (a := role.addendum) is not None]
        addenda += [t.addendum for t in self.tracks.values() if t.addendum is not None]
        for rel in [*self.link, *addenda, *self.context]:
            if Path(rel).is_absolute() or '..' in Path(rel).parts:
                raise ValueError(f'{rel!r} must be a relative path inside the repo')
        return self

    def _check_ratios(self) -> None:
        """Each ratio divides two other metrics that the evaluate command reports, and no stop of the ladder reads one."""
        reported = set(self.reported)
        for m in self.metrics:
            if m.ratio is None:
                continue
            parts = (m.ratio.numerator, m.ratio.denominator)
            if missing := [p for p in parts if p not in reported]:
                raise ValueError(f'{m.name!r}: ratio of {missing}, which are not metrics that evaluate reports')
            if len(set(parts)) == 1:
                raise ValueError(f'{m.name!r}: ratio of {parts[0]!r} by itself')
        ratios = {m.name for m in self.metrics if m.ratio is not None}
        for rung in self.ladder:
            if named := sorted(rung.stop.keys() & ratios):
                raise ValueError(f'ladder.{rung.name}.stop: {named} are ratios; stop on their parts')

    @property
    def reported(self) -> list[str]:
        """The metrics that the evaluate command may report: every one but the ratios, which piloop computes."""
        return [m.name for m in self.metrics if m.ratio is None]

    @property
    def required(self) -> list[str]:
        """The metrics that the full panel must report: the primaries and guardrails, and the parts of their ratios."""
        judging = [m for m in self.metrics if m.role is not MetricRole.WATCH]
        names = [p for m in judging for p in ((m.ratio.numerator, m.ratio.denominator) if m.ratio else (m.name,))]
        return list(dict.fromkeys(names))

    def _check_ladder(self) -> None:
        """Each rung has its own name, stops on metrics of ``[[metrics]]``, and the rungs with a command come first."""
        names = [rung.name for rung in self.ladder]
        if repeated := sorted({n for n in names if names.count(n) > 1}):
            raise ValueError(f'ladder: rungs {repeated} appear more than once')
        known = {m.name for m in self.metrics}
        for rung in self.ladder:
            if unknown := sorted(rung.stop.keys() - known):
                raise ValueError(f'ladder.{rung.name}.stop: {unknown} are not in [[metrics]]')
        owned = [rung.evaluate is not None for rung in self.ladder]
        if owned != sorted(owned, reverse=True):
            raise ValueError('ladder: the rungs with their own evaluate come first')

    def _check_stop_rule(self) -> None:
        """Check that the stop rule serves this campaign (``StopRule.check_config``), and has a source for each fact."""
        rule = STOP_RULES.selected(self.plugins)
        name = STOP_RULES.name_of(rule)
        if (why := rule.check_config(Shape(tracks=len(self.all_tracks)))) is not None:
            raise ValueError(f'the stop rule {name!r} {why}')
        supplied = {fact for source in self.fact_sources() for fact in source.supplies()}
        if missing := [fact.name for fact in rule.reads() if fact not in supplied]:
            raise ValueError(
                f'the stop rule {name!r} reads {", ".join(missing)}, which no fact source of this campaign supplies: add'
                ' one to [plugins.fact_source] use that does'
            )

    def _check_facts(self) -> None:
        """Check that no two fact sources supply the same fact, and that another source supplies each that one reads."""
        seen: dict[str, FactSource] = {}
        sources = self.fact_sources()
        for source in sources:
            for fact in source.supplies():
                if (other := seen.setdefault(fact.name, source)) is not source:
                    names = f'{FACT_SOURCES.name_of(other)} and {FACT_SOURCES.name_of(source)}'
                    raise ValueError(
                        f'the fact {fact.name!r} comes from two sources of [plugins.fact_source] use: {names}'
                    )
        for source in sources:
            if missing := [fact.name for fact in source.reads() if seen.get(fact.name, source) is source]:
                raise ValueError(
                    f'the fact source {FACT_SOURCES.name_of(source)!r} reads {", ".join(missing)}, which no other'
                    ' source of [plugins.fact_source] use supplies: add one that does'
                )

    def fact_sources(self) -> list[FactSource]:
        """The campaign's fact sources: ``[plugins.fact_source] use``, else every installed source on by default."""
        found = FACT_SOURCES.instances(self.plugins)
        return [found[name] for name in FACT_SOURCES.used(self.plugins)]

    def status_sections(self) -> list[Section]:
        """What ``piloop status``, the API and a UI show: ``[plugins.section] use``, else the defaults of status."""
        found = SECTIONS.instances(self.plugins)
        return [found[name] for name in SECTIONS.used(self.plugins)]

    def sections_of(self, name: RoleName) -> list[Section]:
        """What the briefs of a role show, in order: the sections of its ``shown``."""
        found = SECTIONS.instances(self.plugins)
        return deprecated.context_listed([found[section] for section in self.role(name).shown()], found)

    def triggers_of(self, name: str) -> list[Trigger]:
        """The triggers of a sweeping role, in order: those that its ``triggers`` names. None for a role of runs."""
        match self.roles.all().get(RoleName(name)):
            case SweepRole(triggers=names):
                found = TRIGGERS.instances(self.plugins)
                return [found[trigger] for trigger in names]
            case _:
                return []

    def _check_tracks(self) -> None:
        """Check that each track has one primary.

        With several tracks, each primary also needs a margin, each track needs a table, and the shares must sum to 1.
        """
        primaries = [m for m in self.metrics if m.role is MetricRole.PRIMARY]
        if not primaries:
            raise ValueError('at least one metric must be primary')
        names = [m.track_name for m in primaries]
        if len(primaries) == 1 and not primaries[0].empty:
            raise ValueError(
                f'{primaries[0].name!r}: floor, margin and fraction on a primary apply only with several tracks'
            )
        if len(primaries) > 1:
            if dupes := sorted({n for n in names if names.count(n) > 1}):
                raise ValueError(f'tracks {dupes} have more than one primary: give each primary its own track')
            if unguarded := [m.name for m in primaries if not m.regression]:
                raise ValueError(
                    f'primaries {unguarded} need a margin or a fraction: with several tracks each guards the others'
                )
            if untabled := [n for n in names if n not in self.tracks]:
                raise ValueError(f'tracks {untabled} need a [tracks.<name>] table with their share')
        if unknown := sorted(set(self.tracks) - set(names)):
            raise ValueError(f'[tracks] {unknown} name no track a primary declares; tracks are {names}')
        total = sum(self.tracks[n].share if n in self.tracks else 1 for n in names)
        if not math.isclose(total, 1):
            raise ValueError(f'track shares sum to {total:g}, not 1')

    def _check_refs(self, scope: Scope, plugins: Plugins) -> None:
        """Check that each name of an instance (``Ref``) of a capability of ``scope`` names an instance in ``plugins``.

        Raises:
            ValueError: A name names no instance. The message lists the instances.
        """
        if found := unknown(refs(self), plugins, scope):
            raise ValueError(found[0])

    def unreached(self) -> list[Unreached]:
        """The instances that a table configures and that no name puts in use, of a capability that only names reach.

        ``load_campaign_config`` refuses them, not the validation: a stored config of an older piloop could hold a table
        that nothing uses, such as an MCP server, and must still load. ``piloop init`` then says what to fix.
        """
        return unreached(self.plugins, refs(self))

    def check_home(self, home: Plugins) -> None:
        """Check the names of instances of ``config.toml`` (``home``, its ``[plugins]``), such as a runtime.

        Raises:
            ValueError: A name names no instance of the home.
        """
        self._check_refs(Scope.HOME, home)

    def _check_mcp(self, name: RoleName, role: RoleConfig, rotation: list[Harness]) -> None:
        """Every harness that the role may run can take its servers in its argv. ``_check_refs`` checks the names."""
        if not role.mcp:
            return
        for harness_name, harness in zip(role.harnesses or self.harnesses, rotation, strict=True):
            if why := harness.refuses_servers():
                raise ValueError(
                    f'plugins.role.{name}.mcp: harness {harness_name!r}: {why}; or take it out of the rotation'
                )

    @property
    def mcp_servers(self) -> dict[str, McpServer]:
        """The MCP servers of the campaign, by name: ``[plugins.mcp_server.<name>]``."""
        return MCP_SERVERS.instances(self.plugins)

    def role_servers(self, name: RoleName) -> dict[str, McpServer]:
        """The MCP servers that the agents of a role get, by name."""
        servers = self.mcp_servers
        return {server: servers[server] for server in self.role(name).mcp}

    def role_secrets(self, name: RoleName, harness: str) -> dict[EnvName, EnvName]:
        """The variables of an agent of a role on ``harness`` that take a secret, with the secret's name.

        Each variable of the role's MCP servers is the secret of its own name, unless the harness's ``env`` gives it a
        value or another secret.
        """
        definition = self.harness_for(harness)
        servers = env_names(self.role_servers(name).values())
        return {n: n for n in servers if n not in definition.env} | definition.secrets

    @property
    def roles(self) -> Roles:
        """The roles of the campaign: ``[plugins.role.<name>]``."""
        return Roles(ROLES.instances(self.plugins))

    @property
    def harnesses(self) -> list[str]:
        """The harness rotation: ``[plugins.harness] use``, else the harnesses that are on by default (``pi``)."""
        return list(HARNESSES.used(self.plugins))

    @property
    def runtime(self) -> str:
        """Where the agents run: ``[plugins.runtime] use``, a runtime of ``config.toml``, else ``subprocess``."""
        [name] = RUNTIMES.used(self.plugins)
        return name

    @property
    def harness(self) -> dict[str, Harness]:
        """The harnesses of the campaign, by name: ``[plugins.harness.<name>]``."""
        return HARNESSES.instances(self.plugins)

    @property
    def harness_caps(self) -> dict[str, int]:
        """The ``max_concurrent`` of each harness that sets one, by name."""
        return {name: cap for name, h in self.harness.items() if (cap := h.max_concurrent) is not None}

    def role(self, name: RoleName) -> RoleConfig:
        """The ``[plugins.role.<name>]`` table. The keys that only one role has need ``self.roles``.

        Raises:
            KeyError: ``piloop.toml`` has no such role.
        """
        return self.roles.all()[name]

    def role_harnesses(self, name: RoleName) -> list[str]:
        """The rotation a role uses: its own list, or else the campaign's."""
        return self.role(name).harnesses or self.harnesses

    def role_models(self, name: RoleName) -> dict[str, str]:
        """The model for each harness of a role: each harness's ``model``, with the role's map on top."""
        own = {harness: model for harness, h in self.harness.items() if (model := h.model) is not None}
        return {**own, **(self.role(name).model or {})}

    def role_runtime(self, name: RoleName) -> str:
        """The name of the runtime a role uses: its own, or else the campaign's."""
        return self.role(name).runtime or self.runtime

    @property
    def run_slots(self) -> int:
        """The number of runs that can be open at the same time: the ``max_concurrent`` of every role that does runs."""
        return sum(role.max_concurrent for role in self.roles.doing_runs().values())

    @property
    def guardrails(self) -> list[Metric]:
        """The campaign's guardrails. A candidate of any track must stay within their margins and floors."""
        return [m for m in self.metrics if m.role is MetricRole.GUARDRAIL]

    @property
    def all_tracks(self) -> list[Track]:
        """Every track, in the order of its primary in ``metrics``. The first one is the default."""
        primaries = [m for m in self.metrics if m.role is MetricRole.PRIMARY]
        return [
            Track(
                name=m.track_name,
                primary=m,
                share=(table := self.tracks.get(m.track_name, TrackConfig())).share,
                addendum=table.addendum,
                guardrails=(*self.guardrails, *(p for p in primaries if p is not m)),
            )
            for m in primaries
        ]

    @property
    def multitrack(self) -> bool:
        """Whether the campaign has more than one track. Only then do the renderers show track names."""
        return len(self.all_tracks) > 1

    def track(self, name: TrackName) -> Track:
        """The track called ``name``.

        Raises:
            KeyError: ``piloop.toml`` has no such track. The message lists the tracks it has.
        """
        found = next((t for t in self.all_tracks if t.name == name), None)
        if found is None:
            raise KeyError(f'no track {name!r} in piloop.toml; tracks: {", ".join(t.name for t in self.all_tracks)}')
        return found

    @property
    def evaluator_definition(self) -> dict[str, object]:
        """What decides a verdict's numbers: the evaluate command, the linked inputs and the judging metrics.

        ``judging`` has the name and direction of each primary and guardrail, by name, and the parts of a ratio. ``ladder``, only when there is
        one, has each rung's name and command: a rung's values compare only under the same rungs. Its ``stop`` is the
        policy of the climb, as a margin is of the verdict. A watch metric never decides a verdict, so adding, removing or redirecting one measures nothing again: the
        older measurements just lack it. Promoting a watch metric to a guardrail, or demoting a guardrail, does. Roles
        among the judging metrics, margins, floors and the verdict rule are the policy of the verdict, not part of the
        score.
        """
        judging = sorted((m for m in self.metrics if m.role is not MetricRole.WATCH), key=attrgetter('name'))
        rungs = [{'name': rung.name, 'evaluate': rung.evaluate} for rung in self.ladder]
        return {
            'evaluate': list(self.evaluate),
            'link': list(self.link),
            'judging': [
                {'name': m.name, 'direction': m.direction.value}
                | ({'ratio': m.ratio.model_dump()} if m.ratio is not None else {})
                for m in judging
            ],
        } | ({'ladder': rungs} if rungs else {})

    @property
    def evaluator_key(self) -> str:
        """A short hash of ``evaluator_definition``. It identifies an ``evaluators`` row."""
        return hashlib.sha256(json.dumps(self.evaluator_definition, sort_keys=True).encode()).hexdigest()[:16]

    def harness_for(self, name: str) -> Harness:
        """The harness called ``name``.

        Raises:
            KeyError: The campaign has no such harness.
        """
        return self.harness[name]


class ConfigError(ValueError):
    """A ``piloop.toml`` that does not load. The message names the file, each bad key and value, and the fix."""


class ConfigMissing(ConfigError):
    """A campaign directory without a ``piloop.toml``."""


DOCS = 'https://piloop.autoresearch.inc/reference/configuration'
FIXES = {
    'extra_forbidden': f'delete the key or correct its spelling; {DOCS} lists every key',
    'missing': f'add the key; {DOCS} says what it takes',
}


def explain(where: object, error: ValidationError) -> str:
    """Pydantic's errors as one line per bad key: the key and value (``metrics[0].role = 'boss'``), problem and fix."""
    lines = [f'{where} is not a valid piloop config ({error.error_count()} problem(s)):']
    for e in error.errors():
        key = ''.join(f'[{part}]' if isinstance(part, int) else f'.{part}' for part in e['loc']).lstrip('.')
        value = '' if e['type'] == 'missing' or isinstance(e['input'], dict | list) else f' = {e["input"]!r}'
        message = e['msg'].removeprefix('Value error, ')
        fix = FIXES.get(e['type'], f'{DOCS} describes the key')
        lines.append(f'  {key or "(the file)"}{value}: {message}. Fix: {fix}')
    return '\n'.join(lines)


def prompt_files(role: RoleConfig) -> dict[str, str]:
    """Return the prompt files that a role's table sets, by key: ``prompt``, and a sweep's two prompts."""
    keys = {'prompt': role.prompt}
    if isinstance(role, SweepRole):
        keys |= {'incident_prompt': role.incident_prompt, 'routine_prompt': role.routine_prompt}
    return {key: rel for key, rel in keys.items() if rel is not None}


def merged(base: object, over: object) -> object:
    """``over`` on top of ``base``, key by key: a table merges into the table of the same key, any other value replaces."""
    match base, over:
        case {**under}, {**above}:
            return {**under, **{key: merged(under.get(key), value) for key, value in above.items()}}
        case _:
            return over


def _has_table(plugins: object, capability: str, name: str) -> bool:
    """Whether ``plugins``, a file's raw ``[plugins]``, has the table ``[plugins.<capability>.<name>]``."""
    return isinstance(plugins, dict) and isinstance(of := plugins.get(capability), dict) and name in of


def load_campaign_config(campaign: Path, **overrides: object) -> CampaignConfig:
    """Load ``<campaign>/piloop.toml``.

    ``overrides`` take precedence over ``PILOOP_CAMPAIGN_*`` environment variables, which take precedence over the
    file. Deprecated keys still load through ``piloop.deprecated``, with a ``PiloopDeprecationWarning``.

    Raises:
        ConfigMissing: The campaign has no ``piloop.toml``.
        ConfigError: The file is not valid TOML, not a valid config, names an instance that the home's ``config.toml``
            lacks, or names a prompt file that does not exist.
    """
    toml = campaign / CONFIG_FILE
    if not toml.is_file():
        raise ConfigMissing(f'{campaign} has no {CONFIG_FILE}: `piloop init {campaign}` makes one')
    try:
        raw = tomllib.loads(toml.read_text())
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f'{toml} is not valid TOML: {e}') from e
    old = deprecated.old_instances(raw)
    deprecated.apply(toml, raw, deprecated.bridges(Scope.CAMPAIGN))
    defaults = settings.campaign_defaults
    raw['plugins'] = merged(defaults, raw.get('plugins', {}))

    class _Loader(BaseSettings, CampaignConfig):
        model_config = SettingsConfigDict(env_prefix='PILOOP_CAMPAIGN_', env_nested_delimiter='__', extra='forbid')

        @classmethod
        def settings_customise_sources(
            cls,
            settings_cls: type[BaseSettings],
            init_settings: PydanticBaseSettingsSource,
            env_settings: PydanticBaseSettingsSource,
            dotenv_settings: PydanticBaseSettingsSource,
            file_secret_settings: PydanticBaseSettingsSource,
        ) -> tuple[PydanticBaseSettingsSource, ...]:
            moved = MovedEnv(env_settings, deprecated.bridges(Scope.CAMPAIGN), 'PILOOP_CAMPAIGN_')
            return init_settings, moved, InitSettingsSource(settings_cls, raw)

    # pydantic validates the values; the cast only tells the type checker to let them through
    kwargs = cast(dict[str, Any], {k: v for k, v in overrides.items() if v is not None})
    try:
        cfg = CampaignConfig.model_validate(_Loader(**kwargs).model_dump())
    except ValidationError as e:
        raise ConfigError(explain(toml, e)) from e
    for lost in cfg.unreached():
        if _has_table(defaults, lost.capability, lost.name):
            continue  # a default of the home's config.toml: a campaign that does not use it leaves it
        if (lost.capability, lost.name) in old:
            deprecated.unused_instance(toml, lost.name, str(lost))
            continue
        raise ConfigError(f'{toml} is not a valid piloop config (1 problem(s)):\n  {lost}')
    try:
        cfg.check_home(settings.providers)
    except ValueError as e:
        raise ConfigError(f'{toml} is not a valid piloop config for {settings.home / "config.toml"}: {e}') from e
    missing = [
        f'  plugins.role.{name}.{key} = {rel!r}: no such file. Fix: create it, or correct the path'
        for name, role in cfg.roles.all().items()
        for key, rel in prompt_files(role).items()
        if not (campaign / rel).is_file()
    ]
    if missing:
        raise ConfigError('\n'.join([f'{toml} is not a valid piloop config ({len(missing)} problem(s)):', *missing]))
    return cfg
