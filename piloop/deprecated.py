"""Compat code that piloop keeps until a set release: deprecated ``piloop.toml`` keys, old names and old behavior.

Mark each piece of compat code with ``until``: the release that deletes it and what to delete. A test finds every
marker and fails once ``pyproject.toml`` reaches its release, so the code goes before that release ships.

A deprecated key still loads, with a warning, until that release. To deprecate a key, write a bridge: a function that
takes the file path, the parsed TOML and the release that removes the key, changes the old key to the new form in place
and calls ``warn`` with that release. A key that only moved is a ``Move``: ``move_keys`` applies one to the file, and
the revision that rewrites the stored copies applies the same moves. Declare it with ``bridge``, which marks it as
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
from typing import TYPE_CHECKING, Protocol

from pydantic import InstanceOf, TypeAdapter

from piloop.plugins.host import PluginError, Scope, Slot
from piloop.version import Version

if TYPE_CHECKING:  # the sections import this module
    pass


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


def move_keys(toml: Path, raw: dict[str, object], move: Move, removed_in: Version) -> None:
    """Apply ``move`` to the parsed file, and warn at the first old key it moved."""
    if found := move.apply(raw):
        parts = found[0].split('.')
        # an inline table sets none of its keys on a line of its own: warn at the key that holds it
        key = next((k for n in range(len(parts), 0, -1) if _lines(toml, k := '.'.join(parts[:n]))), found[0])
        warn(toml, key, f'{_written(move.old)} is deprecated; write {_written(move.new)}', removed_in=removed_in)


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


def show_in_cli() -> None:
    """Show each deprecation once. By default, Python hides a ``DeprecationWarning`` outside ``__main__``."""
    warnings.simplefilter('default', PiloopDeprecationWarning)


def warn(toml: Path, key: str, message: str, *, removed_in: Version, nth: int = 0) -> None:
    """Warn at the line of ``toml`` that sets ``key`` for the ``nth`` time, or at line 0 if no line sets it.

    A dotted ``key`` (``plugins.role.orchestrator``) is set by its table header, or by a line that sets its last part. The
    output looks like a compiler error: ``piloop.toml:12: PiloopDeprecationWarning: …``, then the line.
    """
    lines = _lines(toml, key)
    lineno = lines[nth] if nth < len(lines) else 0
    text = f'{message}; removed in piloop {removed_in}'
    warnings.warn_explicit(text, PiloopDeprecationWarning, str(toml), lineno, registry=_shown)


def dropped(message: str, *, removed_in: str) -> None:
    """Warn, once in the process, that a validator of the config took out a key that is gone: a file's or a stored one.

    A validator of the config calls it, so a file and a campaign's stored config go one path. Mark the validator with
    ``until``.
    """
    text = f'{message}; removed in piloop {Version.parse(removed_in)}'
    warnings.warn_explicit(text, PiloopDeprecationWarning, 'piloop config', 0, registry=_shown)


def nth(toml: Path, table: str, key: str) -> int:
    """How many lines of ``toml`` set ``key`` before the header of ``table``: ``warn``'s ``nth`` for ``table.key``."""
    lines = toml.read_text().splitlines()
    header = next((n for n, line in enumerate(lines) if line.strip() == f'[{table}]'), len(lines))
    return sum(line.partition('=')[0].strip() == key for line in lines[:header])


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


@bridge('0.7.0', '[tracks] share is removed: delete track_shares')
def track_shares(toml: Path, raw: dict[str, object], removed_in: Version) -> None:
    """``share`` of a ``[tracks.<name>]`` is removed: workers claim any open idea, so no track has a share of the slots.

    piloop drops it, and drops a table that it leaves empty.
    """
    tracks = table_at(raw, 'tracks') or {}
    for name, table in [(n, t) for n, t in tracks.items() if isinstance(t, dict) and 'share' in t]:
        del table['share']
        if not table:
            del tracks[name]
        warn(
            toml,
            f'tracks.{name}.share',
            'workers claim any open idea now, so [tracks] share is removed; delete it',
            removed_in=removed_in,
            nth=nth(toml, f'tracks.{name}', 'share'),
        )


