"""scripts/merge.py: the migrations of the tree that a merge would make declare each revision once, with one head."""

import subprocess
from pathlib import Path

import pytest

from scripts import merge
from scripts.merge import Held, check_commits, check_migrations, check_remote, check_stack, replay

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


def test_a_stack_on_its_base_s_tip_whose_lower_heads_are_in_the_top_s_head_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    subprocess.run(['git', 'init', '-q'], check=True)
    for config in (['user.name', 'ci'], ['user.email', 'ci@localhost']):
        subprocess.run(['git', 'config', *config], check=True)
    base = commit('chore: base')
    lower = commit('feat: lower')
    top = commit('feat: top')
    check_stack(base, [(1, lower), (2, top)])
    subprocess.run(['git', 'reset', '-q', '--hard', base], check=True)
    pushed_again = commit('feat: lower, pushed again after the top was built')
    with pytest.raises(Held, match=rf'#1 at {pushed_again[:8]} is not in the head of #2: rebase the stack on it'):
        check_stack(base, [(1, pushed_again), (2, top)])
    with pytest.raises(Held, match=rf'#1 at {lower[:8]} is not on the tip of its base: rebase the stack on it'):
        check_stack(pushed_again, [(1, lower), (2, top)])


def test_each_commit_that_lands_has_a_conventional_subject_and_no_listed_word() -> None:
    check_commits(['feat(watch): a filter\n\nbody\n', 'fix!: x\n'], ['someone'])
    with pytest.raises(Held, match="the commit subject 'wip' is not a Conventional Commit"):
        check_commits(['feat: x\n', 'wip\n'], [])
    with pytest.raises(Held, match='has a word of names.txt'):
        check_commits(['fix: as Someone asked\n'], ['someone'])


def test_the_commits_above_a_rebase_merged_one_are_replayed_on_its_copy_with_their_trees_authors_and_messages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    subprocess.run(['git', 'init', '-q'], check=True)
    for config in (['user.name', 'ci'], ['user.email', 'ci@localhost']):
        subprocess.run(['git', 'config', *config], check=True)
    base = commit('chore: base')
    (tmp_path / 'a.txt').write_text('a\n')
    subprocess.run(['git', 'add', 'a.txt'], check=True)
    lower = commit('feat: lower')
    (tmp_path / 'a.txt').write_text('a\nb\n')
    subprocess.run(['git', 'add', 'a.txt'], check=True)
    subprocess.run(
        ['git', 'commit', '-q', '--author', 'Someone <someone@localhost>', '-m', 'feat: upper\n\nbody'], check=True
    )
    upper = subprocess.run(['git', 'rev-parse', 'HEAD'], check=True, capture_output=True, text=True).stdout.strip()

    def git(*args: str) -> str:
        return subprocess.run(['git', *args], check=True, capture_output=True, text=True).stdout.strip()

    merged = git('commit-tree', f'{lower}^{{tree}}', '-p', base, '-m', 'feat: lower')
    replayed = replay(lower, upper, merged)
    assert replayed != upper
    assert git('rev-parse', f'{replayed}^{{tree}}') == git('rev-parse', f'{upper}^{{tree}}')
    assert git('rev-parse', f'{replayed}^') == merged
    assert git('log', '-1', '--format=%an <%ae>%n%B', replayed) == git('log', '-1', '--format=%an <%ae>%n%B', upper)
    with pytest.raises(Held, match='the base moved under the train'):
        replay(lower, upper, base)


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    """The gh commands that the script runs, recorded instead of run."""
    recorded: list[tuple[str, ...]] = []

    def gh(*args: str) -> str:
        recorded.append(args)
        return ''

    monkeypatch.setattr(merge, 'gh', gh)
    return recorded


LABEL = [
    (
        'label',
        'create',
        'train',
        '--force',
        '--description',
        "landed by the merge script's train: CI tests only its top",
    )
]
ON = [('pr', 'edit', '1', '--add-label', 'train'), ('pr', 'edit', '2', '--add-label', 'train')]
OFF = [('pr', 'edit', '1', '--remove-label', 'train'), ('pr', 'edit', '2', '--remove-label', 'train')]


def test_a_train_that_lands_takes_its_label_off_again(calls: list[tuple[str, ...]]) -> None:
    with merge.labelled([1, 2]):
        assert calls == LABEL + ON
    assert calls == LABEL + ON + OFF


def test_a_train_that_stops_partway_takes_its_label_off_the_pull_requests_that_it_did_not_merge(
    calls: list[tuple[str, ...]],
) -> None:
    with pytest.raises(Held, match='#2 merge refused'), merge.labelled([1, 2]):
        raise Held('#2 merge refused: not mergeable')
    assert calls == LABEL + ON + OFF


def test_a_batch_marks_a_draft_ready_before_it_merges_it(
    calls: list[tuple[str, ...]], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pr = merge.PullRequest(7, 'c0ffee', 'feat/x', 'main', draft=True)
    merger = merge.Merger('dev', 'main', tmp_path, None)
    (tmp_path / 'titles.txt').write_text('7|feat: x\n')
    (tmp_path / '7.md').write_text('Body.\n')
    for name, value in [('pull', lambda number: pr), ('watch', lambda pr: {}), ('main', lambda: 'main')]:
        monkeypatch.setattr(merge.Merger, name, staticmethod(value))
    monkeypatch.setattr(merge.Merger, 'merged_tree', lambda self, base, head: 'tree')
    monkeypatch.setattr(merge, 'check_migrations', lambda tree: None)
    ran: list[list[str]] = []
    monkeypatch.setattr(
        merge.subprocess, 'run', lambda args, **_: ran.append(args) or subprocess.CompletedProcess(args, 0)
    )
    merger.merge(7)
    assert calls[0] == ('pr', 'ready', '7')
    assert ran[0][:4] == ['gh', 'pr', 'merge', '7']
