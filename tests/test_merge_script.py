"""scripts/merge.py: the migrations of the tree that a merge would make declare each revision once, with one head."""

import subprocess
from pathlib import Path

import pytest

from scripts.merge import Held, check_commits, check_migrations, check_remote, check_stack

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


@pytest.mark.parametrize(
    'url',
    ['git@github.com:owner/dev.git', 'https://github.com/owner/dev', 'ssh://git@github.com/Owner/dev.git'],
)
def test_a_remote_of_the_repository_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    monkeypatch.chdir(tmp_path)
    subprocess.run(['git', 'init', '-q'], check=True)
    subprocess.run(['git', 'remote', 'add', 'dev', url], check=True)
    check_remote('dev', 'owner/dev')


def test_a_remote_of_another_repository_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    subprocess.run(['git', 'init', '-q'], check=True)
    subprocess.run(['git', 'remote', 'add', 'origin', 'git@github.com:owner/public.git'], check=True)
    with pytest.raises(
        Held, match='the remote origin is git@github.com:owner/public.git, not the repository owner/dev'
    ):
        check_remote('origin', 'owner/dev')


def commit(message: str) -> str:
    subprocess.run(['git', 'commit', '-q', '--allow-empty', '-m', message], check=True)
    return subprocess.run(['git', 'rev-parse', 'HEAD'], check=True, capture_output=True, text=True).stdout.strip()


def test_a_stack_whose_lower_heads_are_in_the_top_s_head_passes_and_one_left_behind_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    subprocess.run(['git', 'init', '-q'], check=True)
    for config in (['user.name', 'ci'], ['user.email', 'ci@localhost']):
        subprocess.run(['git', 'config', *config], check=True)
    base = commit('chore: base')
    lower = commit('feat: lower')
    top = commit('feat: top')
    check_stack([(1, lower), (2, top)])
    subprocess.run(['git', 'reset', '-q', '--hard', base], check=True)
    pushed_again = commit('feat: lower, pushed again after the top was built')
    with pytest.raises(Held, match=rf'#1 at {pushed_again[:8]} is not in the head of #2: rebase the stack on it'):
        check_stack([(1, pushed_again), (2, top)])


def test_each_commit_that_lands_has_a_conventional_subject_and_no_listed_word() -> None:
    check_commits(['feat(watch): a filter\n\nbody\n', 'fix!: x\n'], ['someone'])
    with pytest.raises(Held, match="the commit subject 'wip' is not a Conventional Commit"):
        check_commits(['feat: x\n', 'wip\n'], [])
    with pytest.raises(Held, match='has a word of names.txt'):
        check_commits(['fix: as Someone asked\n'], ['someone'])
