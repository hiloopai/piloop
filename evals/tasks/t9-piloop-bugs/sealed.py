"""The piloop-bugs task's sealed score: the share of each report's held-out tests that pass, with the suite as a guard.

Run as ``python sealed.py <tree> <base> <sealed> <out>``, in a sandbox without network. The version is scored in a copy
of ``<tree>`` whose ``tests/`` is replaced by the pinned one (``<sealed>/pinned/tests``: an edit to a test is dropped,
and a test file that the version adds is not run), with the split's held-out tests under ``tests/hidden``
(``<sealed>/hidden/<bug>/``), the pinned pytest configuration (``<sealed>/pytest.ini``), and no ``conftest.py`` outside
``tests/``. A guard test that fails on the version and on the starting commit alike fails for the sandbox, and does
not count. The pinned test fixtures that sealed storage cannot hold, an executable or a symlink, come from the task's
data in the image, ``$PILOOP_BUGS_DATA``. ``evaluate.py`` comes from ``<base>``, the starting commit: it installs the
version's locked dependencies from uv's cache and runs pytest. A test that fails runs once more, and counts as failed
only if it fails again.

With ``guard`` in ``meta.json`` (the holdout), the whole pinned suite runs too: the campaign's split, which scores
each candidate that beats the best, runs its held-out tests alone, as the suite takes about 15 minutes on 2 CPUs.

Writes one JSON object to ``<out>``, ``{"score", "status", "detail", "raw", "items"}``: ``score`` is the mean over the
bugs of ``meta.json`` but those of ``reinjected`` of the share of their held-out tests that pass, and ``raw`` is
``{"reinjected": ...}``, the same mean over the bugs of ``reinjected``, or None when it names none. With ``guard``,
both are 0, with status ``regressed``, when a test of the pinned suite fails, but for those of ``guard_exclude`` (the
tests that a correct fix changes, and those that fail in every sandbox). ``items`` says whether each held-out test
passed, as ``<bug>::<test>``, and, with ``guard``, whether the suite held, as ``suite``. A version whose dependencies do
not install, or on which pytest cannot run, scores nothing (status ``crash``).
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import ModuleType

HIDDEN = 'tests/hidden'
DATA = Path(os.environ.get('PILOOP_BUGS_DATA', '/opt/evals/data/t9-piloop-bugs'))
SHOWN = 5
"""The failing tests that the detail names."""


def evaluator(base: Path) -> ModuleType:
    """The starting commit's ``evaluate.py``, as a module."""
    spec = importlib.util.spec_from_file_location('evaluate', base / 'evaluate.py')
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def assemble(tree: Path, sealed: Path, work: Path) -> None:
    """Copy the version into ``work`` with the pinned tests, the held-out ones and the pinned configuration."""
    shutil.copytree(tree, work, ignore=shutil.ignore_patterns('.git', '.venv', '__pycache__'), symlinks=True)
    shutil.rmtree(work / 'tests', ignore_errors=True)
    shutil.copytree(sealed / 'pinned' / 'tests', work / 'tests')
    shutil.copytree(DATA / 'tests', work / 'tests', symlinks=True, dirs_exist_ok=True)
    for path in work.rglob('conftest.py'):
        if not path.is_relative_to(work / 'tests'):
            path.unlink()
    shutil.copytree(sealed / 'hidden', work / HIDDEN)
    shutil.copyfile(sealed / 'pytest.ini', work / 'pytest-sealed.ini')
    # a test of the suite reads git's attributes of the repo, which a tree without .git has none of
    subprocess.run(['git', 'init', '-q'], cwd=work, check=True)


def excluded(nodeid: str, exclude: list[str]) -> bool:
    """Whether ``nodeid`` is out of the guard: a held-out test, or one of ``exclude`` or under one."""
    return nodeid.startswith(f'{HIDDEN}/') or any(
        nodeid == e or nodeid.startswith((f'{e}::', f'{e}/', f'{e}[')) for e in exclude
    )


