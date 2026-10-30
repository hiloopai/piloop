"""A store that piloop 0.2.0 wrote is read in place: the same BLAKE3 digests, the same ``bytes.zst`` layout and markers.

``tests/fixtures/store_0_2_0`` was written by piloop 0.2.0 from PyPI: ``local`` holds ``tree`` (``put_tree``) and its
JSON manifest (``put_manifest``); ``staging`` staged one blob to the directory remote ``remote`` and kept its marker.
``written.json`` has the addresses that 0.2.0 returned.
"""

import json
import os
import shutil
from datetime import timedelta
from pathlib import Path

import pytest

from piloop.plugins.cas.cached import REMOTE, CachedCas
from piloop.plugins.cas.contract import Integrity, integrity
from piloop.plugins.cas.disk import BoundElsewhere, Disk, DiskCas
from piloop.plugins.cas.kit.objects import key, named
from piloop.store.tree import cid_of

FIXTURE = Path(__file__).parents[1] / 'fixtures' / 'store_0_2_0'
WRITTEN = json.loads((FIXTURE / 'written.json').read_text())
MANIFEST = named(WRITTEN['manifest'])
STAGED = named(WRITTEN['staged'])
URL = 'file:///remote'
"""The remote URL that 0.2.0 bound ``staging`` to."""


def tree(root: Path) -> dict[str, bytes | str]:
    """Each file's bytes and each symlink's target, by relative path."""
    found: dict[str, bytes | str] = {}
    for path in sorted(root.rglob('*')):
        rel = str(path.relative_to(root))
        if path.is_symlink():
            found[rel] = os.readlink(path)
        elif path.is_file():
            found[rel] = path.read_bytes()
    return found


def copy(name: str, to: Path) -> Path:
    shutil.copytree(FIXTURE / name, to / name, symlinks=True)
    return to / name


def staging(root: Path, remote: Path, url: str = URL) -> CachedCas:
    """The cache of a 0.2.0 staging home over the directory remote that it uploaded to: the objects as 0.2.0 put them."""
    return CachedCas(DiskCas(root), root, DiskCas(remote), url, timedelta(0))


def test_a_local_store_of_0_2_0_is_read_in_place(tmp_path: Path) -> None:
    root = copy('local', tmp_path)
    cas = Disk(path=root).open(tmp_path)
    before = {p: p.stat().st_mtime_ns for p in root.rglob('*')}
    files = [p for p in sorted((FIXTURE / 'tree').rglob('*')) if p.is_file() and not p.is_symlink()]
    blobs = {cid_of(path) for path in files}
    assert len(list((root / 'blake3').iterdir())) == 5
    assert all(integrity(cas, cid) is Integrity.OK for cid in blobs | {MANIFEST})
    for path in files:
        assert cas.get(cid_of(path)) == path.read_bytes() and cas.put_file(path) == cid_of(path), (
            'the same content, the same address'
        )
    assert {p: p.stat().st_mtime_ns for p in root.rglob('*') if p.name != 'tmp'} == before, 'nothing rewritten'


def test_a_staging_store_of_0_2_0_reads_through_its_remote_and_stays_bound_to_it(tmp_path: Path) -> None:
    root, remote = copy('staging', tmp_path), copy('remote', tmp_path)
    cas = staging(root, remote)
    assert cas.has(STAGED) and not (root / key(STAGED)).exists() and (root / 'blake3' / WRITTEN['staged'] / REMOTE)
    assert cas.get(STAGED) == b'staged on the remote and evicted here\n'
    assert cas.usage().remote == 1
    with pytest.raises(BoundElsewhere, match=f'mirrors to {URL}'):
        staging(root, remote, 'file:///elsewhere')
    with pytest.raises(BoundElsewhere, match=f'is a cache over {URL}'):
        Disk(path=root).open(tmp_path)
