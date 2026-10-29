"""Compat code that piloop keeps until a set release: deprecated ``piloop.toml`` keys, old names and old behavior.

Mark each piece of compat code with ``until``: the release that deletes it and what to delete. A test finds every
marker and fails once ``pyproject.toml`` reaches its release, so the code goes before that release ships.

A deprecated key still loads, with a warning, until that release. To deprecate a key, write a bridge: a function that
takes the file path, the parsed TOML and the release that removes the key, changes the old key to the new form in place
and calls ``warn`` with that release. A key that only moved is a ``Move``: ``moved`` applies a list of them to the
file and to its environment variables, and the revision that rewrites the stored copies applies the same list. Declare it with ``bridge``, which marks it as ``until`` does and registers it.
Its docstring is its entry under "Deprecated keys" in the generated configuration reference
(``docs/reference/configuration.mdx``). The release notes list it under ``Deprecated``.

When its marker fails, delete the bridge's function and its tests, and run ``make docs``. The old key then fails as an
unknown key, and the release notes list it under ``Removed``.

Where an unknown-key error would mislead, a ``removed_<key>`` guard fails with a link to the upgrading guide instead,
marked with ``until`` for the release after: a key that would load as something else (``[roles.worker]`` as a new
role), one that would silently change nothing (a ``PILOOP_PROJECT_<KEY>`` variable), or a key of a removed feature.
"""

import os
import warnings
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from itertools import count
from pathlib import Path
from typing import Literal, Protocol, assert_never

from piloop.version import Version


class Marked(Protocol):
    """What ``until`` returns: a decorator that returns what it decorates."""

    def __call__[T](self, obj: T, /) -> T: ...


def _same[T](obj: T, /) -> T:
    return obj


def until(version: str, reason: str) -> Marked:
    """Mark compat code that piloop keeps until release ``version``. ``reason`` says what to delete and what replaces it.

    Call it as a statement where the code is (a branch, an alias, a table entry), or put it on a function or a class as
    a decorator. It only parses ``version`` (``Version.parse``), so a malformed one fails when the code runs.
    ``tests/test_deprecated.py`` reads every call in ``piloop/`` and fails once ``pyproject.toml`` is at ``version``.
    Write both arguments as literal strings, ``version`` as ``X.Y.Z``, and call it as ``deprecated.until`` (here,
    ``until``), so that the test can read it.

    Raises:
        ValueError: ``version`` is not ``X.Y.Z``.
    """
    Version.parse(version)
    return _same


type Rewrite = Callable[[Path, dict[str, object], Version], None]
"""The body of a bridge: it rewrites the parsed file in place, and warns that the old key goes in the given release."""


@dataclass(frozen=True)
class Bridge:
    """A bridge that ``apply`` runs on a parsed file: its rewrite, and the release that removes it."""

    rewrite: Rewrite
    removed_in: Version

    def __call__(self, toml: Path, raw: dict[str, object]) -> None:
        self.rewrite(toml, raw, self.removed_in)


BRIDGES: list[Bridge] = []
"""The bridges of ``piloop.toml``, in the order that they run: the order of their definitions."""
CONFIG_BRIDGES: list[Bridge] = []
"""The bridges of ``config.toml``, the global settings file (``piloop.settings.config_file``)."""


def bridge(version: str, reason: str, into: list[Bridge] = BRIDGES) -> Callable[[Rewrite], Bridge]:
    """Declare a bridge that piloop keeps until release ``version``, as ``until`` marks code, and register it in ``into``.

    The bridge gets ``version`` to pass to ``warn``. ``tests/test_deprecated.py`` reads every call, as it does
    ``until``'s.
    """
    removed_in = Version.parse(version)

    def register(rewrite: Rewrite) -> Bridge:
        found = Bridge(rewrite, removed_in)
        into.append(found)
        return found

    return register


@dataclass(frozen=True)
class Move:
    """A key that moved: ``old`` is ``new`` now. Both are dotted paths, and each ``*`` stands for one name, in order.

    ``Move('roles.*', 'plugins.role.*')`` moves ``[roles.research]`` to ``[plugins.role.research]``, and
    ``Move('model.*', 'plugins.harness.*.model')`` moves ``model = { claude = "…" }`` into each harness's table. A key
    that ``new`` already sets keeps its value, and a table merges into the table at ``new`` key by key.
    """

    old: str
    new: str

    def apply(self, raw: dict[str, object]) -> list[str]:
        """Move every key of ``raw`` that ``old`` matches, in place. Return the old keys, dotted, in the file's order.

        A table that the move leaves empty goes too, and so does an empty table at the names, such as ``model = {}``.
        """
        found = list(_matches(raw, self.old.split('.'), ()))
        for names in found:
            value = _pop(raw, _filled(self.old, names))
            _put(raw, _filled(self.new, names), value, wins=False)
        head = self.old.partition('.*')[0].split('.')
        if not found and next(_matches(raw, head, ()), None) is not None and _value(raw, head) == {}:
            _pop(raw, head)
            return ['.'.join(head)]
        return ['.'.join(_filled(self.old, names)) for names in found]

    def rebased(self, path: list[str]) -> list[str] | None:
        """``path`` at its new place when ``old`` matches it or a table above it, else None. Names are not wildcards."""
        old = self.old.split('.')
        if len(path) < len(old) or any(o not in ('*', p) for o, p in zip(old, path, strict=False)):
            return None
        names = tuple(p for o, p in zip(old, path, strict=False) if o == '*')
        return [*_filled(self.new, names), *path[len(old) :]]

    def inverse(self) -> Move:
        """The move back, for a downgrade."""
        return Move(self.new, self.old)


