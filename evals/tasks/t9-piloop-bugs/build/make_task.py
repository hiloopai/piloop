"""Build the t9-piloop-bugs task from a piloop checkout: the bugged tree, its sealed splits and the self-test fixes.

Usage:
    python3 -I make_task.py <piloop checkout> <hidden dir> <work dir> [--reference <rev>] [--upload <sealed uri>]

``<hidden dir>`` holds one directory per bug, in no repository, which counts once its ``verify.txt`` (the outcome of
its tests with and without the bug) is there: ``campaign/`` and ``holdout/`` (the held-out tests of the two sealed
splits), ``guard_exclude.txt`` (the pinned tests that a correct fix changes, one node id per line) and, for a closed
bug brought back, ``inject.patch``. Its ``symptom.md`` and its ``dev/`` repro are no part of the task: the brief names
only the subsystems. An open bug's fix is the commit of ``<rev>`` (default ``evals/t9-reference``, a branch on ``PIN``)
whose subject names the bug's number, as ``#693`` or ``b693``; a closed bug's fix undoes its ``inject.patch``. Writes,
under ``<work dir>``:

* ``private/``: piloop at ``PIN`` without ``DROPPED``, with every ``inject.patch`` applied: the starting commit's files
  beside the task's ``repo/``.
* ``sealed/``, ``sealed-campaign/``: the holdout and the campaign's split, as ``sealed.py`` reads them.
* ``data/``: the files of ``MODED``, as the task's ``data`` puts them in the image.
* ``fixes/<bug>.patch``: each bug's fix against ``private/``, with its tests, for ``selftest.py``, and the hidden
  dir's ``selftest-cases.json``: the self-test's cases that name the bugs' code or held-out tests.

With ``--upload``, it syncs these to ``<sealed uri>/t9-piloop-bugs/``, ``.campaign/``, ``.private/`` and ``.selftest/``
(the fixes and the hidden dir, so that the self-test runs again from sealed storage alone).
"""

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent
TASK = HERE.parent
NAME = TASK.name
PIN = 'c5100cbed2bc9235a3ac37eeb25f63fc485ede49'
"""piloop at the task's pin: public on hiloopai/piloop."""
DROPPED = ('evals', 'tests/evals')
"""What the agent's tree leaves out: the eval runner, which is no part of piloop, and its tests."""
GIT = ('git', '-c', 'user.name=evals', '-c', 'user.email=evals@localhost')
MODED = (
    'install.sh',
    'tests/fixtures/fake_bin.py',
    'tests/fixtures/store_0_2_0/tree/idea1/link',
    'tests/fixtures/store_0_2_0/tree/idea1/run.sh',
)
"""The files of the pin that sealed storage cannot hold, an executable or a symlink: the task's ``data`` takes them
from the pin into the image, and the private tree leaves them out."""


def sandbox_fails() -> tuple[str, ...]:
    """The pinned tests that fail in every sandbox at the start, which the task's ``evaluate.py`` lists."""
    spec = importlib.util.spec_from_file_location('evaluate', TASK / 'repo' / 'evaluate.py')
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.SANDBOX_FAILS


def run(*argv: str, cwd: Path | None = None, stdin: bytes | None = None) -> bytes:
    """The output of ``argv``, which writes no bytecode. Raises ``RuntimeError`` with its error output when it fails."""
    env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}
    done = subprocess.run(argv, cwd=cwd, input=stdin, capture_output=True, env=env)
    if done.returncode != 0:
        raise RuntimeError(f'{" ".join(argv)[:300]} exited {done.returncode}: {done.stderr.decode()[-2000:]}')
    return done.stdout


def fix_commits(checkout: Path, rev: str) -> dict[str, str]:
    """The commits of ``rev`` since ``PIN``, by the bug number in their subject."""
    log = run('git', '-C', str(checkout), 'log', '--format=%H %s', f'{PIN}..{rev}').decode()
    found = {}
    for line in log.splitlines():
        sha, subject = line.split(' ', 1)
        if m := re.search(r'(?:#|\bb)(\d+)\b', subject):
            found[m.group(1)] = sha
    return found


