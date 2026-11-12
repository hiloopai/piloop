"""The daemon's terms on its home (``Term``): the processes that serve its API read the current one from the database.

A term begins when the daemon serves, and stops when the daemon begins to stop. The newest term is the current one.
"""

import sqlite3
from contextlib import closing
from pathlib import Path

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from piloop.db.models import Term, utcnow


def newest(s: Session) -> int:
    """The newest term's epoch, or 0 before any daemon served this home."""
    return s.scalar(select(func.max(Term.epoch))) or 0


def newest_in(database: Path) -> int:
    """The newest term's epoch in the database file ``database``, read before anything opens it: 0 without one.

    A leader provider reads it before its restore replaces the file (``piloop.plugins.leader``), so a pooled connection
    must not hold the old file open.
    """
    if not database.is_file():
        return 0
    with closing(sqlite3.connect(f'file:{database}?mode=ro', uri=True)) as con:
        try:
            return con.execute('SELECT max(epoch) FROM terms').fetchone()[0] or 0
        except sqlite3.OperationalError:  # never migrated this far: no terms table
            return 0


def running_in(database: Path) -> tuple[int, int, str | None] | None:
    """The newest term in the database file ``database`` while its daemon runs: its epoch, pid and listen URL."""
    if not database.is_file():
        return None
    with closing(sqlite3.connect(f'file:{database}?mode=ro', uri=True)) as con:
        try:
            row = con.execute(
                'SELECT epoch, pid, listen, stopping_at FROM terms ORDER BY epoch DESC LIMIT 1'
            ).fetchone()
        except sqlite3.OperationalError:  # never migrated this far: no terms table
            return None
    return None if row is None or row[3] is not None else (row[0], row[1], row[2])


def begin(s: Session, epoch: int, pid: int, listen: str | None) -> Term:
    """Add term ``epoch`` of the daemon ``pid``, which serves other machines on ``listen``. The caller commits.

    The leader provider gave the epoch (``piloop.plugins.leader``).
    """
    term = Term(epoch=epoch, pid=pid, listen=listen, started_at=utcnow())
    s.add(term)
    s.flush()
    return term


def current(s: Session) -> Term | None:
    """The newest term, or None before any daemon served this home."""
    return s.scalars(select(Term).order_by(Term.epoch.desc()).limit(1)).first()


def stop(s: Session) -> None:
    """The current term stops from now, unless it stops already. The caller commits."""
    newest = select(func.max(Term.epoch)).scalar_subquery()
    s.execute(update(Term).where(Term.epoch == newest, Term.stopping_at.is_(None)).values(stopping_at=utcnow()))


def stopping(s: Session) -> bool:
    """Whether the daemon of the current term stops. False before any term."""
    term = current(s)
    return term is not None and term.stopping_at is not None