@dataclass(frozen=True)
class Tag:
    """A key that a moved table gains: ``key`` on each table at ``path``, by the first marker of ``by`` that it sets.

    ``Tag('plugins.mcp_server.*', 'provider', (('url', 'http'), ('command', 'stdio')))`` gives a server of 0.3 the
    provider that its keys say. A table that sets ``key`` already keeps it.
    """

    path: str
    key: str
    by: tuple[tuple[str, str], ...]
    """Each marker key, and the value of ``key`` for a table that sets it, in order."""

    def apply(self, raw: dict[str, object]) -> list[str]:
        """Set ``key`` on each table at ``path`` that has a marker and no ``key``. Return their paths, dotted."""
        tagged: list[str] = []
        for names in list(_matches(raw, self.path.split('.'), ())):
            path = _filled(self.path, names)
            table = _value(raw, path)
            if not isinstance(table, dict) or self.key in table:
                continue
            if (value := next((v for marker, v in self.by if marker in table), None)) is not None:
                table[self.key] = value
                tagged.append('.'.join(path))
        return tagged

    def inverse(self) -> Untag:
        """The step back, for a downgrade."""
        return Untag(self.path, self.key, tuple(v for _, v in self.by))


@dataclass(frozen=True)
class Untag:
    """The inverse of a ``Tag``: ``key`` leaves each table at ``path`` where it has one of ``values``."""

    path: str
    key: str
    values: tuple[str, ...]

    def apply(self, raw: dict[str, object]) -> list[str]:
        """Drop ``key`` where its value is one of ``values``. Return the paths of the tables, dotted."""
        untagged: list[str] = []
        for names in list(_matches(raw, self.path.split('.'), ())):
            path = _filled(self.path, names)
            table = _value(raw, path)
            if isinstance(table, dict) and table.get(self.key) in self.values:
                del table[self.key]
                untagged.append('.'.join(path))
        return untagged


@dataclass(frozen=True)
class Prune:
    """The keys of each table at ``path`` that hold nothing: null, or an empty list or table.

    A stored config dumped every key of a model, its defaults included, where a file sets only some. A table of a
    model that a release split into providers (an MCP server into ``http`` and ``stdio``) then holds the keys of the
    other one, empty. Dropping them changes no value: an empty key is its default.
    """

    path: str

    def apply(self, raw: dict[str, object]) -> list[str]:
        """Drop the keys that hold nothing. Return the paths of the tables that lost one, dotted."""
        pruned: list[str] = []
        for names in list(_matches(raw, self.path.split('.'), ())):
            path = _filled(self.path, names)
            table = _value(raw, path)
            if isinstance(table, dict) and (empty := [k for k, v in table.items() if v in (None, [], {})]):
                for key in empty:
                    del table[key]
                pruned.append('.'.join(path))
        return pruned

    def inverse(self) -> None:
        """Nothing to undo: each key that it dropped is the default of its model."""
        return None


type Step = Move | Prune | Tag
"""One rewrite of a bridge's data: a key that moved, keys that hold nothing, or a key that a moved table gains."""


def moved(toml: Path, raw: dict[str, object], steps: Sequence[Step], env: str, removed_in: Version) -> None:
    """Apply ``steps`` to the parsed file, and to the variables of its keys (``<env><KEY>__<KEY>``), with a warning.

    A variable of an old key sets the new key, over the file, as the variable of the new key would. A ``Tag`` and a
    ``Prune`` only complete what a ``Move`` before them warned about, and warn of nothing themselves.
    """
    for step in steps:
        match step:
            case Tag() | Prune():
                step.apply(raw)
            case _:  # a Move
                _move(toml, raw, step, env, removed_in)


