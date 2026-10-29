"""Record what a database of this release holds of piloop's persistent triggers: ``uv run -m scripts.release_triggers``.

A migration creates a trigger as the code renders it when the migration runs, so a database keeps the SQL of the
release that created the trigger until a later migration creates it again. ``tests/db/test_migrations.py`` puts these
triggers into a database at this release's heads, upgrades it, and checks that it then has each trigger as the code
renders it. Run this in the release's pull request (CONTRIBUTING.md, Release): the test fails until the file is of the
version in ``pyproject.toml``.
"""

import sqlite3
import tempfile
import tomllib
from contextlib import closing
from pathlib import Path

from pydantic import BaseModel
from sqlalchemy import create_engine

from piloop.db.session import migrate, stamps
from piloop.plugins.cas.contract import CAS

ROOT = Path(__file__).parents[1]
PATH = ROOT / 'tests' / 'fixtures' / 'release_triggers.json'


class ReleaseTriggers(BaseModel, extra='forbid'):
    """The persistent triggers of a new database of ``version``, by name, and the head of each of its chains."""

    version: str
    heads: dict[str, str]
    triggers: dict[str, str]


def record() -> ReleaseTriggers:
    """Migrate a new database, and read its triggers and its heads."""
    with tempfile.TemporaryDirectory() as home:
        path = Path(home) / 'piloop.db'
        engine = create_engine(f'sqlite:///{path}')
        migrate(engine, cas=CAS.selected({}).open(Path(home)), backups=0, daemon=None)
        engine.dispose()
        with closing(sqlite3.connect(path)) as con:
            triggers = dict(con.execute("SELECT name, sql FROM sqlite_master WHERE type = 'trigger' ORDER BY name"))
        version = tomllib.loads((ROOT / 'pyproject.toml').read_text())['project']['version']
        return ReleaseTriggers(version=version, heads=dict(stamps(path)), triggers=triggers)


if __name__ == '__main__':
    PATH.write_text(record().model_dump_json(indent=2) + '\n')
