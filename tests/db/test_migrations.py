"""The schema at head: what the migrations create, that models and migrations agree, and that 0001 upgrades to head."""

import json
import re
import resource
import shutil
import signal
import sqlite3
import threading
import time
from contextlib import closing
from json import dumps, loads
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError

from piloop.actor import System
from piloop.db import session
from piloop.db.models import Campaign
from piloop.db.schema import CORE
from piloop.db.session import (
    CAS_ATTRIBUTE,
    MIGRATIONS,
    RELEASES,
    BackupFailed,
    Database,
    MigrationRevision,
    Release,
    SchemaAhead,
    SchemaTooOld,
    alembic_config,
    backups,
    fingerprints,
    heads,
    history,
    migrate,
    revision,
    schema_mismatch,
    scripts,
    stamps,
)
from piloop.db.triggers import PERSISTENT
from piloop.plugins.campaign_type.kit.backlog.tables import Idea
from piloop.version import Version, installed
from scripts.release_triggers import PATH as RELEASE_TRIGGERS, ReleaseTriggers
from tests.conftest import bare_config, disk
from tests.test_deprecated import piloop_version

INBOX = 'ix_comments_inbox'


INBOX_WHERE = 'priority IS NOT NULL AND acked_at IS NULL'


def alembic(path: Path) -> AlembicConfig:
    """The Alembic config of the database at ``path``, with the CAS of its home, as ``migrate`` makes it."""
    cfg = AlembicConfig()
    cfg.set_main_option('script_location', str(MIGRATIONS))
    cfg.set_main_option('sqlalchemy.url', f'sqlite:///{path}')
    cfg.attributes[CAS_ATTRIBUTE] = disk(path.parent)
    return cfg


