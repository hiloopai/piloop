"""Compat code that piloop keeps until a set release: deprecated ``piloop.toml`` keys, old names and old behavior.

Mark each piece of compat code with ``until``: the release that deletes it and what to delete. A test finds every
marker and fails once ``pyproject.toml`` reaches its release, so the code goes before that release ships.

A deprecated key still loads, with a warning, until that release. To deprecate a key, write a bridge: a function that
takes the file path, the parsed TOML and the release that removes the key, changes the old key to the new form in place
and calls ``warn`` with that release. A key that only moved is a ``Move``: ``moved`` applies a list of them to the
file and to its environment variables, and the revision that rewrites the stored copies applies the same list. Declare
it with ``bridge``, which marks it as ``until`` does, next to its capability in a ``bridges`` module that imports no
provider, and name it in the ``piloop.bridges`` entry points (``BRIDGES``). A bridge that reads what another wrote says
so with ``after`` or ``before``. A bridge of several capabilities, such as ``plugin_tables``, is here.
Its docstring is its entry under "Deprecated keys" in the generated configuration reference
(``docs/reference/configuration.mdx``). The release notes list it under ``Deprecated``.

When its marker fails, delete the bridge's function and its tests, and run ``make docs``. The old key then fails as an
unknown key, and the release notes list it under ``Removed``.

Where an unknown-key error would mislead, a ``removed_<key>`` guard fails with a link to the upgrading guide instead,
marked with ``until`` for the release after: a key that would load as something else (``[roles.worker]`` as a new
role), one that would silently change nothing (a ``PILOOP_PROJECT_<KEY>`` variable), or a key of a removed feature.
"""

import graphlib
import os
import warnings
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from itertools import count
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, assert_never

from pydantic import InstanceOf, TypeAdapter

from piloop.plugins.host import PluginError, Scope, Slot
from piloop.version import Version

if TYPE_CHECKING:  # the sections import this module
    from piloop.plugins.section.contract import Section


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
    """A bridge that ``apply`` runs on a parsed file: its rewrite, the release that removes it, and its file."""

    rewrite: Rewrite
    removed_in: Version
    scope: Scope = Scope.CAMPAIGN
    """Which file it rewrites: ``piloop.toml``, or ``config.toml`` (``Scope.HOME``)."""
    after: tuple[str, ...] = ()
    """The bridges whose result it reads, by name: it runs after them. Django's ``dependencies`` of a migration."""
    before: tuple[str, ...] = ()
    """The bridges that read its result, by name: it runs before them. Django's ``run_before``."""

    def __call__(self, toml: Path, raw: dict[str, object]) -> None:
        self.rewrite(toml, raw, self.removed_in)


BRIDGES: Slot[Bridge] = Slot('bridges', TypeAdapter(InstanceOf[Bridge]), 'bridge')
"""Every bridge, by its name: piloop's and a package's.

A capability declares its bridges next to it, in a module that imports no provider (``<capability>/bridges.py``):
they run before any provider loads.
"""


def bridge(
    version: str, reason: str, scope: Scope = Scope.CAMPAIGN, after: tuple[str, ...] = (), before: tuple[str, ...] = ()
) -> Callable[[Rewrite], Bridge]:
    """Declare a bridge of the file of ``scope`` that piloop keeps until release ``version``, as ``until`` marks code.

    The bridge gets ``version`` to pass to ``warn``. An entry point in ``piloop.bridges`` names it (``BRIDGES``).
    ``tests/test_deprecated.py`` reads every call, as it does ``until``'s.
    """
    removed_in = Version.parse(version)

    def declared(rewrite: Rewrite) -> Bridge:
        return Bridge(rewrite, removed_in, scope, after, before)

    return declared


def bridges(scope: Scope) -> list[Bridge]:
    """The bridges of the file of ``scope``, each after those whose result it reads, else in the order of their names.

    Raises:
        PluginError: An entry point does not load or names no ``Bridge``, a bridge runs after or before one that there
            is not, or the bridges run after each other in a cycle.
    """
    found = BRIDGES.loaded()
    order: graphlib.TopologicalSorter[str] = graphlib.TopologicalSorter()
    for name, b in sorted(found.items()):
        if unknown := [n for n in (*b.after, *b.before) if n not in found]:
            raise PluginError(f'the bridge {name} runs after or before no bridge called {", ".join(unknown)}')
        order.add(name, *b.after)
        for later in b.before:
            order.add(later, name)
    try:
        names = list(order.static_order())
    except graphlib.CycleError as e:
        raise PluginError(f'the bridges run after each other in a cycle: {" -> ".join(e.args[1])}') from e
    return [found[name] for name in names if found[name].scope is scope]


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
        taken = table_at(raw, *self.tables.split('.')) or {}
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
        known = table_at(raw, *e.tables.split('.')) or {}
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