def failing_at_start(base: Path, sealed: Path, tests: list[str], evaluate: ModuleType, work: Path) -> set[str]:
    """Those of ``tests`` that fail on the starting commit too, in the same sandbox: its fault, not the version's.

    A sandbox can fail a test for a reason of its own (gVisor has no ``/proc/locks``), which ``guard_exclude`` cannot
    know of in advance. Only the tests that failed on the version run again, so this costs a few seconds. None when
    ``<base>`` is not a tree of the repo, as for the campaign's split, which has no guard.
    """
    if not (base / 'pyproject.toml').exists():
        return set()
    assemble(base, sealed, work)
    evaluate.sync(work)
    found = evaluate.outcomes(work, tests, config=work / 'pytest-sealed.ini')
    return {n for n in tests if found.get(n, 'failed') == 'failed'}


def main(tree: Path, base: Path, sealed: Path) -> dict[str, object]:
    """Score the version in ``tree`` on the split in ``sealed``."""
    meta = json.loads((sealed / 'meta.json').read_text())
    bugs: dict[str, list[str]] = meta['bugs']
    exclude: list[str] = meta['guard_exclude']
    evaluate = evaluator(base)
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / 'tree'
        assemble(tree, sealed, work)
        config = work / 'pytest-sealed.ini'
        try:
            evaluate.sync(work)
            deselect = [arg for e in exclude for arg in ('--deselect', e)]
            paths = ['tests'] if meta['guard'] else [HIDDEN]
            args = [*paths, '-n', str(evaluate.cpus()), *deselect]
            found = evaluate.outcomes(work, args, config=config)
            if failed := sorted(n for n, o in found.items() if o == 'failed'):
                again = evaluate.outcomes(work, failed, config=config)
                found |= {n: again.get(n, 'failed') for n in failed}
            suspects = [n for n, o in found.items() if o == 'failed' and meta['guard'] and not excluded(n, exclude)]
            at_start = failing_at_start(base, sealed, suspects, evaluate, Path(tmp) / 'base') if suspects else set()
        except RuntimeError as e:
            return {'score': None, 'status': 'crash', 'detail': str(e)[-2000:], 'raw': None, 'items': {}}
    items: dict[str, bool] = {}
    shares: dict[str, float] = {}
    for bug, tests in bugs.items():
        passed = [found.get(f'{HIDDEN}/{bug}/{t}') == 'passed' for t in tests]
        items |= {f'{bug}::{t.split("::", 1)[1]}': p for t, p in zip(tests, passed, strict=True)}
        shares[bug] = sum(passed) / len(tests)
    reinjected: list[str] = meta.get('reinjected', [])
    held, whole = sum(items.values()), sum(share == 1 for share in shares.values())
    detail = (
        f'{meta["split"]}: {whole} of {len(bugs)} reports fixed in full, {held} of {len(items)} held-out tests pass'
    )
    if meta['guard']:
        broken = sorted(n for n, o in found.items() if o == 'failed' and not excluded(n, exclude) and n not in at_start)
        detail += f'; suite: {len(broken)} of {sum(not excluded(n, exclude) for n in found)} tests fail'
        if at_start:
            detail += f', and {len(at_start)} that fail at the start too ({", ".join(sorted(at_start)[:SHOWN])})'
        items['suite'] = not broken
        if broken:
            named = ', '.join(broken[:SHOWN]) + (f' and {len(broken) - SHOWN} more' if len(broken) > SHOWN else '')
            detail += f' ({named})'
            shares = dict.fromkeys(shares, 0.0)
    headline = [v for b, v in shares.items() if b not in reinjected]
    raw = {'reinjected': sum(shares[b] for b in reinjected) / len(reinjected)} if reinjected else None
    status = 'regressed' if items.get('suite') is False else 'ok'
    return {'score': sum(headline) / len(headline), 'status': status, 'detail': detail, 'raw': raw, 'items': items}


if __name__ == '__main__':
    Path(sys.argv[4]).write_text(json.dumps(main(*(Path(a) for a in sys.argv[1:4]))))