def _move(toml: Path, raw: dict[str, object], move: Move, env: str, removed_in: Version) -> None:
    """Apply ``move`` to the parsed file and to the variables of its keys, and warn at the first old key it moved."""
    if found := move.apply(raw):
        parts = found[0].split('.')
        # an inline table sets none of its keys on a line of its own: warn at the key that holds it
        key = next((k for n in range(len(parts), 0, -1) if _lines(toml, k := '.'.join(parts[:n]))), found[0])
        warn(toml, key, f'{_written(move.old)} is deprecated; write {_written(move.new)}', removed_in=removed_in)
    for name, value in sorted(os.environ.items()):
        if not name.upper().startswith(env) or (path := move.rebased(name[len(env) :].lower().split('__'))) is None:
            continue
        _put(raw, path, value, wins=True)
        new = env + '__'.join(path).upper()
        warn(toml, name, f'{name} is deprecated; set {new}', removed_in=removed_in)


@dataclass(frozen=True)
class Entries:
    """A list of inline ``{ provider = "…", … }`` entries at ``old`` is a list of names at ``new`` now.

    An entry without parameters is the name of its provider. One with parameters becomes an instance table at
    ``tables``: named after the provider, after the name of the list's owner first when ``prefix`` gives one (``*``,
    the name that the wildcard of ``old`` stands for, such as a role's), with ``_2`` and on when that name is taken.
    ``Entries('plugins.role.*.triggers', 'plugins.role.*.triggers', 'plugins.trigger', '*')`` turns research's
    ``[{ provider = "low_water", buffer = 4 }]`` into ``["research_low_water"]`` and its table.
    """

    old: str
    new: str
    tables: str
    prefix: str | None

    def apply(self, raw: dict[str, object]) -> list[str]:
        """Turn each list that ``old`` matches into names at ``new``, in place. Return the old keys, dotted.

        A list that is not set (null) goes, and sets nothing.
        """
        found = [names for names in _matches(raw, self.old.split('.'), ()) if self._old(raw, names)]
        for names in found:
            listed = _pop(raw, _filled(self.old, names))
            if isinstance(listed, list):
                owner = names[0] if self.prefix == '*' and names else self.prefix
                _put(raw, _filled(self.new, names), [self._named(raw, e, owner) for e in listed], wins=True)
        return ['.'.join(_filled(self.old, names)) for names in found]

    def _old(self, raw: dict[str, object], names: tuple[str, ...]) -> bool:
        """Whether the list at ``old`` is of the old form: at a key that moved, or with an entry that is a table."""
        listed = _value(raw, _filled(self.old, names))
        return self.old != self.new or (isinstance(listed, list) and any(isinstance(e, dict) for e in listed))

    def _named(self, raw: dict[str, object], entry: object, owner: str | None) -> object:
        """The name of ``entry``, after its parameters went to a table of their own. Not an entry: as it is."""
        if not isinstance(entry, dict) or not isinstance(provider := entry.get('provider'), str):
            return entry
        params = {k: v for k, v in entry.items() if k != 'provider'}
        if not params:
            return provider
        taken = _table(raw, *self.tables.split('.')) or {}
        base = f'{owner}_{provider}' if owner else provider
        table = params if base == provider else {'provider': provider, **params}
        name = next(n for i in count(1) if taken.get(n := base if i == 1 else f'{base}_{i}', table) == table)
        _put(raw, [*self.tables.split('.'), name], table, wins=True)
        return name

    def inverse(self) -> Listed:
        """The way back, for a downgrade: each name at ``new`` is an entry at ``old`` again."""
        return Listed(self)


@dataclass(frozen=True)
class Listed:
    """The inverse of ``Entries``: a list of names is a list of inline entries again.

    The tables stay: two lists can name one, such as a section of a role and of ``[status]``. A downgrade drops them.
    """

    entries: Entries

    def apply(self, raw: dict[str, object]) -> list[str]:
        """Turn each list at ``new`` back into entries at ``old``, in place. Return the new keys, dotted."""
        e = self.entries
        known = _table(raw, *e.tables.split('.')) or {}
        found = list(_matches(raw, e.new.split('.'), ()))
        for names in found:
            listed = _pop(raw, _filled(e.new, names))
            if isinstance(listed, list):
                _put(raw, _filled(e.old, names), [_entry(n, known) for n in listed], wins=True)
        return ['.'.join(_filled(e.new, names)) for names in found]


def _entry(name: object, tables: Mapping[str, object]) -> object:
    """The inline entry of the instance ``name``: its table, with its ``provider``, else the provider of its name."""
    table = tables.get(name) if isinstance(name, str) else None
    if not isinstance(table, dict):
        return {'provider': name} if isinstance(name, str) else name
    return {'provider': table.get('provider', name), **{k: v for k, v in table.items() if k != 'provider'}}


def entries(toml: Path, raw: dict[str, object], lists: Sequence[Entries], removed_in: Version) -> None:
    """Apply ``lists`` to the parsed file, with a warning for each that a list of the file matched.

    A list that an earlier bridge wrote, such as the ``[[facts]]`` of a ``[spend]``, gets that bridge's warning only.
    """
    for listed in lists:
        if written := [key for key in listed.apply(raw) if _lines(toml, key)]:
            where = 'a list of the names of [' + listed.tables + '.<name>] tables'
            warn(
                toml,
                written[0],
                f'{_written(listed.old)} of inline entries is deprecated; write {where}',
                removed_in=removed_in,
            )