@bridge('0.7.0', '[agents] relay_wait and relay_deadline are removed: delete relay_waits', Scope.HOME)
def relay_waits(toml: Path, raw: dict[str, object], removed_in: Version) -> None:
    """``relay_wait`` and ``relay_deadline`` of ``[agents]`` are removed. piloop drops them.

    ``piloop submit`` returns at once, so the relay of a worker elsewhere calls it once.
    """
    agents = table_at(raw, 'agents') or {}
    for key in ('relay_wait', 'relay_deadline'):
        if key in agents:
            del agents[key]
            message = f'`piloop submit` returns at once, so [agents] {key} is removed; delete it'
            warn(toml, f'agents.{key}', message, removed_in=removed_in)


@bridge('0.7.0', '[evaluation] wake_after_tries is removed: delete wake_after_tries', Scope.HOME)
def wake_after_tries(toml: Path, raw: dict[str, object], removed_in: Version) -> None:
    """``wake_after_tries`` of ``[evaluation]`` is removed: no agent wakes on an evaluation's retries. piloop drops it.

    An evaluation that fails ``stop_after_tries`` times in a row for the machine still escalates.
    """
    evaluation = table_at(raw, 'evaluation') or {}
    if 'wake_after_tries' in evaluation:
        del evaluation['wake_after_tries']
        message = (
            'no agent wakes on the retries of an evaluation now, so [evaluation] wake_after_tries is removed; delete it'
        )
        warn(toml, 'evaluation.wake_after_tries', message, removed_in=removed_in)


MEMORY_SECTION = 'lessons'
"""The section of research memory: the lessons of past campaigns on the repo."""


@bridge('0.8.0', 'research memory is removed in 0.7: delete research_memory and MEMORY_SECTION')
def research_memory(toml: Path, raw: dict[str, object], removed_in: Version) -> None:
    """``[plugins.memory]`` and the ``lessons`` section are removed with research memory. piloop drops them.

    The lessons, documents and journal are gone: an orchestrator keeps its notes as ``note`` comments on the campaign.
    A section table of ``provider = "lessons"`` goes too, and so does its name in ``[plugins.section] use`` and in a
    role's ``sections``.
    """

    def dropped(key: str, what: str, at: int = 0) -> None:
        warn(toml, key, f'research memory is removed, so {what} is removed; delete it', removed_in=removed_in, nth=at)

    plugins = table_at(raw, 'plugins') or {}
    if 'memory' in plugins:
        del plugins['memory']
        dropped('plugins.memory', '[plugins.memory]')
    sections = table_at(raw, 'plugins', 'section') or {}
    gone = {MEMORY_SECTION} | {
        name for name, table in sections.items() if isinstance(table, dict) and table.get('provider') == MEMORY_SECTION
    }
    for name in sorted(gone & set(sections)):
        del sections[name]
        dropped(f'plugins.section.{name}', f'[plugins.section.{name}]')
    if isinstance(use := sections.get('use'), list) and (named := [n for n in use if isinstance(n, str) and n in gone]):
        sections['use'] = [n for n in use if n not in named]
        at = nth(toml, 'plugins.section', 'use')
        dropped('plugins.section.use', f'{", ".join(named)} in [plugins.section] use', at)
    for role in table_at(raw, 'plugins', 'role') or {}:
        table = table_at(raw, 'plugins', 'role', role) or {}
        if isinstance(listed := table.get('sections'), list) and (
            named := [n for n in listed if isinstance(n, str) and n in gone]
        ):
            table['sections'] = [n for n in listed if n not in named]
            at = nth(toml, f'plugins.role.{role}', 'sections')
            dropped(f'plugins.role.{role}.sections', f'{", ".join(named)} in [plugins.role.{role}] sections', at)


@bridge('0.8.0', '[agents] wake_budget is removed with piloop wake: delete wake_budget', Scope.HOME)
def wake_budget(toml: Path, raw: dict[str, object], removed_in: Version) -> None:
    """``wake_budget`` of ``[agents]`` is removed with ``piloop wake``. piloop drops it."""
    agents = table_at(raw, 'agents') or {}
    if 'wake_budget' in agents:
        del agents['wake_budget']
        message = '`piloop wake` is removed with research memory, so [agents] wake_budget is removed; delete it'
        warn(toml, 'agents.wake_budget', message, removed_in=removed_in)
