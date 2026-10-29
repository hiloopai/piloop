"""The commands on the database: ``sql``, ``schema`` and ``migrate``."""

import json
import sqlite3
import sys
from contextlib import closing
from functools import partial
from pathlib import Path
from typing import Annotated

import cyclopts
from rich.table import Table
from rich.text import Text

from piloop.actor import Actor
from piloop.cli import common, local
from piloop.db import sql
from piloop.db.schema import CORE
from piloop.db.session import Database, Stamps, backups, described, stamps
from piloop.net import tokens
from piloop.net.acting import OWNER, MayNotAct, acts_as
from piloop.plugins.section.contract import SECTIONS, Guide, guides
from piloop.plugins.vcs.contract import VCS


def sql_cmd(
    statement: Annotated[
        str,
        # allow_leading_hyphen: cyclopts reads a bare `-` as an unknown option otherwise
        cyclopts.Parameter(help='One SQL statement, or - to read it from stdin.', allow_leading_hyphen=True),
    ],
    /,
) -> None:
    """Run one SQL statement on the piloop database.

    These are denied: schema changes, ATTACH, transactions, and writes to `alembic_version` and `session_actor`. Only
    the PRAGMAs that describe the schema are allowed. A query prints at most 500 rows. With `-`, the statement is read
    from stdin: piped in from a quoted heredoc (`<<'EOF'`), an agent's text reaches the database as written, where a
    double-quoted argument would let the shell run what is in backticks or `$(…)` first.
    """
    # TODO: remove `-` and the briefs' heredocs once agents write through typed write verbs and `piloop mcp`.
    if statement == '-':
        if sys.stdin.isatty():
            raise SystemExit("piloop sql - reads the statement from stdin: pipe it in with a quoted heredoc (<<'EOF')")
        statement = sys.stdin.read()
        if not statement.strip():
            raise SystemExit('piloop sql - read no statement from stdin')
    if (shell := sql.dot_command(statement)) is not None:
        raise SystemExit(f'sql error: {shell}')
    local.refuse_other_schema()
    database = common.settings.home / 'piloop.db'
    with closing(sql.connect(database, actor=_signer())) as con:
        try:
            result = sql.run(con, statement)
        except sqlite3.Error as e:
            holder = sql.lock_holder(e, database)
            raise SystemExit(f'sql error: {e}' if holder is None else f'sql error: {e} ({holder})') from e
    if result.columns:
        table = Table(*result.columns, show_lines=False)
        for row in result.rows:
            # Text, not str: a cell is data, so '[voting]' from snippet() or a '[wip]' title is not rich markup
            table.add_row(*(Text(_cell(v)) for v in row))
        common.console.print(table)
        if result.truncated:
            common.console.print(f'[dim]truncated to {sql.ROW_LIMIT} rows[/]')
    else:
        common.console.print(f'{result.changes} row(s) changed, last insert id {result.last_insert_rowid}')


def _signer() -> Actor:
    """Whom ``piloop sql`` signs as: ``PILOOP_ACTOR``, as ``acts_as`` lets ``PILOOP_AGENT_TOKEN``'s agent or the owner."""
    token = common.settings.agent_token
    db = Database(common.settings.database_url, actor=common.settings.actor)  # sql_cmd checked its schema
    with closing(db), db.session() as s:
        caller = OWNER if token is None else tokens.verify(s, token.get_secret_value())
        if caller is None:
            raise SystemExit('sql error: PILOOP_AGENT_TOKEN is unknown, or its agent ended')
        try:
            return acts_as(caller, common.settings.actor, partial(tokens.tokenless, s))
        except MayNotAct as e:
            raise SystemExit(f'sql error: {e}') from e


def schema(*names: str) -> None:
    """Print the guides to the database and every table, or only the guides that you name.

    A brief has the guides of its agent's role; this prints any guide, also one that no brief has, such as
    `orchestration`. The examples name no campaign, and say what the default vcs provider writes.

    Args:
        names: The guides to print, such as `provenance` or `queries`. Without one, every guide, then every table.
    """
    vcs = VCS.selected({})
    found = SECTIONS.instances({})
    installed = guides()
    if unknown := [n for n in names if n not in installed]:
        raise SystemExit(f'no guide {", ".join(unknown)}; the guides are {", ".join(installed)}')
    shown = [found[n] for n in names or installed]
    parts = [f'## {g.TITLE}\n\n{g.text(vcs)}' for g in shown if isinstance(g, Guide)]
    if not names:
        parts.append(f'## Schema\n\n```sql\n{sql.schema_ddl()}\n```')
    common.console.print('\n\n'.join(parts), markup=False, highlight=False, soft_wrap=True)


def _cell(value: object) -> str:
    return '' if value is None else json.dumps(value) if isinstance(value, dict | list) else str(value)


def migrate_cmd() -> None:
    """Migrate the database to the schema of this piloop, and print what changed and where its backup is.

    Before it migrates, it copies the database next to it (`migration_backups` in config.toml). Only this command, `piloop serve` when it starts and `piloop upgrade` migrate. Every other command
    refuses a database at another schema revision, and says what to run. It refuses to migrate under the running
    daemon of an older piloop, and refuses an agent that the daemon launched.
    """
    common.refuse_agent('migrate')
    old = stamps(local.home() / 'piloop.db')
    db = Database(common.settings.database_url, actor=common.settings.actor)
    try:
        backup = local.migrate(db)
        _print_schema(old, stamps(common.settings.home / 'piloop.db'), backup)
    finally:
        db.close()


def _print_schema(old: Stamps, new: Stamps, backup: Path | None) -> None:
    """What ``migrate`` changed: each chain's revision before and after, and the backup of core's."""
    if old == new:
        newest = backups(common.settings.home / 'piloop.db')
        common.console.print(
            f'schema: {described(new)}, current' + (f'; the newest backup: {newest[-1]}' if newest else ''),
            markup=False,
            highlight=False,
            soft_wrap=True,
        )
        return
    moved = ', '.join(f'{name} {old.get(name, "none")} → {at}' for name, at in new.items() if old.get(name) != at)
    match old.get(CORE):
        case None:
            done = ''
        case core if backup is not None:
            done = f'; backup of core {core}: {backup}'
        case _:
            done = '; no backup (migration_backups = 0)'
    common.console.print(f'schema: {moved}{done}', markup=False, highlight=False, soft_wrap=True)