def _written(path: str) -> str:
    return f'`{path.replace("*", "<name>")}`'


def _filled(pattern: str, names: tuple[str, ...]) -> list[str]:
    """``pattern``'s parts, with ``names`` in place of its wildcards, in order."""
    found = iter(names)
    return [next(found) if part == '*' else part for part in pattern.split('.')]


def _matches(table: object, parts: list[str], names: tuple[str, ...]) -> Iterator[tuple[str, ...]]:
    """The names that fill the wildcards of each key of ``table`` that ``parts`` matches."""
    if not parts:
        yield names
        return
    if not isinstance(table, dict):
        return
    head, rest = parts[0], parts[1:]
    for key in list(table) if head == '*' else [head] if head in table else []:
        yield from _matches(table[key], rest, (*names, key) if head == '*' else names)


def _value(raw: dict[str, object], path: list[str]) -> object:
    """The value at ``path``, which ``_matches`` found."""
    value: object = raw
    for key in path:
        assert isinstance(value, dict)  # _matches found a table on the way
        value = value[key]
    return value


def _pop(raw: dict[str, object], path: list[str]) -> object:
    """Remove the key at ``path`` and return its value. Remove each table above it that is left empty."""
    parents = [raw]
    for key in path[:-1]:
        child = parents[-1][key]
        assert isinstance(child, dict)  # _matches found a table on the way
        parents.append(child)
    value = parents[-1].pop(path[-1])
    for parent, key in zip(reversed(parents[:-1]), reversed(path[:-1]), strict=True):
        if parent[key] == {}:
            del parent[key]
    return value


def _put(raw: dict[str, object], path: list[str], value: object, *, wins: bool) -> None:
    """Set ``path`` to ``value``. A table merges into the table there, and the value that ``wins`` keeps its keys.

    A value on the way that is not a table is left as it is: the validation refuses it.
    """
    table: object = raw
    for key in path[:-1]:
        table = table.setdefault(key, {}) if isinstance(table, dict) else None
    if not isinstance(table, dict):
        return
    there = table.get(path[-1])
    if isinstance(there, dict) and isinstance(value, dict):
        table[path[-1]] = {**there, **value} if wins else {**value, **there}
    elif there is None or wins:
        table[path[-1]] = value


# Where the ``default`` action records the warnings it showed, so a file loaded twice in one process warns once.
_shown: dict[str | tuple[str, type[Warning], int], int] = {}


class PiloopDeprecationWarning(DeprecationWarning):
    """A deprecated piloop key that still works until the release that removes it. The CLI shows each one once."""


def _table(raw: dict[str, object], *path: str) -> dict[str, object] | None:
    """Return the table at ``path`` in the parsed file, or None. A value that is not a table is left to validation."""
    table: object = raw
    for key in path:
        table = table.get(key) if isinstance(table, dict) else None
    return table if isinstance(table, dict) else None


def _nth(toml: Path, table: str, key: str) -> int:
    """How many lines of ``toml`` set ``key`` before the header of ``table``: ``warn``'s ``nth`` for ``table.key``."""
    lines = toml.read_text().splitlines()
    header = next((n for n, line in enumerate(lines) if line.strip() == f'[{table}]'), len(lines))
    return sum(line.partition('=')[0].strip() == key for line in lines[:header])


PLUGIN_TABLES = (
    Move('harness.*', 'plugins.harness.*'),
    Move('model.*', 'plugins.harness.*.model'),
    Move('max_concurrent.*', 'plugins.harness.*.max_concurrent'),
    Move('roles.*', 'plugins.role.*'),
    Move('harnesses', 'plugins.harness.use'),
    Move('runtime', 'plugins.runtime.use'),
)
"""The tables of the harnesses and the roles, and the keys of each harness, as ``[plugins.<capability>.<name>]``, and
the instances that the campaign uses, as ``[plugins.<capability>] use``."""


@bridge('0.5.0', 'the harness and role tables are under [plugins]: delete plugin_tables and PLUGIN_TABLES')
def plugin_tables(toml: Path, raw: dict[str, object], removed_in: Version) -> None:
    """The tables of the harnesses and the roles are under ``[plugins]``, with the keys of each harness.

    ``[harness.<name>]`` is ``[plugins.harness.<name>]``. ``model = { <name> = "…" }`` and ``max_concurrent = { <name> =
    n }`` are ``model`` and ``max_concurrent`` of ``[plugins.harness.<name>]``. ``[roles.<name>]`` is
    ``[plugins.role.<name>]``. ``harnesses`` is ``[plugins.harness] use``, and ``runtime`` is ``[plugins.runtime] use``.
    Their ``PILOOP_CAMPAIGN_*`` variables move with them. A key that the new table sets too keeps its value there.
    """
    moved(toml, raw, PLUGIN_TABLES, 'PILOOP_CAMPAIGN_', removed_in)


