"""The table of ``local``: one row for each archiving of a campaign's workspace."""

import sqlite3
from datetime import datetime
from pathlib import Path

from covalence import Cid
from sqlalchemy import CheckConstraint, ForeignKey, Index, text
from sqlalchemy.orm import Mapped, composite, mapped_column

from piloop import deprecated
from piloop.db.models import NOW, Base, CidText, utcnow
from piloop.db.schema import Refusal, Schema
from piloop.db.triggers import on
from piloop.ids import CampaignId, TargetKind, TargetRef
from piloop.store.tree import RAW


class Archive(Base):
    """A part of a campaign's shared workspace that the daemon moved into the artifact store (``store``).

    One row per archiving: a subject's top-level entries (``subject`` set) once it settled (``subject.settled``), and
    whatever is left when the campaign stops (``subject`` NULL). ``manifest`` names the stored tree, once its every
    blob is in the store. The local copies stay until the daemon prunes them, by age or when the disk is short
    (``pruned_at``). ``piloop restore`` writes pruned files back. Written by the daemon only.
    """

    __tablename__ = 'archives'
    __table_args__ = (
        Index('ix_archives_campaign_subject', 'campaign_id', 'subject_kind', 'subject_id'),
        CheckConstraint('(subject_kind IS NULL) = (subject_id IS NULL)', name='archives_subject'),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    campaign_id: Mapped[CampaignId] = mapped_column(ForeignKey('campaigns.id', ondelete='CASCADE'))
    subject_kind: Mapped[TargetKind | None] = mapped_column(ForeignKey('target_kinds.id', name='targetkind'))
    subject_id: Mapped[int | None]
    subject: Mapped[TargetRef | None] = composite('subject_kind', 'subject_id')
    """The settled subject whose entries this is; NULL for what a stopped campaign left. No foreign key: the archive
    outlives a deleted subject."""
    manifest: Mapped[Cid | None] = mapped_column(CidText)
    """The CID of the stored manifest (path → blob, size, mode) in the artifact store; NULL when there was nothing
    to archive."""
    files: Mapped[int] = mapped_column(default=0, server_default=text('0'))
    """Files and symlinks archived."""
    bytes: Mapped[int] = mapped_column(default=0, server_default=text('0'))
    """Their total size, uncompressed."""
    created_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=NOW)
    pruned_at: Mapped[datetime | None]
    """When the daemon removed the workspace copies that still held what was stored. NULL while they may be there."""
    complete: Mapped[bool] = mapped_column(server_default=text('1'))
    """Whether this row ends its job: the last batch of a job that stored everything. A job that stopped short (no
    room, a failure, a stop of the daemon) has only incomplete rows, so it stays due."""


@deprecated.until(
    '0.5.0', 'a 0.3 home with a JSON manifest of 0.2: delete json_manifests, and Schema.refuses if unused'
)
def json_manifests(con: sqlite3.Connection) -> Refusal | None:
    """Why a home whose archive still names a JSON manifest of piloop 0.2 does not migrate, else None.

    piloop 0.3 stored each one again at its start, unless the store could not give it back. This piloop reads none.
    """
    if con.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'archives'").fetchone() is None:
        return None  # a schema from before the archives
    rows = con.execute('SELECT id, manifest FROM archives WHERE manifest IS NOT NULL ORDER BY id').fetchall()
    if not (old := [archive for archive, manifest in rows if Cid(manifest).codec == RAW]):
        return None
    return Refusal(
        f'archive {", ".join(map(str, old))} still names a manifest of piloop 0.2',
        "install piloop 0.3 (`uv tool install 'piloop==0.3.*'`) and run `piloop migrate`, which stores it again and"
        ' says what to do when the store cannot give it back, then install this piloop again and run `piloop upgrade`',
    )


TABLES = (Archive,)
SCHEMA = Schema(
    tables=TABLES, versions=Path(__file__).parent / 'migrations', triggers=on(TABLES), refuses=json_manifests
)
