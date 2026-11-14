"""The stored configs of real homes migrate through the current chain and validate.

``tests/fixtures/configs/<revision>-<n>.json`` is a ``campaigns.config`` as a real home stored it at that revision,
structure only: its keys, tables and value types, with every path, name, command and text replaced. Together, the
files of a revision hold every key and value type that those homes stored at it. Only a revision that this piloop
migrates from has files: the head of the previous minor release, or a newer one. All are at 0088 (0.7.0), as 0.7's
revisions left them. Files 1 to 24 are the bundled templates' configs as the 0.6 pins before ``optimization 0009``
stored them, and files 25 to 28 as 0.5.0 stored them, one for each structure. Files 29 to 34 are those that real homes
stored at 0053, as 0.4's to 0.7's revisions rewrote them.
"""

import sqlite3
from contextlib import closing
from itertools import groupby
from pathlib import Path

import pytest
from alembic import command
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import create_engine

from piloop.config import CampaignConfig
from piloop.db.session import migrate
from tests.conftest import disk
from tests.db.test_migrations import alembic

CONFIGS = Path(__file__).parents[1] / 'fixtures' / 'configs'
STORED = TypeAdapter(CampaignConfig)
BY_REVISION = {
    revision: [path.stem for path in paths]
    for revision, paths in groupby(sorted(CONFIGS.glob('*.json')), key=lambda path: path.stem.partition('-')[0])
}


@pytest.mark.parametrize('revision', sorted(BY_REVISION))
def test_each_stored_config_of_a_real_home_migrates_and_validates_with_no_key_left_for_a_bridge(
    tmp_path: Path, revision: str
) -> None:
    path = tmp_path / 'piloop.db'
    command.upgrade(alembic(path), revision)
    names = BY_REVISION[revision]
    with closing(sqlite3.connect(path)) as con:
        for name in names:
            config = (CONFIGS / f'{name}.json').read_text()
            con.execute('INSERT INTO campaigns (name, path, config) VALUES (?, ?, ?)', (name, f'/{name}', config))
        con.commit()
    engine = create_engine(f'sqlite:///{path}')
    migrate(engine, cas=disk(tmp_path), backups=0, daemon=None)
    engine.dispose()
    failed, loaded = {}, list[CampaignConfig]()
    with closing(sqlite3.connect(path)) as con:
        for name, config in con.execute('SELECT name, config FROM campaigns ORDER BY id'):
            try:
                loaded.append(STORED.validate_json(config))
            except ValidationError as e:
                failed[name] = str(e)
    assert failed == {}, 'an upgrade breaks a config that a real home stored'
    for cfg in loaded:
        assert 'orchestrator' not in cfg.roles.all(), 'the removed role goes'