MCP_PROVIDERS: tuple[Step, ...] = (
    Prune('plugins.mcp_server.*'),
    Tag('plugins.mcp_server.*', 'provider', (('url', 'http'), ('command', 'stdio'))),
)
"""Each MCP server of 0.3 under ``[plugins.mcp_server]`` gets the provider that its keys say, without the other's."""
MCP_TABLES: tuple[Step, ...] = (Move('mcp.*', 'plugins.mcp_server.*'), *MCP_PROVIDERS)
"""The MCP servers, as ``[plugins.mcp_server.<name>]`` with the provider that their keys say."""


@bridge('0.5.0', '[mcp.<name>] is [plugins.mcp_server.<name>]: delete mcp_tables and MCP_TABLES')
def mcp_tables(toml: Path, raw: dict[str, object], removed_in: Version) -> None:
    """``[mcp.<name>]`` is ``[plugins.mcp_server.<name>]``, with the provider that its keys say.

    ``provider = "http"`` with ``url``, ``provider = "stdio"`` with ``command``. Its
    ``PILOOP_CAMPAIGN_MCP__<NAME>__<KEY>`` variables move with it.
    """
    moved(toml, raw, MCP_TABLES, 'PILOOP_CAMPAIGN_', removed_in)


def _roles(raw: dict[str, object]) -> dict[str, object] | None:
    """``[plugins.role]`` of the parsed file, added when it is missing. None when a value on its way is not a table."""
    plugins = raw.setdefault('plugins', {})
    roles = plugins.setdefault('role', {}) if isinstance(plugins, dict) else None
    return roles if isinstance(roles, dict) else None


def _into(raw: dict[str, object], rule: str, keys: Mapping[str, object]) -> None:
    """Add ``keys`` to ``[plugins.<rule>]`` of the parsed file, under the keys that the table sets."""
    if (table := _rule(raw, rule)) is not None:
        for key, value in keys.items():
            table.setdefault(key, value)


def _held(raw: dict[str, object], limit: object) -> None:
    """Hold the campaign at ``limit`` of the fact ``spend``: ``[plugins.stop_rule] hold_at``, under what it sets."""
    found = _rule(raw, 'stop_rule')
    rule = found if found is not None else {}  # None: a value on the way is not a table, which the validation refuses
    held = rule.get('hold_at', {})
    rule['hold_at'] = {'spend': limit, **held} if isinstance(held, dict) else held


def _rule(raw: dict[str, object], rule: str) -> dict[str, object] | None:
    """``[plugins.<rule>]`` of the parsed file, added when it is missing.

    None when a value on its way is not a table: the validation refuses it.
    """
    plugins = raw.setdefault('plugins', {})
    table = plugins.setdefault(rule, {}) if isinstance(plugins, dict) else None
    return table if isinstance(table, dict) else None


INCIDENT_TRIGGERS = ('run_failed', 'dead_at_startup')
"""What the ``anomaly`` trigger found: a failed run, and an agent dead at its start."""


@bridge('0.5.0', 'the anomaly trigger is run_failed and dead_at_startup: delete anomaly_trigger and INCIDENT_TRIGGERS')
def anomaly_trigger(toml: Path, raw: dict[str, object], removed_in: Version) -> None:
    """The ``anomaly`` trigger is two triggers: ``run_failed`` and ``dead_at_startup``, in its place in the list."""
    for name, role in (_table(raw, 'plugins', 'role') or {}).items():
        triggers = role.get('triggers') if isinstance(role, dict) else None
        if not isinstance(role, dict) or not isinstance(triggers, list):
            continue
        old = [t for t in triggers if isinstance(t, dict) and t.get('provider') == 'anomaly']
        if not old:
            continue
        role['triggers'] = [
            new for t in triggers for new in ([{'provider': n} for n in INCIDENT_TRIGGERS] if t in old else [t])
        ]
        warn(
            toml,
            f'plugins.role.{name}.triggers',
            'the anomaly trigger is deprecated; write "run_failed", "dead_at_startup"',
            removed_in=removed_in,
            nth=_nth(toml, f'plugins.role.{name}', 'triggers'),
        )


@bridge('0.5.0', 'max_spend is hold_at of the spend: delete the max_spend bridge')
def max_spend(toml: Path, raw: dict[str, object], removed_in: Version) -> None:
    """``[plugins.stop_rule] max_spend`` is ``hold_at = { spend = <value> }``. A value that ``hold_at`` sets too wins."""
    plugins = raw.get('plugins')
    rule = plugins.get('stop_rule') if isinstance(plugins, dict) else None
    if not isinstance(rule, dict) or 'max_spend' not in rule:
        return
    _held(raw, rule.pop('max_spend'))
    warn(
        toml,
        'plugins.stop_rule.max_spend',
        'max_spend is deprecated; write hold_at = { spend = ... }',
        removed_in=removed_in,
    )