def ini(pyproject: Path) -> str:
    """The pytest configuration of ``pyproject`` as a ``pytest.ini``, for ``pytest -c``."""
    options = tomllib.loads(pyproject.read_text())['tool']['pytest']['ini_options']
    lines = ['[pytest]']
    for key, value in options.items():
        if key == 'addopts':
            lines.append(f'addopts = {subprocess.list2cmdline(value)}')
        elif isinstance(value, list):
            lines += [f'{key} =', *(f'    {v}' for v in value)]
        else:
            lines.append(f'{key} = {str(value).lower() if isinstance(value, bool) else value}')
    return '\n'.join(lines) + '\n'


def collected(private: Path, bug: str, tests: Path) -> list[str]:
    """The node ids of the tests in ``tests`` as ``tests/hidden/<bug>/`` of ``private``, relative to it."""
    place = private / 'tests' / 'hidden' / bug
    shutil.copytree(tests, place)
    try:
        argv = ('uv', 'run', '--frozen', 'pytest', '-p', 'no:cacheprovider', '--collect-only', '-q', str(place))
        out = run(*argv, cwd=private).decode()
    finally:
        shutil.rmtree(private / 'tests' / 'hidden')
    prefix = f'tests/hidden/{bug}/'
    ids = [line.removeprefix(prefix) for line in out.splitlines() if line.startswith(prefix)]
    names = [i.split('::', 1)[1] for i in ids]
    if not ids or len(set(names)) != len(names):
        raise ValueError(f'{tests}: no tests, or two tests of one name: {ids}')
    return ids


def split_dir(private: Path, bugs: list[Path], split: str, out: Path) -> None:
    """The sealed data of ``split``: the pinned tests and configuration, the held-out tests and ``meta.json``."""
    shutil.copytree(private / 'tests', out / 'pinned' / 'tests')
    (out / 'pytest.ini').write_text(ini(private / 'pyproject.toml'))
    tests: dict[str, list[str]] = {}
    exclude = list(sandbox_fails())
    for bug in bugs:
        exclude += [line for line in (bug / 'guard_exclude.txt').read_text().splitlines() if line.strip()]
        if not list((bug / split).glob('test_*.py')):
            raise ValueError(f'{bug.name} has no {split} tests')
        shutil.copytree(bug / split, out / 'hidden' / bug.name, ignore=shutil.ignore_patterns('__pycache__'))
        tests[bug.name] = collected(private, bug.name, out / 'hidden' / bug.name)
    # the holdout's headline leaves out the closed bugs brought back: their fixes are public upstream, and a piloop arm's
    # box holds an installed piloop that has them; the campaign's split counts them, as the agents' signal does
    reinjected = [b.name for b in bugs if (b / 'inject.patch').exists()] if split == 'holdout' else []
    meta = {
        'split': split,
        'bugs': tests,
        'reinjected': reinjected,
        # the campaign's split scores each candidate that beats the best: the suite's 15 minutes would hold each score
        'guard': split == 'holdout',
        'guard_exclude': exclude,
    }
    (out / 'meta.json').write_text(json.dumps(meta, indent=1) + '\n')


def unique_names(private: Path, hidden: Path) -> None:
    """Fail unless every test file has a name of its own: pytest imports a test module by its file name alone.

    Raises:
        ValueError: two test files have one name.
    """
    names = [p.name for p in (private / 'tests').rglob('test_*.py')]
    names += [p.name for split in ('campaign', 'holdout') for p in hidden.glob(f'*/{split}/test_*.py')]
    if repeated := sorted({n for n in names if names.count(n) > 1}):
        raise ValueError(f'test files with one name: {repeated}')