def table_at(raw: dict[str, object], *path: str) -> dict[str, object] | None:
    """Return the table at ``path`` in the parsed file, or None. A value that is not a table is left to validation."""
    table: object = raw
    for key in path:
        table = table.get(key) if isinstance(table, dict) else None
    return table if isinstance(table, dict) else None


def nth(toml: Path, table: str, key: str) -> int:
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


def roles(raw: dict[str, object]) -> dict[str, object] | None:
    """``[plugins.role]`` of the parsed file, added when it is missing. None when a value on its way is not a table."""
    plugins = raw.setdefault('plugins', {})
    roles = plugins.setdefault('role', {}) if isinstance(plugins, dict) else None
    return roles if isinstance(roles, dict) else None


def into(raw: dict[str, object], rule: str, keys: Mapping[str, object]) -> None:
    """Add ``keys`` to ``[plugins.<rule>]`` of the parsed file, under the keys that the table sets."""
    if (table := plugin_table(raw, rule)) is not None:
        for key, value in keys.items():
            table.setdefault(key, value)


def plugin_table(raw: dict[str, object], rule: str) -> dict[str, object] | None:
    """``[plugins.<rule>]`` of the parsed file, added when it is missing.

    None when a value on its way is not a table: the validation refuses it.
    """
    plugins = raw.setdefault('plugins', {})
    table = plugins.setdefault(rule, {}) if isinstance(plugins, dict) else None
    return table if isinstance(table, dict) else None


def popped(raw: dict[str, object], table: str, key: str) -> object:
    """Pop ``key`` from ``[table]`` of the parsed file, or None, and the table too once it is empty."""
    found = table_at(raw, table)
    value = found.pop(key, None) if found is not None else None
    if found == {}:
        del raw[table]
    return value


PLUGIN_LISTS = (
    Entries('plugins.role.*.triggers', 'plugins.role.*.triggers', 'plugins.trigger', '*'),
    Entries('plugins.role.*.sections', 'plugins.role.*.sections', 'plugins.section', '*'),
    Entries('status.sections', 'plugins.section.use', 'plugins.section', 'status'),
    Entries('facts', 'plugins.fact_source.use', 'plugins.fact_source', None),
)
"""The lists of inline entries of the triggers, the sections and the fact sources, as lists of names."""


@bridge(
    '0.5.0',
    'the lists of triggers, sections and facts name their instances: delete plugin_lists and PLUGIN_LISTS',
    after=('plugin_tables',),
)
def plugin_lists(toml: Path, raw: dict[str, object], removed_in: Version) -> None:
    """A list of ``{ provider = "…", … }`` entries names instances: ``[plugins.<capability>.<name>]`` tables.

    ``[plugins.role.<name>] triggers`` and ``sections`` name ``[plugins.trigger.<name>]`` and
    ``[plugins.section.<name>]``. ``[status] sections`` is ``[plugins.section] use``, and ``[[facts]]`` is
    ``[plugins.fact_source] use`` with a table for each source. An entry without parameters is the name of its provider.
    One with parameters gets a table, named after its role (``research_low_water``) or ``status`` first.
    """
    entries(toml, raw, PLUGIN_LISTS, removed_in)


@until('0.5.0', 'a list of sections without the context shows it after its guides: delete context_listed')
def context_listed(shown: list[Section], found: Mapping[str, Section]) -> list[Section]:
    """A role's list of sections, with the ``context`` section after its guides when it names none.

    Before the context was a section, every brief showed it after the guides of its role's list, and the list's other
    sections after it.
    """
    from piloop.plugins.section.context import Context  # here: the sections import this module
    from piloop.plugins.section.contract import Guide  # here: the sections import this module

    if any(isinstance(section, Context) for section in shown):
        return shown
    guides = [section for section in shown if isinstance(section, Guide)]
    return [*guides, found['context'], *(section for section in shown if not isinstance(section, Guide))]


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


def apply(toml: Path, raw: dict[str, object], found: Sequence[Bridge]) -> None:
    """Run each bridge of ``found`` on the parsed file, in order, before the file is validated.

    ``config.load_campaign_config`` runs ``bridges(Scope.CAMPAIGN)``, and ``settings`` runs ``bridges(Scope.HOME)``.
    """
    for b in found:
        b(toml, raw)