LESSONS = 'lessons'
"""The section that ``wake_budget`` sized."""
OLD_BUDGETS = {'watchdog': 0}
"""The roles whose ``wake_budget`` had another default than ``[memory]``'s."""


@bridge('0.5.0', 'wake_budget is the budget of the lessons section: delete wake_budget, LESSONS and OLD_BUDGETS')
def wake_budget(toml: Path, raw: dict[str, object], removed_in: Version) -> None:
    """``[memory] wake_budget`` and ``[plugins.role.<name>] wake_budget`` are the ``budget`` of the ``lessons`` section.

    A role's own value wins over ``[memory]``'s. A budget that differs from the role's old default sets the role's
    ``sections`` to its defaults with the lessons at that budget, or without them at 0. A role that sets ``sections``
    keeps them. The watchdog's old default was 0: it now reads the lessons too.
    """
    from piloop.plugins.role.contract import ROLES, new_role  # here: the roles import this module
    from piloop.plugins.section.contract import defaults  # here: the sections import this module

    shared = _popped(raw, 'memory', 'wake_budget')
    if (roles := _roles(raw)) is None:
        return
    owned = {n: t.pop('wake_budget') for n, t in roles.items() if isinstance(t, dict) and 'wake_budget' in t}
    if shared is not None:
        warn(toml, 'memory.wake_budget', _MOVED, removed_in=removed_in)
    for name in owned:
        warn(
            toml,
            f'plugins.role.{name}.wake_budget',
            _MOVED,
            removed_in=removed_in,
            nth=_nth(toml, f'plugins.role.{name}', 'wake_budget'),
        )
    for name in dict.fromkeys([*ROLES.providers(), *roles]):
        table = roles.setdefault(name, {})
        budget = owned.get(name, None if name in OLD_BUDGETS else shared)
        if not isinstance(table, dict) or 'sections' in table or budget in (None, OLD_BUDGETS.get(name, 400)):
            continue
        model = ROLES.model_of(name) if name in ROLES.providers() else new_role(name, table)
        # a role of its own sections, which wake_budget never sized
        if model.model_fields['sections'].default is not None:
            continue
        listed = [{'provider': name} for name in defaults(model.SURFACE)]
        others = [e for e in listed if e['provider'] != LESSONS]
        table['sections'] = [{'provider': LESSONS, 'budget': budget}, *others] if budget else others


_MOVED = "wake_budget is deprecated; set budget in a [plugins.section.<name>] of lessons, in the role's sections"


@bridge('0.5.0', '[memory] documents is [plugins.memory] documents: delete the memory_documents bridge')
def memory_documents(toml: Path, raw: dict[str, object], removed_in: Version) -> None:
    """``[memory] documents`` is ``documents`` in ``[plugins.memory]``, of the ``local`` provider.

    A value that ``[plugins.memory]`` sets too wins.
    """
    memory = _table(raw, 'memory')
    if memory is None or 'documents' not in memory:
        return
    _into(raw, 'memory', {'documents': _popped(raw, 'memory', 'documents')})
    warn(
        toml,
        'memory.documents',
        '[memory] documents is deprecated; write documents in [plugins.memory]',
        removed_in=removed_in,
        nth=_nth(toml, 'memory', 'documents'),
    )


def _popped(raw: dict[str, object], table: str, key: str) -> object:
    """Pop ``key`` from ``[table]`` of the parsed file, or None, and the table too once it is empty."""
    found = _table(raw, table)
    value = found.pop(key, None) if found is not None else None
    if found == {}:
        del raw[table]
    return value


OLD_HARNESSES = ('pi', 'claude', 'codex')
"""The built-in harnesses of 0.3, whose tables named their own renderer and tally."""


@bridge('0.5.0', 'mcp and usage of a [harness.<name>] table are its provider: delete the harness_providers bridge')
def harness_providers(toml: Path, raw: dict[str, object], removed_in: Version) -> None:
    """``mcp`` and ``[plugins.harness.<name>.usage]`` are the harness's provider: it renders the servers, reads usage.

    A table named after a built-in harness drops them: the provider's own come from ``home``. A table with a new name
    gets ``provider``: its usage's ``tally``, else its ``mcp``. A table that sets neither runs its argv and does
    nothing else, as before.
    """
    tables = _table(raw, 'plugins', 'harness')
    for name, table in tables.items() if tables is not None else ():
        if not isinstance(table, dict) or not {'mcp', 'usage'} & table.keys():
            continue
        mcp, usage = table.pop('mcp', None), table.pop('usage', None)
        tally = usage.get('tally') if isinstance(usage, dict) else None
        if name in OLD_HARNESSES:
            message = (
                f'mcp and usage of [plugins.harness.{name}] are deprecated: the harness renders its servers and reads'
                ' its usage itself, under its home'
            )
        else:
            table['provider'] = named = tally or mcp
            message = f'mcp and usage of [plugins.harness.{name}] are deprecated; write provider = "{named}"'
        warn(toml, f'plugins.harness.{name}', message, removed_in=removed_in)


