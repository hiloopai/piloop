"""scripts/merge.py: the migrations of the tree that a merge would make declare each revision once, with one head."""

import subprocess
from pathlib import Path

import pytest

from scripts.merge import Held, check_migrations

VERSIONS = 'piloop/db/migrations/versions'
PLUGIN = 'piloop/plugins/archive/local/migrations'


Files = dict[str, tuple[str, str | None]]


def check(root: Path, monkeypatch: pytest.MonkeyPatch, files: Files) -> None:
    """Commit no files, only a tree of them, and check it."""
    for path, (revision, down) in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        down_text = 'None' if down is None else repr(down)
        (root / path).write_text(f"revision: str = '{revision}'\ndown_revision: str | None = {down_text}\n")
    monkeypatch.chdir(root)
    subprocess.run(['git', 'init', '-q'], check=True)
    subprocess.run(['git', 'add', '.'], check=True)
    sha = subprocess.run(['git', 'write-tree'], check=True, capture_output=True, text=True).stdout.strip()
    check_migrations(sha)


def test_a_squash_that_renames_the_files_of_a_chain_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    check(
        tmp_path,
        monkeypatch,
        {
            f'{VERSIONS}/0001_initial.py': ('0001', None),
            f'{VERSIONS}/0002_squashed.py': ('0002', '0001'),
            f'{PLUGIN}/0001_adopt.py': ('0001', None),
        },
    )


def test_a_revision_id_declared_twice_holds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(Held, match=r'versions declares revision 0002 twice'):
        check(
            tmp_path,
            monkeypatch,
            {
                f'{VERSIONS}/0001_initial.py': ('0001', None),
                f'{VERSIONS}/0002_a.py': ('0002', '0001'),
                f'{VERSIONS}/0002_b.py': ('0002', '0001'),
            },
        )


def test_two_heads_hold(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(Held, match=r'has the heads 0002, 0003'):
        check(
            tmp_path,
            monkeypatch,
            {
                f'{VERSIONS}/0001_initial.py': ('0001', None),
                f'{VERSIONS}/0002_a.py': ('0002', '0001'),
                f'{VERSIONS}/0003_b.py': ('0003', '0001'),
            },
        )
