"""``measurements.units``: a measurement's per-unit results move from ``<home>/units/<id>.json`` into the CAS.

The column names the blob, and ``piloop sql`` hides it, as the file kept the results from agents. In the CAS, a home
restored on another host keeps them. The upgrade stores each file beside the database's home in the CAS and names it,
and leaves the files: nothing reads them any more. The downgrade writes the files back, and drops the column.

Revision ID: 0015
Revises: 0014
"""

import json
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import op
from covalence import Cid

from piloop.db.session import migration_cas

revision: str = '0015'
down_revision: str | None = '0014'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UNITS = 'units'
"""The directory of the files, under the home: the directory of the database file."""


def _dir() -> Path | None:
    database = op.get_bind().engine.url.database
    return None if not database or database == ':memory:' else Path(database).parent / UNITS


def upgrade() -> None:
    op.execute('ALTER TABLE measurements ADD COLUMN units VARCHAR')
    units = _dir()
    if units is None or not units.is_dir():
        return
    cas, bind = migration_cas(), op.get_bind()
    for path in sorted(units.glob('*.json')):
        if path.stem.isdigit():
            blob = cas.put(json.dumps(json.loads(path.read_bytes())).encode())
            bind.execute(
                sa.text('UPDATE measurements SET units = :b WHERE id = :id'), {'b': str(blob), 'id': int(path.stem)}
            )


def downgrade() -> None:
    units, cas, bind = _dir(), migration_cas(), op.get_bind()
    if units is not None:
        for measurement, blob in bind.execute(sa.text('SELECT id, units FROM measurements WHERE units IS NOT NULL')):
            if (data := cas.get(Cid(blob))) is None:
                raise LookupError(f'measurement#{measurement}: its per-unit results {blob} are not in the CAS')
            units.mkdir(parents=True, exist_ok=True)
            (units / f'{measurement}.json').write_bytes(data)
    op.execute('ALTER TABLE measurements DROP COLUMN units')  # in place: a batch rebuild would lose AUTOINCREMENT