PLUGIN_LISTS = (
    Entries('plugins.role.*.triggers', 'plugins.role.*.triggers', 'plugins.trigger', '*'),
    Entries('plugins.role.*.sections', 'plugins.role.*.sections', 'plugins.section', '*'),
    Entries('status.sections', 'plugins.section.use', 'plugins.section', 'status'),
    Entries('facts', 'plugins.fact_source.use', 'plugins.fact_source', None),
)
"""The lists of inline entries of the triggers, the sections and the fact sources, as lists of names."""


@bridge('0.5.0', 'the lists of triggers, sections and facts name their instances: delete plugin_lists and PLUGIN_LISTS')
def plugin_lists(toml: Path, raw: dict[str, object], removed_in: Version) -> None:
    """A list of ``{ provider = "…", … }`` entries names instances: ``[plugins.<capability>.<name>]`` tables.

    ``[plugins.role.<name>] triggers`` and ``sections`` name ``[plugins.trigger.<name>]`` and
    ``[plugins.section.<name>]``. ``[status] sections`` is ``[plugins.section] use``, and ``[[facts]]`` is
    ``[plugins.fact_source] use`` with a table for each source. An entry without parameters is the name of its provider.
    One with parameters gets a table, named after its role (``research_low_water``) or ``status`` first.
    """
    entries(toml, raw, PLUGIN_LISTS, removed_in)


BILLING = 'billing'
"""The ``command`` source that a ``[spend] command`` is."""
BILLED = 'billing_usd'
"""The fact that ``BILLING`` reads, which is the ``spend`` that ``[spend]`` was."""
SPEND_COMMAND = (
    'sh',
    '-c',
    'out=$("$@") && printf \'{"metrics": {"billing_usd": %s}}\\n\' "$out" >> "$PILOOP_RESULT_FILE"',
    'sh',
)
"""What runs a ``[spend] command``, which prints one number, so that it reports ``{"metrics": {"billing_usd": …}}``."""
DEFAULT_SOURCES = ['campaign', 'optimization']
"""The fact sources of a campaign that does not set ``[plugins.fact_source] use``."""


@bridge(
    '0.5.0',
    '[spend] is a command source and the facts of the spend source: delete spend_table, SPEND_COMMAND, BILLING,'
    ' BILLED and DEFAULT_SOURCES',
)
def spend_table(toml: Path, raw: dict[str, object], removed_in: Version) -> None:
    """``[spend]`` is the ``command`` source ``billing``, which reads ``billing_usd``, and ``spend`` sums only that.

    So ``spend`` and ``hold_at = { spend = … }`` mean what they meant. The command prints one number, so it runs in
    ``SPEND_COMMAND``. ``notices`` are those of ``billing_usd``, and ``closing_review`` is ``while_held`` of
    ``[plugins.stop_rule]``. Both sources join ``[plugins.fact_source] use``, after its own sources. A table of either
    name that the file has wins. It runs after ``plugin_lists``, so a ``[[facts]]`` is a ``use`` already.
    """
    spend = raw.get('spend')
    if not isinstance(spend, dict):
        return
    del raw['spend']
    review = spend.pop('closing_review', None)
    command = spend.get('command')
    source: dict[str, object] = {
        'provider': 'command',
        'command': [*SPEND_COMMAND, *command] if isinstance(command, list) else command,
        'metrics': [BILLED],
    }
    if 'interval' in spend:
        source['interval'] = spend['interval']
    if 'notices' in spend:
        source['notices'] = {BILLED: spend['notices']}
    if (sources := _rule(raw, 'fact_source')) is not None:
        match sources.get('use', DEFAULT_SOURCES):
            case list() as used:
                sources['use'] = [*used, *(name for name in (BILLING, 'spend') if name not in used)]
            case _:
                pass  # not a list: the validation refuses it
        sources.setdefault(BILLING, source)
        sources.setdefault('spend', {'facts': [BILLED]})
    if review is not None:
        _into(raw, 'stop_rule', {'while_held': review})
    warn(
        toml,
        'spend',
        '[spend] is deprecated; write a [plugins.fact_source.<name>] source with provider = "command" that reports'
        ' <fact>, and name the fact in [plugins.fact_source.spend] facts',
        removed_in=removed_in,
    )


RUNTIME_TABLES = (Move('runtimes.*', 'plugins.runtime.*'),)
"""The settings of each runtime, as ``[plugins.runtime.<name>]``."""


