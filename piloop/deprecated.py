"""Compat code that piloop keeps until a set release: deprecated ``piloop.toml`` keys, old names and old behavior.

Mark each piece of compat code with ``until``: the release that deletes it and what to delete. A test finds every
marker and fails once ``pyproject.toml`` reaches its release, so the code goes before that release ships.

A deprecated key still loads, with a warning, until that release. To deprecate a key, write a bridge: a function that
takes the file path, the parsed TOML and the release that removes the key, changes the old key to the new form in place
and calls ``warn`` with that release. A revision rewrites the stored copies the same way (``Move`` and ``Prune`` are
the steps that revision 0072 takes). Declare it with ``bridge``, which marks it as
``until`` does, next to its capability in a ``bridges`` module that imports no provider, and name it in the
``piloop.bridges`` entry points (``BRIDGES``). A bridge that reads what another wrote says so with ``after`` or
``before``.
Its docstring is its entry under "Deprecated keys" in the generated configuration reference
(``docs/reference/configuration.mdx``). The release notes list it under ``Deprecated``.

When its marker fails, delete the bridge's function and its tests, and run ``make docs``. The old key then fails as an
unknown key, and the release notes list it under ``Removed``.

Where an unknown-key error would mislead, a ``removed_<key>`` guard fails with a link to the upgrading guide instead,
marked with ``until`` for the release after: a key that would load as something else (``[roles.worker]`` as a new
role), one that would silently change nothing (a ``PILOOP_PROJECT_<KEY>`` variable), or a key of a removed feature.
"""

import graphlib
import warnings
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from pydantic import InstanceOf, TypeAdapter

from piloop.plugins.host import PluginError, Scope, Slot
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
    version: str,
    reason: str,
    scope: Scope = Scope.CAMPAIGN,
    after: tuple[str, ...] = (),
    before: tuple[str, ...] = (),
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

    ``Move('roles.*', 'plugins.role.*')`` moves ``[roles.orchestrator]`` to ``[plugins.role.orchestrator]``, and
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


type Step = Move | Prune
"""One rewrite of a bridge's data: a key that moved, or keys that hold nothing."""


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


def show_in_cli() -> None:
    """Show each deprecation once. By default, Python hides a ``DeprecationWarning`` outside ``__main__``."""
    warnings.simplefilter('default', PiloopDeprecationWarning)


def warn(toml: Path, key: str, message: str, *, removed_in: Version) -> None:
    """Warn at the first line of ``toml`` that sets ``key``, or at line 0 if no line sets it.

    A dotted ``key`` (``plugins.role.orchestrator``) is set by its table header, or by a line that sets its last part. The
    output looks like a compiler error: ``piloop.toml:12: PiloopDeprecationWarning: …``, then the line.
    """
    lines = _lines(toml, key)
    lineno = lines[0] if lines else 0
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


UPGRADING = 'https://piloop.autoresearch.inc/guides/upgrading'
"""The upgrading guide, which lists each removed key and what replaces it."""
LESSONS = 'lessons'
"""The section of research memory, which 0.7 removed."""


@until('0.9.0', 'a 0.7 file may still set the keys that 0.8.0 removed: delete removed_keys, LESSONS and its callers')
def removed_keys(toml: Path, raw: dict[str, object], scope: Scope) -> list[str]:
    """Each key of the parsed file of ``scope`` that 0.8.0 removed, as a line: where it is and what to do. None: none.

    0.7 loaded these keys with a warning; an unknown-key error would not say that they went in a release. A line names
    the key and the line that sets it, as a warning did.
    """
    found: list[tuple[str, str, str]] = []  # the key as the file sets it, its dotted path, why it went
    if scope is Scope.HOME:
        if 'wake_budget' in (table_at(raw, 'agents') or {}):
            found.append(('[agents] wake_budget', 'agents.wake_budget', '`piloop wake` is removed'))
        return _lines_of(toml, found)
    if 'scoring' in (table_at(raw, 'evaluator') or {}):
        found.append(('[evaluator] scoring', 'evaluator.scoring', 'piloop scores what an implementer submits'))
    memory = 'research memory is removed'
    if 'memory' in (table_at(raw, 'plugins') or {}):
        found.append(('[plugins.memory]', 'plugins.memory', memory))
    sections = table_at(raw, 'plugins', 'section') or {}
    gone = {LESSONS} | {n for n, t in sections.items() if isinstance(t, dict) and t.get('provider') == LESSONS}
    found += [(f'[plugins.section.{n}]', f'plugins.section.{n}', memory) for n in sorted(gone & set(sections))]
    named = {'plugins.section.use': sections.get('use')} | {
        f'plugins.role.{role}.sections': (table_at(raw, 'plugins', 'role', role) or {}).get('sections')
        for role in table_at(raw, 'plugins', 'role') or {}
    }
    for key, names in named.items():
        if isinstance(names, list) and (listed := [n for n in names if isinstance(n, str) and n in gone]):
            table, _, last = key.rpartition('.')
            found.append((f'{", ".join(map(repr, listed))} in [{table}] {last}', key, memory))
    return _lines_of(toml, found)


def _lines_of(toml: Path, found: list[tuple[str, str, str]]) -> list[str]:
    """``found`` as ``removed_keys`` gives it: one line for each, at the first line of ``toml`` that sets it."""
    return [
        f'{toml}:{_line(toml, key)}: delete {what}: piloop 0.8.0 removed it ({why}). See {UPGRADING}'
        for what, key, why in found
    ]


def _line(toml: Path, key: str) -> int:
    """The number of the line of ``toml`` that sets the dotted ``key``, or 0 when none does (an inline table's key).

    That is its table's header, or its last part under the header of the table above it.
    """
    # a header may end with a comment; a key's line is read by its name alone
    lines = [
        line.partition('#')[0].strip() if line.lstrip().startswith('[') else line.strip()
        for line in toml.read_text().splitlines()
    ]
    if f'[{key}]' in lines:
        return lines.index(f'[{key}]') + 1
    table, _, last = key.rpartition('.')
    if f'[{table}]' not in lines:
        return 0
    start = lines.index(f'[{table}]') + 1
    for n, line in enumerate(lines[start:], start + 1):
        if line.startswith('['):
            return 0
        if line.partition('=')[0].strip() == last:
            return n
    return 0
