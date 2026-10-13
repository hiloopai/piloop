"""Indexes: tables in piloop.db that a builder keeps in sync with a source table, for agents to query by SQL.

An index is an entry in ``INDEXES`` with three hooks. ``create`` is its DDL, which a migration runs. ``triggers`` are
the persistent triggers that update it on every write to the source; they are part of
``piloop.db.triggers.PERSISTENT``. ``rebuild`` syncs it again from the whole source: the migration that creates it
runs it, and so does ``piloop index rebuild``. Today every index is full-text search: ``ideas_fts``,
``comments_fts`` and ``documents_fts``. A semantic index (sqlite-vec over the same tables, say) is one more builder,
whose ``rebuild`` computes the vectors.

Full-text search makes "has anyone tried soft voting" one ``MATCH`` query, however long the campaign is. An
external-content table (``content='ideas'``) stores only the index, never a second copy of the text. So three
triggers on each table must tell the index about every insert, delete and change of the indexed columns.

Agents read the indexes and never write them: the authorizer (``piloop.db.sql``) lets only the sync triggers write.
"""

from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import Connection

from piloop.db.ddl import Trigger

FTS_TOKENIZER = 'porter unicode61 remove_diacritics 2'
"""Hypotheses are English text. Porter stemming makes vote, votes and voting one term (and encoder and encoding), so a
search matches every form of a word. unicode61 with remove_diacritics 2 makes naïve match naive, and the same for any
other accented spelling. The cost is that a prefix query (``ensem*``) matches stems, not words."""


class Index(Protocol):
    """A builder: one table that agents query, kept in sync with ``source``."""

    @property
    def name(self) -> str:
        """The table that agents query. It is also the key in ``INDEXES`` and what ``piloop index rebuild`` takes."""
        ...

    @property
    def source(self) -> str:
        """The table that the index is built from."""
        ...

    def create(self) -> tuple[str, ...]:
        """The DDL that creates the index, each statement ending with ``;``. Never its triggers."""
        ...

    def triggers(self) -> tuple[Trigger, ...]:
        """The persistent triggers that keep the index up to date. Each one ``requires`` the index."""
        ...

    def rebuild(self, con: Connection) -> None:
        """Sync the index with every row of ``source``, on a connection that the authorizer does not restrict."""
        ...


@dataclass(frozen=True)
class Fts:
    """An FTS5 external-content index over ``columns`` of ``source``: ``<source>_fts``. Its rowid is the row's ``id``."""

    source: str
    columns: tuple[str, ...]

    @property
    def name(self) -> str:
        return f'{self.source}_fts'

    def create(self) -> tuple[str, ...]:
        return (
            f"CREATE VIRTUAL TABLE {self.name} USING fts5({', '.join(self.columns)}, content='{self.source}', "
            f"content_rowid='id', tokenize='{FTS_TOKENIZER}');",
        )

    def triggers(self) -> tuple[Trigger, ...]:
        """The three sync triggers.

        This is the standard pattern for external content. The index never reads the table on a write, so a change is
        an explicit ``'delete'`` of the old text, then an insert of the new text. The old text must be exactly what
        was indexed, so it comes from ``OLD.*``. ``OF id, <columns>`` limits the update trigger to the indexed columns
        and the rowid. Without it, a change of status or priority, and the ``updated_at`` and ``updated_by`` updates
        that follow every UPDATE, would each index the row again three times.
        """
        fts, names = self.name, ', '.join(self.columns)
        new = ', '.join(f'NEW.{c}' for c in self.columns)
        old = ', '.join(f'OLD.{c}' for c in self.columns)
        insert = f'INSERT INTO {fts} (rowid, {names}) VALUES (NEW.id, {new})'
        delete = f"INSERT INTO {fts} ({fts}, rowid, {names}) VALUES ('delete', OLD.id, {old})"
        return (
            Trigger(f'{fts}_insert', self.source, 'AFTER INSERT', body=insert, requires=fts),
            Trigger(f'{fts}_delete', self.source, 'AFTER DELETE', body=delete, requires=fts),
            Trigger(
                f'{fts}_update',
                self.source,
                'AFTER UPDATE',
                of=('id', *self.columns),
                body=f'{delete};\n{insert}',
                requires=fts,
            ),
        )

    def rebuild(self, con: Connection) -> None:
        con.exec_driver_sql(f"INSERT INTO {self.name} ({self.name}) VALUES ('rebuild')")


INDEXES: dict[str, Index] = {
    index.name: index
    for index in (
        Fts('ideas', ('title', 'hypothesis', 'plan')),
        Fts('comments', ('body',)),
        Fts('documents', ('title', 'body')),
    )
}
"""Every index, by the name that agents query. A new index is an entry here and a migration that runs its
``create``, calls ``recreate`` for its source and runs its ``rebuild``. Then run ``make docs``."""
SYNC: tuple[Trigger, ...] = tuple(t for index in INDEXES.values() for t in index.triggers())
"""Every sync trigger, in the order of ``PERSISTENT``."""
TRIGGER_NAMES = frozenset(t.name for t in SYNC)
"""The only triggers that may write an index. The authorizer lets their bodies through and denies all else."""


def is_index_object(name: str) -> bool:
    """Whether ``name`` is an index, one of its shadow tables (for example ``ideas_fts_data``) or a sync trigger.

    None of them is a SQLAlchemy model, so autogenerate must not see them. Agents must not write any of them.
    """
    return name in INDEXES or any(name.startswith(f'{index}_') for index in INDEXES)