@bridge(
    '0.5.0', '[runtimes.<name>] is [plugins.runtime.<name>]: delete runtime_tables and RUNTIME_TABLES', CONFIG_BRIDGES
)
def runtime_tables(toml: Path, raw: dict[str, object], removed_in: Version) -> None:
    """In ``config.toml``, ``[runtimes.<name>]`` is ``[plugins.runtime.<name>]``.

    ``PILOOP_RUNTIMES__<NAME>__<KEY>`` is ``PILOOP_PLUGINS__RUNTIME__<NAME>__<KEY>``.
    """
    moved(toml, raw, RUNTIME_TABLES, 'PILOOP_', removed_in)


@bridge('0.5.0', 'name_claim of the proxy network is name_claims: delete the name_claim bridge', CONFIG_BRIDGES)
def name_claim(toml: Path, raw: dict[str, object], removed_in: Version) -> None:
    """In ``config.toml``, ``[plugins.network] name_claim`` of the ``proxy`` network is ``name_claims``, a list of one.

    A ``name_claims`` that the table sets too wins.
    """
    network = _table(raw, 'plugins', 'network')
    if network is None or network.get('provider') != 'proxy' or 'name_claim' not in network:
        return
    network.setdefault('name_claims', [network.pop('name_claim')])
    warn(
        toml,
        'plugins.network.name_claim',
        'name_claim is deprecated; write name_claims = ["<claim>"]',
        removed_in=removed_in,
    )


type MessageFormat = Literal['lines', 'claude-hook']
"""The ``--format`` of ``piloop agent messages`` in 0.3, which the hook of a running agent still passes."""


@until('0.5.0', '--format of piloop agent messages is --harness: delete message_format, MessageFormat and the option')
def message_format(form: MessageFormat) -> str | None:
    """The harness whose messages ``--format`` printed: ``claude-hook`` is Claude's hook, ``lines`` is none."""
    match form:
        case 'lines':
            return None
        case 'claude-hook':
            return 'claude'
        case _:
            assert_never(form)


OLD_INSTANCES = {'harness': 'harness', 'model': 'harness', 'max_concurrent': 'harness', 'mcp': 'mcp_server'}
"""The keys of a 0.3.1 file that configured instances, and their capability now."""


@until(
    '0.5.0', 'a 0.3.1 table that nothing uses warns: delete old_instances, OLD_INSTANCES, unused_instance, their calls'
)
def old_instances(raw: Mapping[str, object]) -> frozenset[tuple[str, str]]:
    """The instances, by capability and name, that a file configures in the keys of 0.3.1, before the bridges run.

    A 0.3.1 file set a model for a harness that only ideas pinned, and could define an MCP server that no role listed.
    Such a table that nothing uses only warns (``unused_instance``), where a table of the new shape is refused.
    """
    return frozenset(
        (capability, str(name))
        for key, capability in OLD_INSTANCES.items()
        if isinstance(table := raw.get(key), dict)
        for name in table
    )


def unused_instance(toml: Path, name: str, message: str) -> None:
    """Warn that a table of 0.3.1 that nothing uses, the instance ``name``, is refused from 0.5.0, as a new one is."""
    warn(toml, name, f'{message}; a file of 0.3 only gets this warning', removed_in=UNUSED_UNTIL)


UNUSED_UNTIL = Version.parse('0.5.0')
"""The release from which a table of 0.3.1 that nothing uses is refused, as any other."""


def show_in_cli() -> None:
    """Show each deprecation once. By default, Python hides a ``DeprecationWarning`` outside ``__main__``."""
    warnings.simplefilter('default', PiloopDeprecationWarning)


def warn(toml: Path, key: str, message: str, *, removed_in: Version, nth: int = 0) -> None:
    """Warn at the line of ``toml`` that sets ``key`` for the ``nth`` time, or at line 0 if no line sets it.

    A dotted ``key`` (``roles.research.scout``) is set by its table header, or by a line that sets its last part. The
    output looks like a compiler error: ``piloop.toml:12: PiloopDeprecationWarning: …``, then the line.
    """
    lines = _lines(toml, key)
    lineno = lines[nth] if nth < len(lines) else 0
    text = f'{message}; removed in piloop {removed_in}'
    warnings.warn_explicit(text, PiloopDeprecationWarning, str(toml), lineno, registry=_shown)


def _lines(toml: Path, key: str) -> list[int]:
    """The numbers of the lines of ``toml`` that set the dotted ``key``: its table header, or its last part.

    The header of an array of tables (``[[facts]]``) sets it too.
    """
    last = key.rpartition('.')[2]
    return [
        n
        for n, line in enumerate(toml.read_text().splitlines(), 1)
        if line.strip() in (f'[{key}]', f'[[{key}]]') or line.partition('=')[0].strip() == last
    ]


def apply(toml: Path, raw: dict[str, object], bridges: list[Bridge] = BRIDGES) -> None:
    """Run every bridge on the parsed file. ``config.load_campaign_config`` calls this before it validates the file."""
    for b in bridges:
        b(toml, raw)