def build(checkout: Path, hidden: Path, work: Path, rev: str) -> None:
    """Write ``work``; see the module's docstring."""
    shutil.rmtree(work, ignore_errors=True)
    private, fixes = work / 'private', work / 'fixes'
    private.mkdir(parents=True)
    fixes.mkdir()
    run('tar', 'x', '-C', str(private), stdin=run('git', '-C', str(checkout), 'archive', '--format=tar', PIN))
    for path in DROPPED:
        shutil.rmtree(private / path)
    # a repo of its own while it is built, so that each brought-back bug's fix is the diff that undoes its patch
    run(*GIT, 'init', '-q', cwd=private)
    run(*GIT, 'add', '-A', cwd=private)
    run(*GIT, 'commit', '-qm', 'pin', cwd=private)
    bugs = sorted((p for p in hidden.iterdir() if (p / 'verify.txt').exists()), key=lambda p: p.name)
    for p in sorted(hidden.iterdir()):
        if p.is_dir() and p not in bugs:
            print(f'{p.name}: no verify.txt, left out', file=sys.stderr)
    commits = fix_commits(checkout, rev)
    for bug in bugs:
        if (bug / 'inject.patch').exists():
            run(*GIT, 'apply', str(bug / 'inject.patch'), cwd=private)
            run(*GIT, 'commit', '-qam', bug.name, cwd=private)
            fix = run(*GIT, 'diff', 'HEAD', 'HEAD~1', cwd=private)
        elif (number := bug.name.split('-')[0][1:]) in commits:
            fix = run(
                'git',
                '-C',
                str(checkout),
                'diff',
                f'{commits[number]}~1',
                commits[number],
                '--',
                '.',
                ':!evals',
            )
        else:
            print(f'{bug.name}: no fix on {rev}', file=sys.stderr)
            continue
        (fixes / f'{bug.name}.patch').write_bytes(fix)
    shutil.rmtree(private / '.git')
    moded = sorted(
        str(p.relative_to(private))
        for p in private.rglob('*')
        if p.is_symlink() or p.stat().st_mode & 0o111 and p.is_file()
    )
    data = ' '.join(tomllib.loads((TASK / 'task.toml').read_text())['data'])
    if moded != sorted(MODED) or not all(path in data for path in MODED):
        raise ValueError(f'the executables and symlinks of the tree, {moded}, are not those of MODED and the data')
    for path in MODED:
        (private / path).unlink()
    (work / 'data').mkdir()
    run('tar', 'x', '-C', str(work / 'data'), stdin=run('git', '-C', str(checkout), 'archive', PIN, *MODED))
    unique_names(private, hidden)
    run('uv', 'sync', '--frozen', '-q', cwd=private)
    try:
        for split, out in (('holdout', work / 'sealed'), ('campaign', work / 'sealed-campaign')):
            split_dir(private, bugs, split, out)
    finally:
        shutil.rmtree(private / '.venv')
    if (hidden / 'selftest-cases.json').exists():
        shutil.copyfile(hidden / 'selftest-cases.json', work / 'selftest-cases.json')


def upload(work: Path, hidden: Path, uri: str) -> None:
    """Sync the built task to sealed storage."""
    for local, prefix in (
        (work / 'sealed', NAME),
        (work / 'sealed-campaign', f'{NAME}.campaign'),
        (work / 'private', f'{NAME}.private'),
        (work / 'fixes', f'{NAME}.selftest/fixes'),
        (hidden, f'{NAME}.selftest/hidden'),
    ):
        run(
            'aws',
            's3',
            'sync',
            '--delete',
            '--only-show-errors',
            '--exclude',
            '*__pycache__*',
            str(local),
            f'{uri}/{prefix}/',
        )


def main() -> None:
    """Build, and upload with ``--upload``."""
    checkout, hidden, work = (Path(a).resolve() for a in sys.argv[1:4])
    rev = sys.argv[sys.argv.index('--reference') + 1] if '--reference' in sys.argv else 'evals/t9-reference'
    build(checkout, hidden, work, rev)
    if '--upload' in sys.argv:
        upload(work, hidden, sys.argv[sys.argv.index('--upload') + 1].rstrip('/'))


if __name__ == '__main__':
    main()