def test_head_has_every_table_and_trigger_and_matches_the_models(tmp_path: Path) -> None:
    path = tmp_path / 'piloop.db'
    cfg = alembic(path)
    command.upgrade(cfg, 'head')
    with closing(sqlite3.connect(path, isolation_level=None)) as con:
        tables = [t for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
        assert tables == [
            'agent_sessions',
            'alembic_version',
            'archives',
            'campaigns',
            'candidates',
            'comment_kinds',
            'comments',
            'comments_fts',
            'comments_fts_config',
            'comments_fts_data',
            'comments_fts_docsize',
            'comments_fts_idx',
            'documents',
            'documents_fts',
            'documents_fts_config',
            'documents_fts_data',
            'documents_fts_docsize',
            'documents_fts_idx',
            'evaluations',
            'evaluators',
            'events',
            'first_transfers',
            'idea_links',
            'ideas',
            'ideas_fts',
            'ideas_fts_config',
            'ideas_fts_data',
            'ideas_fts_docsize',
            'ideas_fts_idx',
            'journal',
            'lease_workers',
            'leases',
            'lessons',
            'limits',
            'measurements',
            'migrations',
            'runs',
            'secrets',
            'sqlite_sequence',
            'target_kinds',
            'techniques',
            'tokens',
            'worker_instances',
        ], 'an external-content FTS5 index has no _content shadow: the text stays in the base table only'
        triggers = [t for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type = 'trigger' ORDER BY name")]
        assert triggers == sorted(t.name for t in PERSISTENT), 'the package lists what the migrations install'
        con.execute("INSERT INTO campaigns (name, path, config) VALUES ('p', '/p', '{}')")
        con.execute("INSERT INTO candidates (campaign_id, tree_sha, revision) VALUES (1, 't', 'c')")
        with pytest.raises(
            sqlite3.IntegrityError, match='UNIQUE constraint failed: candidates.campaign_id, candidates.tree_sha'
        ):
            con.execute("INSERT INTO candidates (campaign_id, tree_sha, revision) VALUES (1, 't', 'other commit')")
        with pytest.raises(sqlite3.IntegrityError, match='CHECK constraint failed'):
            con.execute("UPDATE candidates SET verdict = 'maybe' WHERE id = 1")
        assert con.execute('SELECT status, stopped_reason FROM campaigns').fetchall() == [('active', None)]
        with pytest.raises(sqlite3.IntegrityError, match='CHECK constraint failed'):
            con.execute("UPDATE campaigns SET status = 'paused' WHERE id = 1")
    command.check(cfg)
    command.downgrade(cfg, 'base')
    with closing(sqlite3.connect(path, isolation_level=None)) as con:
        assert con.execute('PRAGMA user_version').fetchone() == (0,)
        assert (
            con.execute(  # SQLite keeps sqlite_sequence, which AUTOINCREMENT made, and never drops it
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT IN ('alembic_version', 'sqlite_sequence')"
            ).fetchall()
            == []
        )


def test_each_chain_at_head_matches_its_tables(tmp_path: Path) -> None:
    path = tmp_path / 'piloop.db'
    engine = create_engine(f'sqlite:///{path}')
    migrate(engine, cas=disk(tmp_path), backups=0, daemon=None)
    engine.dispose()
    for name in heads():
        command.check(alembic_config(f'sqlite:///{path}', name, disk(tmp_path)))
    with closing(sqlite3.connect(path)) as con:
        assert dict(con.execute('SELECT name, package FROM migrations')) == dict.fromkeys(heads(), 'piloop')


def schema(path: Path) -> list[tuple[str, ...]]:
    with closing(sqlite3.connect(f'file:{path}?mode=ro', uri=True)) as con:
        return con.execute('SELECT type, name, sql FROM sqlite_master ORDER BY name').fetchall()


def test_a_database_of_the_last_release_upgrades_to_every_persistent_trigger_as_the_code_renders_it(
    tmp_path: Path,
) -> None:
    """A database keeps the triggers of the release that created them until a migration creates them again, so a
    change to a trigger's SQL (to an event model that one renders, for example) needs a migration that recreates it."""
    released = ReleaseTriggers.model_validate_json(RELEASE_TRIGGERS.read_text())
    assert released.version == str(piloop_version()), (
        'a release records its triggers: uv run -m scripts.release_triggers'
    )
    path = tmp_path / 'piloop.db'
    for chain, head in released.heads.items():
        command.upgrade(alembic_config(f'sqlite:///{path}', chain, disk(tmp_path)), head)
    with closing(sqlite3.connect(path, isolation_level=None)) as con:
        for (name,) in con.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'").fetchall():
            con.execute(f'DROP TRIGGER {name}')
        for sql in released.triggers.values():
            con.execute(sql)
    engine = create_engine(f'sqlite:///{path}')
    migrate(engine, cas=disk(tmp_path), backups=0, daemon=None)
    engine.dispose()
    with closing(sqlite3.connect(path)) as con:
        installed = dict(con.execute("SELECT name, sql FROM sqlite_master WHERE type = 'trigger'"))
    assert installed == {t.name: t.sql().removesuffix(';') for t in PERSISTENT}, (
        'a persistent trigger changed: add a migration that recreates it (recreate or install)'
    )


def test_a_database_at_a_revision_this_build_does_not_have_is_refused_and_left_as_it_was(tmp_path: Path) -> None:
    path = tmp_path / 'piloop.db'
    engine = create_engine(f'sqlite:///{path}')
    migrate(engine, cas=disk(tmp_path), backups=0, daemon=None)
    head = ScriptDirectory.from_config(alembic(path)).get_current_head()
    with closing(sqlite3.connect(path)) as con:
        con.execute("UPDATE alembic_version SET version_num = '0006_role_keys'")
        con.commit()
    before = path.read_bytes()
    this = re.escape(installed())
    with pytest.raises(
        SchemaAhead,
        match=rf'^{re.escape(str(path))} was migrated by a different piloop build \(core revision 0006_role_keys is not in '
        rf'the history of this {this}, which knows up to {head}\): install the piloop that migrated it \(`piloop '
        r'upgrade` for a newer release\), or set PILOOP_HOME to another directory$',
    ):
        migrate(engine, cas=disk(tmp_path), backups=2, daemon=None)
    assert schema_mismatch(path, None, System.HUMAN) is not None
    assert path.read_bytes() == before and list(tmp_path.glob('piloop.db.bak-*')) == []
    engine.dispose()


TOO_OLD = tuple(
    Release(Version(0, minor, 0), {CORE: MigrationRevision(head)})
    for minor, head in ((1, '0067'), (2, '0068'), (3, '0069'))
)
"""Releases for a piloop 0.4 that migrates from 0.3.0's head, 0069: a database at 0067 is two releases behind it."""


@pytest.mark.parametrize(
    ('stamp', 'origin', 'stops'),
    [
        ('0067', 'piloop 0.1', ('0.2', '0.3')),
        ('0068', 'piloop 0.2', ('0.3',)),
    ],
)
def test_a_database_older_than_the_previous_release_is_refused_and_left_as_it_was(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stamp: str, origin: str, stops: tuple[str, ...]
) -> None:
    """Each release migrates only from the head of the one before it, so a database upgrades one release at a time."""
    monkeypatch.setattr(session, 'RELEASES', TOO_OLD)
    path = tmp_path / 'piloop.db'
    command.upgrade(alembic(path), stamp)
    before = path.read_bytes()
    engine = create_engine(f'sqlite:///{path}')
    this = re.escape(installed())
    steps = ', then '.join(
        rf"install piloop {m} \(`uv tool install 'piloop=={m}\.\*'`\) and run `piloop upgrade`" for m in stops
    )
    with pytest.raises(
        SchemaTooOld,
        match=rf'^{re.escape(str(path))} is from {origin} \(schema revision core {stamp}\), and this {this} migrates from'
        rf' piloop 0\.3 \(schema revision core 0069\) or newer: upgrade one release at a time: {steps}, then install this'
        r' piloop again and run `piloop upgrade`$',
    ):
        migrate(engine, cas=disk(tmp_path), backups=3, daemon=None)
    engine.dispose()
    assert path.read_bytes() == before, 'not migrated'
    assert sorted(p.name for p in tmp_path.iterdir()) == ['piloop.db', 'piloop.db.migrate.lock'], 'no backup, no WAL'


def test_a_database_at_the_previous_release_or_newer_migrates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(session, 'RELEASES', TOO_OLD)
    path = tmp_path / 'piloop.db'
    command.upgrade(alembic(path), '0069')
    engine = create_engine(f'sqlite:///{path}')
    migrate(engine, cas=disk(tmp_path), backups=0, daemon=None)
    engine.dispose()
    assert stamps(path) == heads()


def test_a_database_that_migrate_creates_or_migrates_is_in_wal_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Else the first connections of its clients switch it, and that switch fails while another one reads."""
    monkeypatch.setattr(session, 'RELEASES', TOO_OLD)
    fresh, old = tmp_path / 'fresh.db', tmp_path / 'old.db'
    command.upgrade(alembic(old), '0069')
    for path in (fresh, old):
        engine = create_engine(f'sqlite:///{path}')
        migrate(engine, cas=disk(tmp_path), backups=0, daemon=None)
        engine.dispose()
        with closing(sqlite3.connect(path)) as con:
            assert con.execute('PRAGMA journal_mode').fetchone() == ('wal',), path.name


def test_a_database_from_before_the_first_recorded_release_names_every_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(session, 'RELEASES', TOO_OLD[1:])
    path = tmp_path / 'piloop.db'
    command.upgrade(alembic(path), '0067')
    engine = create_engine(f'sqlite:///{path}')
    with pytest.raises(
        SchemaTooOld,
        match=r' is from a piloop before 0\.2 \(schema revision core 0067\), .*: install piloop 0\.2 .*,'
        r' then install piloop 0\.3 ',
    ):
        migrate(engine, cas=disk(tmp_path), backups=0, daemon=None)
    engine.dispose()


LOCAL_ARCHIVE = 'archive/local'
"""The schema of the built-in archive provider, a plugin's chain."""


def test_a_plugin_chain_older_than_its_head_at_the_previous_release_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The floor holds for every chain that the previous release had, not for core's alone."""
    path = tmp_path / 'piloop.db'
    command.upgrade(alembic(path), 'head')
    command.upgrade(alembic_config(f'sqlite:///{path}', LOCAL_ARCHIVE, disk(tmp_path)), '0002')
    engine = create_engine(f'sqlite:///{path}')
    floor = Release(
        Version(0, 4, 0), {CORE: MigrationRevision(heads()[CORE]), LOCAL_ARCHIVE: MigrationRevision('0003')}
    )
    monkeypatch.setattr(session, 'RELEASES', (*TOO_OLD, floor))
    with pytest.raises(
        SchemaTooOld,
        match=rf' is from piloop 0\.3 \(schema revision core {heads()[CORE]}, archive/local 0002\), and this .* migrates'
        rf' from piloop 0\.4 \(schema revision core {heads()[CORE]}, archive/local 0003\) or newer: .*install piloop 0\.4 ',
    ):
        migrate(engine, cas=disk(tmp_path), backups=0, daemon=None)
    engine.dispose()


def test_a_database_at_a_revision_that_a_baseline_squashed_is_refused_with_the_releases_to_install_first(
    tmp_path: Path,
) -> None:
    """A 0.3 home's core is at 0053, which core's baseline squashed: not another build's, but older than the floor."""
    path = tmp_path / 'piloop.db'
    command.upgrade(alembic(path), '0067')
    with closing(sqlite3.connect(path)) as con:
        con.execute("UPDATE alembic_version SET version_num = '0053'")
        con.commit()
    before = path.read_bytes()
    engine = create_engine(f'sqlite:///{path}')
    with pytest.raises(
        SchemaTooOld,
        match=r' is from piloop 0\.3 \(schema revision core 0053\), and this .* migrates from piloop 0\.4 .*: upgrade one'
        r" release at a time: install piloop 0\.4 \(`uv tool install 'piloop==0\.4\.\*'`\) and run `piloop upgrade`, then",
    ):
        migrate(engine, cas=disk(tmp_path), backups=1, daemon=None)
    engine.dispose()
    assert path.read_bytes() == before and list(tmp_path.glob('piloop.db.bak-*')) == []


def test_the_floor_is_the_head_of_the_previous_minor_release() -> None:
    """``RELEASES`` records each minor release and the head of each of its chains, oldest first; the newest is the floor.

    The release of ``X.Y.0`` records ``X.(Y-1).0`` (CONTRIBUTING.md, Release): at the version bump, this fails until it
    does. Until the next bump, the newest may be the version in ``pyproject.toml``, which is then released.
    """
    this = piloop_version()
    versions = [r.version for r in RELEASES]
    assert versions == sorted(versions) and all(v.patch == 0 for v in versions), 'X.Y.0 releases, oldest first'
    assert all(CORE in r.heads for r in RELEASES), "every release has core's head"
    for name, script in scripts().items():
        chain = history(script)
        recorded = [r.heads[name] for r in RELEASES if name in r.heads]
        assert all(head in chain for head in recorded), f'every head of {name} is a revision of its chain'
        positions = [chain.index(head) for head in recorded]
        assert positions == sorted(positions), f'each head of {name} is after the head of the release before it'
    previous = Version(this.major, this.minor - 1, 0)
    assert previous <= versions[-1] <= this, (
        f'piloop {this} migrates from the heads of piloop {previous}: add {previous} and the head of each chain to'
        ' RELEASES'
    )


def branch(tmp_path: Path) -> AlembicConfig:
    """The migrations of a branch cut at 0069 that adds its own, different 0070."""
    copy = tmp_path / 'branch'

    def after_0069(_dir: str, names: list[str]) -> list[str]:
        return [n for n in names if n == '__pycache__' or (n[:4].isdigit() and n[:4] > '0069')]

    shutil.copytree(MIGRATIONS, copy, ignore=after_0069)
    (copy / 'versions' / '0070_role_keys.py').write_text(
        'from alembic import op\n\n'
        "revision = '0070'\ndown_revision = '0069'\nbranch_labels = None\ndepends_on = None\n\n\n"
        'def upgrade() -> None:\n    op.execute("UPDATE campaigns SET config = \'{}\'")\n\n\n'
        'def downgrade() -> None:\n    pass\n'
    )
    cfg = alembic(tmp_path / 'piloop.db')
    cfg.set_main_option('script_location', str(copy))
    return cfg


def test_a_database_that_another_branch_migrated_to_a_revision_of_the_same_id_is_refused_and_left_as_it_was(
    tmp_path: Path,
) -> None:
    path = tmp_path / 'piloop.db'
    cfg = branch(tmp_path)
    command.upgrade(cfg, 'head')
    with closing(sqlite3.connect(path)) as con:  # what the branch's migrate records
        mark = fingerprints(ScriptDirectory.from_config(cfg))['0070']
        con.execute("INSERT INTO migrations (name, package, fingerprint) VALUES ('core', 'piloop', ?)", (mark,))
        con.commit()
    engine = create_engine(f'sqlite:///{path}')
    before, tables = path.read_bytes(), schema(path)
    assert revision(path) == '0070'
    refused = r'core revision 0070 is not in the history of this .*: this build has a different 0070 or one before it\)'
    with pytest.raises(SchemaAhead, match=refused):
        migrate(engine, cas=disk(tmp_path), backups=2, daemon=None)
    assert re.search(refused, str(schema_mismatch(path, None, System.HUMAN)))
    assert path.read_bytes() == before and schema(path) == tables and list(tmp_path.glob('piloop.db.bak-*')) == []
    engine.dispose()


def test_the_fingerprint_of_each_revision_after_a_baseline_is_what_the_squashed_chain_gave_it() -> None:
    """A database records the fingerprint of its revision, a hash of the names of the revisions up to it. A baseline
    counts the names of the revisions that it squashed, so a database migrated before the squash is not refused."""
    assert fingerprints(ScriptDirectory(str(MIGRATIONS))).items() >= {('0067', 1855909650), ('0073', 235322692)}
    marks = {name: fingerprints(script) for name, script in scripts().items()}
    assert (marks['archive/local']['0002'], marks['memory/local']['0002'], marks['optimization']['0004']) == (
        746290332,
        1939818781,
        468026487,
    )


def at_floor(path: Path) -> None:
    """Take the core chain of the database at ``path`` back to 0.4's head, with the fingerprint that 0.4 recorded."""
    command.downgrade(alembic(path), '0067')
    mark = fingerprints(ScriptDirectory(str(MIGRATIONS)))['0067']
    with closing(sqlite3.connect(path)) as con:
        con.execute("UPDATE migrations SET fingerprint = ? WHERE name = 'core'", (mark,))
        con.commit()


def test_a_migration_first_backs_up_the_database_and_keeps_the_newest_backups(tmp_path: Path) -> None:
    """Before it applies a pending revision, ``migrate`` copies the database next to it, named after the revision it
    is at, and removes the oldest copies beyond ``backups``. Nothing pending, a new database or ``backups=0``: none."""
    path = tmp_path / 'piloop.db'
    engine = create_engine(f'sqlite:///{path}')
    assert migrate(engine, cas=disk(tmp_path), backups=2, daemon=None) is None, 'a new database has nothing to lose'
    at_floor(path)
    with closing(sqlite3.connect(path)) as con:
        con.execute("INSERT INTO campaigns (name, path, config) VALUES ('kept', '/p', '{}')")
        con.commit()
    for old in ('0001-20250101T000000Z', '0002-20250102T000000Z'):
        (tmp_path / f'piloop.db.bak-{old}').write_bytes(b'an older backup')
    backup = migrate(engine, cas=disk(tmp_path), backups=2, daemon=None)
    assert backup is not None and re.fullmatch(r'piloop\.db\.bak-0067-\d{8}T\d{6}Z', backup.name), backup
    with closing(sqlite3.connect(f'file:{backup}?mode=ro', uri=True)) as con:
        assert con.execute('SELECT version_num FROM alembic_version').fetchall() == [('0067',)]
        assert con.execute('SELECT name FROM campaigns').fetchall() == [('kept',)]
        assert con.execute('PRAGMA integrity_check').fetchone() == ('ok',)
    assert sorted(p.name for p in tmp_path.glob('piloop.db.bak-*')) == [
        'piloop.db.bak-0002-20250102T000000Z',
        backup.name,
    ]
    assert migrate(engine, cas=disk(tmp_path), backups=2, daemon=None) is None, 'at head: nothing to back up'
    at_floor(path)
    assert (
        migrate(engine, cas=disk(tmp_path), backups=0, daemon=None) is None
        and len(list(tmp_path.glob('piloop.db.bak-*'))) == 2
    )
    assert revision(path) == ScriptDirectory.from_config(alembic(path)).get_current_head()
    engine.dispose()


def test_a_migration_prunes_only_the_backups_that_piloop_wrote_oldest_by_their_time(tmp_path: Path) -> None:
    """SQLite's ``-shm`` and ``-wal`` next to a copy that something opened, a copy that a person named and a name whose
    digits are no time stay. The oldest by the time in its name goes, not the first by name: here a copy of 0067 is older
    than the copy of 0017."""
    path = tmp_path / 'piloop.db'
    command.upgrade(alembic(path), '0067')
    engine = create_engine(f'sqlite:///{path}')
    ours = [
        'piloop.db.bak-0067-20250101T000000Z',
        'piloop.db.bak-0017-20250102T000000Z',
        'piloop.db.bak-0067-20250103T000000Z',
        'piloop.db.bak-0067-20250104T000000Z',
    ]
    others = [
        'piloop.db.bak-mine',
        'piloop.db.bak-0067-20251399T000000Z',
        'piloop.db.bak-0067-20250104T000000Z-shm',
        'piloop.db.bak-0067-20250104T000000Z-wal',
    ]
    for name in (*ours, *others):
        (tmp_path / name).write_bytes(b'a copy')
    backup = migrate(engine, cas=disk(tmp_path), backups=4, daemon=None)
    assert backup is not None
    assert sorted(p.name for p in tmp_path.glob('piloop.db.bak-*')) == sorted([*ours[1:], backup.name, *others])
    assert backups(path) == [tmp_path / name for name in ours[1:]] + [backup]
    engine.dispose()


def test_a_backup_that_fails_leaves_the_database_unmigrated_and_no_partial_copy(tmp_path: Path) -> None:
    """A full disk, as a limit on the size of the files this process writes: the copy fails, nothing is migrated."""
    path = tmp_path / 'piloop.db'
    command.upgrade(alembic(path), '0067')
    engine = create_engine(f'sqlite:///{path}')
    limit = resource.getrlimit(resource.RLIMIT_FSIZE)
    previous = signal.signal(signal.SIGXFSZ, signal.SIG_IGN)  # a write past the limit fails instead of killing
    resource.setrlimit(resource.RLIMIT_FSIZE, (64 * 1024, limit[1]))
    try:
        with pytest.raises(BackupFailed, match=r'could not back up .*, so it was not migrated: free some space'):
            migrate(engine, cas=disk(tmp_path), backups=3, daemon=None)
    finally:
        resource.setrlimit(resource.RLIMIT_FSIZE, limit)
        signal.signal(signal.SIGXFSZ, previous)
    assert revision(path) == '0067' and list(tmp_path.glob('piloop.db.bak-*')) == []
    engine.dispose()


def test_idea_links_stay_inside_the_campaign_never_point_at_the_idea_itself_and_go_with_either_idea(
    tmp_path: Path,
) -> None:
    """CHECKs refuse a self-link and an unknown kind, the trigger a link into another campaign (or to no idea)."""
    path = tmp_path / 'piloop.db'
    command.upgrade(alembic(path), 'head')
    link = 'INSERT INTO idea_links (idea_id, target_id, kind) VALUES '
    with closing(sqlite3.connect(path, isolation_level=None)) as con:
        con.execute('PRAGMA foreign_keys=ON')
        con.executescript(
            "INSERT INTO campaigns (name, path, config) VALUES ('p', '/p', '{}'), ('q', '/q', '{}');"
            'INSERT INTO ideas (campaign_id, title, hypothesis, created_by, track)'
            " VALUES (1, 'first', 'h', 't', 'a'), (2, 'other', 'h', 't', 'a'), (1, 'then', 'h', 't', 'a');"
        )
        refused = {
            "(1, 1, 'after')": 'CHECK constraint failed: idea_links_not_self',
            "(1, 3, 'blocks')": 'CHECK constraint failed: linkkind',
            "(1, 2, 'after')": 'idea_links: idea_id and target_id must be ideas of the same campaign',
            "(1, 99, 'supersedes')": 'idea_links: idea_id and target_id must be ideas of the same campaign',
        }
        for values, error in refused.items():
            with pytest.raises(sqlite3.IntegrityError, match=error):
                con.execute(link + values)
        con.execute(link + "(3, 1, 'after'), (3, 1, 'supersedes')")  # one pair may carry both kinds
        with pytest.raises(sqlite3.IntegrityError, match='UNIQUE constraint failed: idea_links.idea_id'):
            con.execute(link + "(3, 1, 'after')")
        con.execute('DELETE FROM ideas WHERE id = 1')
        assert con.execute('SELECT count(*) FROM idea_links').fetchone() == (0,), 'links go with the target'
        con.execute(
            "INSERT INTO ideas (campaign_id, title, hypothesis, created_by, track) VALUES (1, 'again', 'h', 't', 'a')"
        )
        con.execute(link + "(4, 3, 'after')")
        con.execute('DELETE FROM ideas WHERE id = 4')
        assert con.execute('SELECT count(*) FROM idea_links').fetchone() == (0,), 'and with the idea'


def test_idea_titles_are_unique_per_campaign(db: Database) -> None:
    """Concurrent research sweeps that land on the same title collide at insert; another campaign may reuse it."""
    with db.session() as s:
        s.add(Campaign(name='a', path='/a', config=bare_config('a')))
        s.add(Campaign(name='b', path='/b', config=bare_config('b')))
        s.commit()
        s.add(Idea(track='score', campaign_id=1, title='target encoding', hypothesis='x', created_by='research#1'))
        s.add(Idea(track='score', campaign_id=2, title='target encoding', hypothesis='x', created_by='research#2'))
        s.commit()
        s.add(Idea(track='score', campaign_id=1, title='target encoding', hypothesis='y', created_by='research#3'))
        with pytest.raises(IntegrityError, match='UNIQUE constraint failed: ideas.campaign_id, ideas.title'):
            s.commit()


def test_concurrent_migrations_of_a_fresh_database_run_one_at_a_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two daemons starting on a fresh home both call ``migrate``: unserialised, both read "no revision" and the second
    dies on ``CREATE TABLE comments`` (the ``piloop serve --detach`` race). A second ``migrate`` must wait until the
    first is out of Alembic, then find the database at head and do nothing.
    """
    path = tmp_path / 'piloop.db'
    real = command.upgrade
    inside = threading.Event()  # the first upgrade has started and is parked ...
    release = threading.Event()  # ... until the test lets it finish
    entered: list[str] = []

    def gated(cfg: AlembicConfig, rev: str) -> None:
        entered.append(threading.current_thread().name)
        if threading.current_thread().name == 'first':
            inside.set()
            assert release.wait(5)
        real(cfg, rev)

    monkeypatch.setattr(command, 'upgrade', gated)
    engines = [create_engine(f'sqlite:///{path}') for _ in range(2)]
    first = threading.Thread(
        target=migrate, args=(engines[0],), kwargs={'cas': disk(tmp_path), 'backups': 0, 'daemon': None}, name='first'
    )
    second = threading.Thread(
        target=migrate, args=(engines[1],), kwargs={'cas': disk(tmp_path), 'backups': 0, 'daemon': None}, name='second'
    )
    first.start()
    assert inside.wait(5)
    second.start()
    time.sleep(0.3)
    assert entered == ['first'], 'the second migrate entered Alembic while the first was still upgrading'
    release.set()
    first.join(5)
    second.join(5)
    assert set(entered) == {'first'}, 'the second found the database at head'
    assert stamps(path) == heads()
    for engine in engines:
        engine.dispose()


TAKES_0015 = 'project_id IN (SELECT "Project_Id" FROM evaluators) AND title NOT LIKE \'%projects%\' -- projects: kept'
"""A role's ``takes`` at revision 0015: identifiers to rename (one quoted, in another case), a literal and a comment."""
WHEN_0015 = "SELECT 1 FROM projects WHERE id = :project AND json_extract(config, '$.evaluator_id') IS NULL"


TAKES_0017 = 'id IN (SELECT run_id FROM "Workers" WHERE kind = \'worker\') -- workers: kept'
"""A role's ``takes`` at revision 0017: the table to rename (quoted, in another case), a literal and a comment."""


def at_0017(con: sqlite3.Connection) -> None:
    """Fill a database at revision 0017 with a row of every kind that names the worker role or the ``workers`` table."""
    roles = {
        'worker': {'takes': TAKES_0017},
        'research': {'triggers': [{'kind': 'sql', 'when': 'SELECT 1 FROM workers'}]},
    }
    con.execute("INSERT INTO campaigns (name, path, config) VALUES ('p', '/p', ?)", (json.dumps({'roles': roles}),))
    con.execute(
        "INSERT INTO ideas (campaign_id, title, hypothesis, track, created_by) VALUES (1, 't', 'h', 'main', 'worker#1')"
    )
    con.execute("INSERT INTO candidates (campaign_id, tree_sha, commit_sha) VALUES (1, 't', 'c')")
    con.execute(
        'INSERT INTO runs (idea_id, attempt, base_candidate_id, harness, model, role, updated_by) VALUES'
        " (1, 1, 1, 'claude', 'm', 'worker', 'worker#1'), (1, 2, 1, 'claude', 'm', 'critic', 'critic#2')"
    )
    con.execute(
        "INSERT INTO workers (kind, campaign_id, run_id, updated_by) VALUES ('worker', 1, 1, NULL), ('research', 1, NULL, NULL)"
    )
    con.execute(
        "INSERT INTO comments (target_kind, target_id, author, body) VALUES ('worker', 1, 'worker#1', 'a worker')"
    )
    con.execute(
        'INSERT INTO events (kind, actor, target_kind, target_id, payload) VALUES'
        " ('worker.wrap_up', 'daemon', 'worker', 1, '{}'),"
        " ('worker.started', 'daemon', 'worker', 1, '{\"reason\": \"worker\"}'),"
        " ('research.finished', 'daemon', 'worker', 2, '{}'),"
        " ('candidate.verdict', 'worker#1', 'candidate', 1, '{\"verdict_by\": \"worker#1\"}'),"
        " ('run.relaunched', 'daemon', 'run', 1, '{\"worker\": 1, \"launches\": 1}')"
    )


def test_an_upgrade_that_fails_halfway_leaves_the_database_as_it_was(tmp_path: Path) -> None:
    """The whole upgrade is one transaction: 0068 adds a column, then 0070 fails, and all of it goes back."""
    path = tmp_path / 'piloop.db'
    command.upgrade(alembic(path), '0067')
    before = path.read_bytes()
    copy = tmp_path / 'failing'
    shutil.copytree(MIGRATIONS, copy, ignore=lambda _dir, names: [n for n in names if n == '__pycache__'])
    (failing := next((copy / 'versions').glob('0070_*.py'))).write_text(
        "revision = '0070'\ndown_revision = '0069'\nbranch_labels = None\ndepends_on = None\n\n\n"
        "def upgrade() -> None:\n    raise RuntimeError('halfway')\n\n\ndef downgrade() -> None:\n    pass\n"
    )
    assert failing.name.startswith('0070_')
    cfg = alembic(path)
    cfg.set_main_option('script_location', str(copy))
    with pytest.raises(RuntimeError, match='halfway'):
        command.upgrade(cfg, 'head')
    with closing(sqlite3.connect(path, isolation_level=None)) as con:
        assert con.execute('SELECT version_num FROM alembic_version').fetchall() == [('0067',)]
        assert 'form' not in [c[1] for c in con.execute('PRAGMA table_info(comments)')], '0068 went back too'
    assert path.read_bytes() == before


HERDR_HARNESS = {
    'executable': 'claude',
    'headless': ['{prompt}'],
    'interactive': ['--session-id', '{session_id}'],
    'startup_dialogs': [['trust this folder', ['down', 'enter']]],
    'busy_screens': ['Retrying in'],
    'failed_turn_screens': None,
    'queue_key': {'key': 'tab', 'pending': 'tab to queue message'},
    'composer': '❯',
}
"""A stored harness table of 0.2, with every key that only the herdr runtime read."""


WRAPPER_0056 = ['sh', '-c', 'out=$("$@") && printf "METRIC spend=%s\\n" "$out"', 'sh']
"""The wrapper that revision 0056 stored for a ``[spend] command``."""


def spend_wrapper(fact: str) -> list[str]:
    """The wrapper of a ``[spend] command`` that reports its number as ``fact`` on the result line."""
    printed = f'out=$("$@") && printf \'{{"metrics": {{"{fact}": %s}}}}\\n\' "$out" >> "$PILOOP_RESULT_FILE"'
    return ['sh', '-c', printed, 'sh']


def test_0069_keeps_every_event_and_comment_points_them_at_target_kinds_and_downgrades(tmp_path: Path) -> None:
    """The kinds are rows; a kind that no piloop declared is refused; the downgrade drops what 0068 cannot hold."""
    path = tmp_path / 'piloop.db'
    cfg = alembic(path)
    command.upgrade(cfg, '0068')
    on_idea = (
        "INSERT INTO events (actor, target_kind, target_id, payload) VALUES ('human', 'idea', 4, '{\"kind\": \"x.y\"}')"
    )
    on_run = "INSERT INTO comments (target_kind, target_id, author, body) VALUES ('run', 2, 'human', 'hm')"
    with closing(sqlite3.connect(path, isolation_level=None)) as con:
        con.execute(on_idea)
        con.execute(on_run)
    command.upgrade(cfg, '0069')
    with closing(sqlite3.connect(path, isolation_level=None)) as con:
        con.execute('PRAGMA foreign_keys=ON')
        assert dict(con.execute('SELECT id, owner FROM target_kinds')) == {
            **dict.fromkeys(('campaign', 'agent_session', 'comment', 'instance', 'home'), 'core'),
            **dict.fromkeys(('idea', 'run', 'candidate', 'measurement'), 'optimization'),
        }
        assert con.execute('SELECT id, target_kind, target_id FROM events').fetchall() == [(1, 'idea', 4)]
        assert con.execute('SELECT id, target_kind, target_id FROM comments').fetchall() == [(1, 'run', 2)]
        with pytest.raises(sqlite3.IntegrityError, match='FOREIGN KEY'):
            con.execute(on_idea.replace("'idea'", "'notion'"))
        con.execute("INSERT INTO target_kinds (id, owner) VALUES ('notion', 'notes')")
        con.execute(on_idea.replace("'idea'", "'notion'"))
    command.downgrade(cfg, '0068')
    with closing(sqlite3.connect(path, isolation_level=None)) as con:
        assert con.execute('SELECT id, target_kind FROM events').fetchall() == [(1, 'idea')], 'notion is not of 0068'
        assert con.execute('SELECT id, target_kind FROM comments').fetchall() == [(1, 'run')]
        with pytest.raises(sqlite3.IntegrityError, match='targetkind'):
            con.execute(on_idea.replace("'idea'", "'notion'"))


def test_0070_turns_run_id_into_the_subject_rewrites_the_events_and_downgrades(tmp_path: Path) -> None:
    """Each run becomes a subject, the events name it with the new keys, and the downgrade gives back both."""
    path = tmp_path / 'piloop.db'
    cfg = alembic(path)
    command.upgrade(cfg, '0069')
    proposer = {'actor': 'research#1', 'harness': 'claude', 'executable': 'claude', 'session_id': 's'}
    identified = {'kind': 'agent_session.identified', 'session_id': 'x', 'harness': 'claude', 'executable': None}
    identified |= {'role': 'implementer', 'model': None, 'campaign': 'c'}
    old = [
        identified | {'run': 7, 'idea': 4, 'proposed_by': proposer},
        identified | {'role': 'research', 'run': None, 'idea': None, 'proposed_by': None},
        {'kind': 'anomaly.startup', 'role': 'implementer', 'harness': None, 'run': 7, 'seconds': 1.0, 'error': 'x'},
    ]
    with closing(sqlite3.connect(path, isolation_level=None)) as con:
        roles = '{"plugins": {"role": {"implementer": {}, "research": {}}}}'
        con.execute("INSERT INTO campaigns (name, path, config) VALUES ('c', '/c', ?)", (roles,))
        con.execute("INSERT INTO agent_sessions (kind, campaign_id, run_id) VALUES ('implementer', 1, 7)")
        con.execute("INSERT INTO agent_sessions (kind, campaign_id) VALUES ('research', 1)")
        for payload in old:
            con.execute(
                "INSERT INTO events (actor, target_kind, target_id, payload) VALUES ('daemon', 'agent_session', 1, ?)",
                (dumps(payload),),
            )
    command.upgrade(cfg, '0070')
    payloads = 'SELECT payload FROM events ORDER BY id'
    with closing(sqlite3.connect(path, isolation_level=None)) as con:
        assert con.execute('SELECT id, subject_kind, subject_id FROM agent_sessions ORDER BY id').fetchall() == [
            (1, 'run', 7),
            (2, None, None),
        ]
        assert 'run_id' not in [c for _, c, *_ in con.execute('PRAGMA table_info(agent_sessions)')]
        assert [loads(p) for (p,) in con.execute(payloads)] == [
            identified | {'subject': 'run#7', 'groups': ['idea#4'], 'parent': proposer},
            identified | {'role': 'research', 'subject': None, 'groups': [], 'parent': None},
            {
                'kind': 'anomaly.startup',
                'role': 'implementer',
                'harness': None,
                'seconds': 1.0,
                'error': 'x',
                'subject': 'run#7',
            },
        ]
        with pytest.raises(sqlite3.IntegrityError, match='agent_sessions_subject'):
            con.execute("INSERT INTO agent_sessions (kind, campaign_id, subject_kind) VALUES ('research', 1, 'run')")
        con.execute(
            "INSERT INTO agent_sessions (kind, campaign_id, subject_kind, subject_id) VALUES ('research', 1, 'idea', 3)"
        )
    command.downgrade(cfg, '0069')
    with closing(sqlite3.connect(path, isolation_level=None)) as con:
        assert con.execute('SELECT id, run_id FROM agent_sessions ORDER BY id').fetchall() == [
            (1, 7),
            (2, None),
            (3, None),
        ]
        assert [loads(p) for (p,) in con.execute(payloads)] == old


def test_0071_points_each_comment_at_comment_kinds_and_downgrades(tmp_path: Path) -> None:
    """The kinds are rows; a kind that no piloop declared is refused; the downgrade drops what 0070 cannot hold."""
    path = tmp_path / 'piloop.db'
    cfg = alembic(path)
    command.upgrade(cfg, '0070')
    comment = (
        "INSERT INTO comments (target_kind, target_id, author, kind, body) VALUES ('run', 2, 'human', 'finding', 'hm')"
    )
    with closing(sqlite3.connect(path, isolation_level=None)) as con:
        con.execute(comment)
    command.upgrade(cfg, '0071')
    with closing(sqlite3.connect(path, isolation_level=None)) as con:
        con.execute('PRAGMA foreign_keys=ON')
        assert {k for (k,) in con.execute('SELECT id FROM comment_kinds')} == {
            'note',
            'decision',
            'escalation',
            'message',
            'finding',
        }
        assert con.execute('SELECT id, kind FROM comments').fetchall() == [(1, 'finding')]
        with pytest.raises(sqlite3.IntegrityError, match='FOREIGN KEY'):
            con.execute(comment.replace("'finding'", "'praise'"))
        con.execute("INSERT INTO comment_kinds (id) VALUES ('praise')")
        con.execute(comment.replace("'finding'", "'praise'"))
        escalation = comment.replace("'finding', 'hm')", "'escalation', 'help')").replace('body)', 'body, priority)')
        con.execute(escalation.replace("'help')", "'help', 1)"))
        assert con.execute("SELECT kind FROM events WHERE target_kind = 'comment'").fetchall() == [
            ('comment.escalation',)
        ], "the rebuild keeps the table's persistent trigger"
    command.downgrade(cfg, '0070')
    with closing(sqlite3.connect(path, isolation_level=None)) as con:
        assert con.execute('SELECT id, kind FROM comments').fetchall() == [(1, 'finding'), (3, 'escalation')], (
            'praise is not of 0070'
        )
        with pytest.raises(sqlite3.IntegrityError, match='commentkind'):
            con.execute(comment.replace("'finding'", "'praise'"))


def test_0072_moves_a_campaign_s_finding_grace_to_its_implementer_drops_0_4_s_nulls_and_downgrades(
    tmp_path: Path,
) -> None:
    """A copy that 0.4 stored has every key of ``agents``, null where the file set none, which no longer validates."""
    path = tmp_path / 'piloop.db'
    cfg = alembic(path)
    command.upgrade(cfg, '0071')
    unset = {'agents': {'brief_words': None, 'finding_grace': None, 'wrap_up_grace': None}}
    grace = {'agents': {'finding_grace': 'PT2M', 'brief_words': None}, 'plugins': {'role': {'implementer': {}}}}
    with closing(sqlite3.connect(path, isolation_level=None)) as con:
        for name, config in (('p', unset), ('q', grace)):
            con.execute(
                'INSERT INTO campaigns (name, path, config) VALUES (?, ?, ?)', (name, f'/{name}', dumps(config))
            )
    stored = 'SELECT config FROM campaigns ORDER BY id'
    command.upgrade(cfg, '0072')
    with closing(sqlite3.connect(path, isolation_level=None)) as con:
        assert [loads(c) for (c,) in con.execute(stored)] == [
            {'agents': {}},
            {'plugins': {'role': {'implementer': {'finding_grace': 'PT2M'}}}},
        ]
    command.downgrade(cfg, '0071')
    with closing(sqlite3.connect(path, isolation_level=None)) as con:
        assert [loads(c) for (c,) in con.execute(stored)] == [
            {'agents': {}},
            {'agents': {'finding_grace': 'PT2M'}},
        ], 'the nulls held nothing: 0.4 reads their keys as unset'


def test_0073_names_a_lease_s_job_by_kind_and_row_with_its_runner_moves_the_pool_limits_and_downgrades(
    tmp_path: Path,
) -> None:
    """An evaluation's lease keeps its row as ``job_id`` and gets its evaluator's runner, while its queue row is there.

    A lease of another kind than 0.4's can be queued, and it blocks the downgrade until it is gone.
    """
    path = tmp_path / 'piloop.db'
    cfg = alembic(path)
    command.upgrade(cfg, '0072')
    lease = "INSERT INTO leases (kind, agent_session_id, evaluation_id, job_cid) VALUES (?, ?, ?, 'cid')"
    with closing(sqlite3.connect(path, isolation_level=None)) as con:
        roles = '{"plugins": {"role": {"implementer": {}}}}'
        con.execute("INSERT INTO campaigns (name, path, config) VALUES ('c', '/c', ?)", (roles,))
        con.execute("INSERT INTO candidates (campaign_id, tree_sha, revision) VALUES (1, 't', 'r')")
        con.execute("INSERT INTO evaluators (campaign_id, key, definition, runner) VALUES (1, 'k', '{}', 'gpu')")
        con.execute('INSERT INTO evaluations (candidate_id, evaluator_id) VALUES (1, 1)')
        con.execute("INSERT INTO agent_sessions (kind, campaign_id) VALUES ('implementer', 1)")
        con.execute(lease, ('agent', 1, None))
        con.execute(lease, ('evaluation', None, 1))
        con.execute(lease, ('evaluation', None, 99))  # measured: its queue row is gone
        for key in ('executor.pools.serve.evaluations', 'executor.pools.serve.agents'):
            con.execute("INSERT INTO limits (key, value, reason, actor) VALUES (?, '1', 'r', 'human')", (key,))
    command.upgrade(cfg, '0073')
    leases = 'SELECT id, kind, agent_session_id, job_id, runner FROM leases ORDER BY id'
    keys = 'SELECT key FROM limits ORDER BY key'
    with closing(sqlite3.connect(path, isolation_level=None)) as con:
        assert con.execute(leases).fetchall() == [
            (1, 'agent', 1, None, None),
            (2, 'evaluation', None, 1, 'gpu'),
            (3, 'evaluation', None, 99, None),
        ]
        assert con.execute(keys).fetchall() == [
            ('executor.pools.serve.agents',),
            ('executor.pools.serve.slots.evaluation',),
        ]
        with pytest.raises(sqlite3.IntegrityError, match='a lease has the job of its kind'):
            con.execute("INSERT INTO leases (kind, job_cid) VALUES ('probe', 'cid')")
        con.execute("INSERT INTO leases (kind, job_id, runner, job_cid) VALUES ('probe', 5, 'gpu', 'cid')")
    with pytest.raises(RuntimeError, match="a lease of the job kind 'probe'"):
        command.downgrade(cfg, '0072')
    with closing(sqlite3.connect(path, isolation_level=None)) as con:
        con.execute("DELETE FROM leases WHERE kind = 'probe'")
    command.downgrade(cfg, '0072')
    with closing(sqlite3.connect(path, isolation_level=None)) as con:
        assert con.execute('SELECT id, kind, agent_session_id, evaluation_id FROM leases ORDER BY id').fetchall() == [
            (1, 'agent', 1, None),
            (2, 'evaluation', None, 1),
            (3, 'evaluation', None, 99),
        ]
        assert con.execute(keys).fetchall() == [
            ('executor.pools.serve.agents',),
            ('executor.pools.serve.evaluations',),
        ]
        with pytest.raises(sqlite3.IntegrityError, match='leasekind'):
            con.execute("INSERT INTO leases (kind, evaluation_id, job_cid) VALUES ('probe', 5, 'cid')")


def test_0074_drops_the_keys_of_the_removed_features_from_each_stored_config(tmp_path: Path) -> None:
    path = tmp_path / 'piloop.db'
    cfg = alembic(path)
    command.upgrade(cfg, '0073')
    roles = {
        'scout': {'interval': 60.0, 'seeds': 2},
        'research': {'max_ideas': 2, 'files': 'ideas', 'plan_words': 90},
        'watchdog': {'triggers': ['run_failed', 'leaking_funnel', 'leaks', 'cron']},
        'implementer': {'files': 'nothing', 'timeout': 60.0},
        'quick': {'runs': True, 'plan_words': 80},
    }
    triggers = {'leaks': {'provider': 'leaking_funnel', 'window': 5}, 'cron': {'max_interval': 60.0}}
    with closing(sqlite3.connect(path, isolation_level=None)) as con:
        for name, config in (('p', {'plugins': {'role': roles, 'trigger': triggers}}), ('q', {'name': 'q'})):
            con.execute(
                'INSERT INTO campaigns (name, path, config) VALUES (?, ?, ?)', (name, f'/{name}', dumps(config))
            )
    stored = 'SELECT config FROM campaigns ORDER BY id'
    command.upgrade(cfg, '0074')
    with closing(sqlite3.connect(path, isolation_level=None)) as con:
        assert [loads(c) for (c,) in con.execute(stored)] == [
            {
                'plugins': {
                    'role': {
                        'research': {'max_ideas': 2, 'files': 'ideas', 'plan_words': 90},
                        'watchdog': {'triggers': ['run_failed', 'cron']},
                        'implementer': {'timeout': 60.0},
                        'quick': {'runs': True},
                    },
                    'trigger': {'cron': {'max_interval': 60.0}},
                }
            },
            {'name': 'q'},
        ]
    command.downgrade(cfg, '0073')


def test_optimization_0005_maps_the_idea_states_onto_the_board_drops_supersedes_and_families_and_downgrades(
    tmp_path: Path,
) -> None:
    """Seven states become four, in the rows and in the events, and the links and the column that v0 drops go."""
    path = tmp_path / 'piloop.db'
    command.upgrade(alembic(path), 'head')
    chain = alembic_config(f'sqlite:///{path}', 'optimization', disk(tmp_path))
    command.upgrade(chain, '0004')
    old = ('seed', 'proposed', 'ready', 'scheduled', 'done', 'parked', 'rejected')
    with closing(sqlite3.connect(path, isolation_level=None)) as con:
        con.execute("INSERT INTO campaigns (name, path, config) VALUES ('c', '/c', '{}')")
        for i, status in enumerate(old, start=1):
            con.execute(
                'INSERT INTO ideas (campaign_id, title, hypothesis, track, status, created_by, family)'
                " VALUES (1, ?, 'h', 'main', ?, 'human', 'ensembling')",
                (f'idea {i}', status),
            )
        con.execute("INSERT INTO idea_links (idea_id, target_id, kind) VALUES (2, 1, 'after'), (3, 1, 'supersedes')")
        changes = [('seed', 'ready'), ('ready', 'scheduled'), ('scheduled', 'parked'), ('parked', 'rejected')]
        for was, now in changes:
            payload = {'kind': 'idea.status', 'old': was, 'new': now}
            con.execute(
                "INSERT INTO events (actor, target_kind, target_id, payload) VALUES ('daemon', 'idea', 1, ?)",
                (dumps(payload),),
            )
        deleted = {'kind': 'idea.deleted', 'title': 't', 'status': 'proposed', 'hypothesis': 'h'}
        con.execute(
            "INSERT INTO events (actor, target_kind, target_id, payload) VALUES ('human', 'idea', 8, ?)",
            (dumps(deleted),),
        )
        for kind, link_kind in (
            ('idea.linked', 'after'),
            ('idea.linked', 'supersedes'),
            ('idea.unlinked', 'supersedes'),
        ):
            linked = {'kind': kind, 'target': 1, 'link_kind': link_kind}
            con.execute(
                "INSERT INTO events (actor, target_kind, target_id, payload) VALUES ('human', 'idea', 3, ?)",
                (dumps(linked),),
            )
    command.upgrade(chain, '0005')
    statuses = 'SELECT status FROM ideas ORDER BY id'
    payloads = 'SELECT payload FROM events ORDER BY id'
    with closing(sqlite3.connect(path, isolation_level=None)) as con:
        assert [s for (s,) in con.execute(statuses)] == [
            'open',
            'open',
            'open',
            'claimed',
            'done',
            'dropped',
            'dropped',
        ]
        assert con.execute('SELECT idea_id, target_id, kind FROM idea_links').fetchall() == [(2, 1, 'after')]
        assert 'family' not in [c for _, c, *_ in con.execute('PRAGMA table_info(ideas)')]
        assert [loads(p) for (p,) in con.execute(payloads)] == [
            {'kind': 'idea.status', 'old': 'open', 'new': 'open'},
            {'kind': 'idea.status', 'old': 'open', 'new': 'claimed'},
            {'kind': 'idea.status', 'old': 'claimed', 'new': 'dropped'},
            {'kind': 'idea.status', 'old': 'dropped', 'new': 'dropped'},
            deleted | {'status': 'open'},
            {'kind': 'idea.linked', 'target': 1, 'link_kind': 'after'},
        ], 'an event of a supersedes link goes with the link: it could not load'

        with pytest.raises(sqlite3.IntegrityError, match='ideastatus'):
            con.execute("UPDATE ideas SET status = 'ready' WHERE id = 1")
        with pytest.raises(sqlite3.IntegrityError, match='linkkind'):
            con.execute("INSERT INTO idea_links (idea_id, target_id, kind) VALUES (3, 1, 'supersedes')")
        con.execute("INSERT INTO ideas_fts (ideas_fts) VALUES ('integrity-check')")
    command.downgrade(chain, '0004')
    with closing(sqlite3.connect(path, isolation_level=None)) as con:
        assert [s for (s,) in con.execute(statuses)] == [
            'ready',
            'ready',
            'ready',
            'scheduled',
            'done',
            'parked',
            'parked',
        ]
        assert [loads(p).get('new') for (p,) in con.execute(payloads)][:4] == ['ready', 'scheduled', 'parked', 'parked']
        assert con.execute('SELECT count(family) FROM ideas').fetchone() == (0,)


def test_0075_drops_the_keys_of_seeds_and_reviews_from_each_stored_config(tmp_path: Path) -> None:
    path = tmp_path / 'piloop.db'
    cfg = alembic(path)
    command.upgrade(cfg, '0074')
    roles = {
        'research': {'triggers': ['low_water', 'seed', 'stalled', 'fresh'], 'files': 'ideas'},
        'watchdog': {'files': 'seeds', 'triggers': ['cron']},
        'implementer': {'idle_alert': 'PT30M', 'timeout': 60.0},
    }
    triggers = {'seed': {}, 'fresh': {'provider': 'seed'}, 'cron': {'max_interval': 60.0}}
    sections = {'pack': {'seeds': 20, 'ideas': 60}}
    config = {'plugins': {'role': roles, 'trigger': triggers, 'section': sections}}
    with closing(sqlite3.connect(path, isolation_level=None)) as con:
        for name, stored in (('p', config), ('q', {'name': 'q'})):
            con.execute(
                'INSERT INTO campaigns (name, path, config) VALUES (?, ?, ?)', (name, f'/{name}', dumps(stored))
            )
    stored = 'SELECT config FROM campaigns ORDER BY id'
    command.upgrade(cfg, '0075')
    with closing(sqlite3.connect(path, isolation_level=None)) as con:
        assert [loads(c) for (c,) in con.execute(stored)] == [
            {
                'plugins': {
                    'role': {
                        'research': {'triggers': ['low_water', 'stalled'], 'files': 'ideas'},
                        'watchdog': {'files': 'nothing', 'triggers': ['cron']},
                        'implementer': {'timeout': 60.0},
                    },
                    'trigger': {'cron': {'max_interval': 60.0}},
                    'section': {'pack': {'ideas': 60}},
                }
            },
            {'name': 'q'},
        ]
    command.downgrade(cfg, '0074')


def test_optimization_0006_gives_each_run_its_campaign_lets_a_run_wait_for_its_claim_and_downgrades(
    tmp_path: Path,
) -> None:
    path = tmp_path / 'piloop.db'
    command.upgrade(alembic(path), 'head')
    chain = alembic_config(f'sqlite:///{path}', 'optimization', disk(tmp_path))
    command.upgrade(chain, '0005')
    queued = {'kind': 'run.queued', 'idea': 1, 'attempt': 1, 'harness': 'claude', 'track': 'main', 'pinned_base': 2}
    with closing(sqlite3.connect(path, isolation_level=None)) as con:
        con.execute("INSERT INTO campaigns (name, path, config) VALUES ('c', '/c', '{}')")
        con.execute(
            'INSERT INTO ideas (campaign_id, title, hypothesis, track, created_by, harness, base_candidate_id)'
            " VALUES (1, 'i', 'h', 'main', 'human', 'codex', 2)"
        )
        con.execute(
            "INSERT INTO runs (idea_id, attempt, base_candidate_id, harness, model) VALUES (1, 1, 1, 'claude', 'm')"
        )
        con.execute(
            "INSERT INTO events (actor, target_kind, target_id, payload) VALUES ('daemon', 'run', 1, ?)",
            (dumps(queued),),
        )
    command.upgrade(chain, '0006')
    with closing(sqlite3.connect(path, isolation_level=None)) as con:
        assert con.execute('SELECT campaign_id, idea_id, attempt FROM runs').fetchall() == [(1, 1, 1)]
        assert {'harness', 'base_candidate_id', 'max_runs'}.isdisjoint(
            c for _, c, *_ in con.execute('PRAGMA table_info(ideas)')
        )
        assert loads(con.execute('SELECT payload FROM events').fetchone()[0]) == {
            'kind': 'run.queued',
            'harness': 'claude',
        }
        con.execute("INSERT INTO runs (campaign_id, base_candidate_id, harness, model) VALUES (1, 1, 'codex', 'm')")
        with pytest.raises(sqlite3.IntegrityError, match='NOT NULL constraint failed: runs.campaign_id'):
            con.execute("INSERT INTO runs (base_candidate_id, harness, model) VALUES (1, 'codex', 'm')")
    with closing(sqlite3.connect(path, isolation_level=None)) as con:
        con.execute("INSERT INTO candidates (campaign_id, run_id, tree_sha, revision) VALUES (1, 2, 't', 'r')")
    with pytest.raises(RuntimeError, match='cannot downgrade: run#2 claimed no idea and made candidates'):
        command.downgrade(chain, '0005')
    with closing(sqlite3.connect(path, isolation_level=None)) as con:
        con.execute('DELETE FROM candidates WHERE run_id = 2')
    command.downgrade(chain, '0005')
    with closing(sqlite3.connect(path, isolation_level=None)) as con:
        assert con.execute('SELECT id, idea_id FROM runs').fetchall() == [(1, 1)], 'the run that claimed nothing goes'
        assert con.execute('SELECT harness, base_candidate_id, max_runs FROM ideas').fetchall() == [(None, None, 2)]
        assert loads(con.execute('SELECT payload FROM events').fetchone()[0]) == {
            'kind': 'run.queued',
            'harness': 'claude',
            'idea': 1,
            'attempt': 1,
            'track': 'main',
        }


def test_0076_drops_the_keys_that_assigned_ideas_from_each_stored_config(tmp_path: Path) -> None:
    path = tmp_path / 'piloop.db'
    cfg = alembic(path)
    command.upgrade(cfg, '0075')
    roles = {
        'implementer': {'takes': 'priority < 3', 'explore_every': 4, 'recovery_requeues': 2, 'timeout': 60.0},
        'research': {'max_ideas': 2},
    }
    with closing(sqlite3.connect(path, isolation_level=None)) as con:
        tracks = {'score': {'share': 0.5}, 'size': {'share': 0.5, 'addendum': 'size.md'}}
        for name, stored in (('p', {'plugins': {'role': roles}, 'tracks': tracks}), ('q', {'name': 'q'})):
            con.execute(
                'INSERT INTO campaigns (name, path, config) VALUES (?, ?, ?)', (name, f'/{name}', dumps(stored))
            )
    command.upgrade(cfg, '0076')
    with closing(sqlite3.connect(path, isolation_level=None)) as con:
        assert [loads(c) for (c,) in con.execute('SELECT config FROM campaigns ORDER BY id')] == [
            {
                'plugins': {'role': {'implementer': {'timeout': 60.0}, 'research': {'max_ideas': 2}}},
                'tracks': {'size': {'addendum': 'size.md'}},
            },
            {'name': 'q'},
        ]
    command.downgrade(cfg, '0075')
