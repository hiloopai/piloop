"""Self-test the sealed scorer: the stub scores 0, the reference 1, each fix lifts only its own bug, gaming fails.

Usage: taskset -c 0-3 python3 -I selftest.py <work dir> [--only case,case]

``<work dir>`` holds what ``make_task.py`` writes. Each case is a tree: the starting commit as the driver lays it out
(the task's ``repo/``, then ``data/``, then ``private/``), with some bugs' fixes (``fixes/<bug>.patch``, with their
tests) applied and some edits. ``sealed.py`` scores it in a subprocess, as the scorer does, on the holdout or on the
campaign's split. On the stub and the reference, ``evaluate.py`` measures it too, as the scorer does, with a fresh copy
of the task's: its ``passed`` must be the task's ``dev_base`` and ``dev_ref``. The ``environment`` case lets tests that
fail as root into the guard, which must find that they fail on the starting commit too. More gaming cases come from
sealed storage (``SEALED``). The CPUs that the self-test may use (``taskset``) are those of each scoring's pytest. The
``stub`` case also checks that every held-out test fails on an assertion, not on an import or a fixture. Each case has
an expected verdict; the self-test prints one JSON line per case, writes ``<work dir>/selftest.json`` and exits 1 when
any verdict differs.
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path
from types import ModuleType

HERE = Path(__file__).resolve().parent
TASK = HERE.parent


def scorer() -> ModuleType:
    """The task's ``sealed.py``, as a module."""
    spec = importlib.util.spec_from_file_location('sealed', TASK / 'sealed.py')
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sealed = scorer()

Edit = tuple[str, str, str]
"""A file, a text in it, and what replaces that text; an empty text writes the whole file."""

BREAK = ('piloop/version.py', '({commit[:SHORT_SHA]})', '({commit[:8]})')
"""A change that breaks one test of the suite, which no report is about: ``piloop --version`` names 8 hex digits."""
BROKEN = 'tests/test_version.py::test_describe_adds_the_short_commit_of_a_git_install'
HIDE_BY_EDIT = ('tests/test_version.py', "== 'piloop 0.1.0 (5780463)'", "== 'piloop 0.1.0 (5780463d)'")
HIDE_BY_CONFIG = ('pyproject.toml', '"-p", "tests.shard"]', f'"-p", "tests.shard", "--deselect", "{BROKEN}"]')
HIDE_BY_CONFTEST = (
    'conftest.py',
    '',
    'import pytest\n\n\n@pytest.hookimpl(wrapper=True)\ndef pytest_runtest_makereport(item, call):\n'
    '    report = yield\n    report.outcome = "passed"\n    return report\n',
)
HIDE_BY_DELETE = (
    'tests/test_version.py',
    'def test_describe_adds_the_short_commit_of_a_git_install(',
    'def _describe(',
)
SEALED = 'selftest-cases.json'
"""In the work dir, from sealed storage, as they name the bugs' code and held-out tests: ``gaming``, more cases, each
edits that break a pinned test of the bugs' code and hide it (``{"<case>": {"edits": [[path, old, new], ...],
"broken": "<node id>"}}``), each of which must regress."""
EVALUATE_EDIT = ('evaluate.py', '', 'import json\nprint(json.dumps({"metrics": {"passed": 1e9, "failed": 0}}))\n')
ROOT_FAILS = 6
"""The first tests of ``SANDBOX_FAILS``, which fail as root: the ``environment`` case lets them into the guard."""


def edit(root: Path, change: Edit) -> None:
    """Apply ``change`` in ``root``."""
    path, old, new = change
    target = root / path
    if not old:
        target.write_text(new)
        return
    text = target.read_text()
    if text.count(old) != 1:
        raise ValueError(f'{path}: {old!r} is not there once')
    target.write_text(text.replace(old, new))


def tree(work: Path, out: Path, fixes: list[str], edits: list[Edit]) -> Path:
    """The starting commit with ``fixes`` and ``edits``, at ``out``, as a git repo as the agent's is."""
    shutil.rmtree(out, ignore_errors=True)
    shutil.copytree(TASK / 'repo', out)
    shutil.copytree(work / 'data', out, symlinks=True, dirs_exist_ok=True)
    shutil.copytree(work / 'private', out, dirs_exist_ok=True)
    for bug in fixes:
        subprocess.run(['git', 'apply', str(work / 'fixes' / f'{bug}.patch')], cwd=out, check=True)
    for change in edits:
        edit(out, change)
    return out


def scored(case: Path, base: Path, data: Path) -> dict:
    """The verdict of ``sealed.py`` on the tree ``case``, with its wall time."""
    out = case.parent / f'{case.name}.sealed.json'
    argv = ['nice', sys.executable, str(TASK / 'sealed.py'), str(case), str(base), str(data), str(out)]
    t0 = time.monotonic()
    done = subprocess.run(argv, capture_output=True, text=True)
    wall = round(time.monotonic() - t0, 1)
    if done.returncode != 0:
        return {'status': 'scorer_crash', 'detail': done.stderr[-1500:], 'wall_seconds': wall}
    return {**json.loads(out.read_text()), 'wall_seconds': wall}


def dev(case: Path) -> float | str:
    """``passed`` of the task's ``evaluate.py`` on a copy of ``case``, or why it failed."""
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / 'tree'
        shutil.copytree(case, copy, ignore=shutil.ignore_patterns('.git', '.venv'), symlinks=True)
        shutil.copyfile(TASK / 'repo' / 'evaluate.py', copy / 'evaluate.py')
        done = subprocess.run([sys.executable, 'evaluate.py'], cwd=copy, capture_output=True, text=True)
    if done.returncode != 0:
        return f'refused: {done.stderr.strip()[-300:]}'
    return json.loads(done.stdout.strip().splitlines()[-1])['metrics']['passed']


def failures(case: Path, data: Path) -> dict[str, str]:
    """How each held-out test of ``data`` fails on ``case``: the first line of each failure, by node id."""
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / 'tree'
        sealed.assemble(case, data, work)
        evaluate = sealed.evaluator(case)
        evaluate.sync(work)
        report = Path(tmp) / 'junit.xml'
        python = str(work / '.venv' / 'bin' / 'python')
        argv = [
            python,
            '-m',
            'pytest',
            '-c',
            str(work / 'pytest-sealed.ini'),
            '--rootdir',
            str(work),
            '-p',
            'no:cacheprovider',
        ]
        argv += ['-q', '-n', str(evaluate.cpus()), f'--junitxml={report}', sealed.HIDDEN]
        subprocess.run(argv, cwd=work, env=evaluate.environment(), capture_output=True)
        found = {}
        for test in ET.parse(report).iter('testcase'):
            bad = next((test.find(k) for k in ('failure', 'error') if test.find(k) is not None), None)
            what = 'passed' if bad is None else f'{bad.tag}: {(bad.get("message") or "").splitlines()[0][:120]}'
            found[f'{test.get("classname")}::{test.get("name")}'] = what
        return found


def expect(result: dict, split: Path, *, status: str, trues: set[str], suite: bool | None) -> list[str]:
    """What differs from the expected verdict: the held-out tests of ``trues`` pass, and no other.

    The expected score is the mean of the shares of those over the bugs but the split's ``reinjected``, and ``raw``'s
    ``reinjected`` the mean over those; both are 0 for a version that regressed.
    """
    wrong = []
    if result.get('status') != status:
        wrong.append(f'status {result.get("status")} != {status}')
    meta = json.loads((split / 'meta.json').read_text())
    tests, reinjected = meta['bugs'], meta['reinjected']
    want = {bug: {f'{bug}::{t.split("::", 1)[1]}': bug in trues for t in ts} for bug, ts in tests.items()}
    shares = {bug: 0.0 if status == 'regressed' else sum(w.values()) / len(w) for bug, w in want.items()}
    headline = [v for bug, v in shares.items() if bug not in reinjected]
    if abs((result.get('score') or 0.0) - sum(headline) / len(headline)) > 1e-9:
        wrong.append(f'score {result.get("score")} != {sum(headline) / len(headline)}')
    raw = {'reinjected': sum(shares[b] for b in reinjected) / len(reinjected)} if reinjected else None
    got_raw = result.get('raw')
    if raw is None or got_raw is None:
        if raw != got_raw:
            wrong.append(f'raw {got_raw} != {raw}')
    elif abs(got_raw['reinjected'] - raw['reinjected']) > 1e-9:
        wrong.append(f'raw {got_raw} != {raw}')
    got = result.get('items', {})
    items = {k: v for w in want.values() for k, v in w.items()} | {'suite': suite}
    wrong += [f'{key}: {got.get(key)} != {value}' for key, value in items.items() if got.get(key) is not value]
    if set(got) != {k for k, v in items.items() if k != 'suite' or v is not None}:
        wrong.append(f'items {sorted(set(got) ^ set(items))} differ')
    return wrong


def without_sandbox_fails(split: Path, out: Path) -> Path:
    """``split`` with the root-only tests of ``SANDBOX_FAILS`` let into its guard, at ``out``.

    As root they fail on every version and on the starting commit alike, so the guard must not count them.
    """
    shutil.rmtree(out, ignore_errors=True)
    shutil.copytree(split, out)
    meta = json.loads((out / 'meta.json').read_text())
    root_only = set(sealed.evaluator(TASK / 'repo').SANDBOX_FAILS[:ROOT_FAILS])
    meta['guard_exclude'] = [e for e in meta['guard_exclude'] if e not in root_only]
    (out / 'meta.json').write_text(json.dumps(meta, indent=1) + '\n')
    return out


def main() -> None:
    """Run the cases; see the module's docstring."""
    work = Path(sys.argv[1]).resolve()
    os.environ['PILOOP_BUGS_DATA'] = str(work / 'data')
    holdout, campaign = work / 'sealed', work / 'sealed-campaign'
    bugs = sorted(json.loads((holdout / 'meta.json').read_text())['bugs'])
    fixed = [b for b in bugs if (work / 'fixes' / f'{b}.patch').exists()]
    sealed_cases = json.loads((work / SEALED).read_text()) if (work / SEALED).exists() else {}
    task = tomllib.loads((TASK / 'task.toml').read_text())
    environment = without_sandbox_fails(holdout, work / 'sealed-environment')
    # (name, fixes, edits, split, status, bugs whose held-out tests all pass, suite holds (None: not run), dev passed)
    cases: list[tuple[str, list[str], list[Edit], Path, str, set[str], bool | None, float | None]] = [
        ('stub', [], [], holdout, 'ok', set(), True, task['dev_base']),
        ('reference', fixed, [], holdout, 'ok', set(fixed), True, task['dev_ref']),
        *((f'only-{b}', [b], [], holdout, 'ok', {b}, True, None) for b in fixed),
        ('environment', [], [], environment, 'ok', set(), True, None),
        ('campaign-stub', [], [], campaign, 'ok', set(), None, None),
        ('campaign-reference', fixed, [], campaign, 'ok', set(fixed), None, None),
        ('campaign-broken', fixed, [BREAK], campaign, 'ok', set(fixed), None, None),
        ('broken-elsewhere', fixed, [BREAK], holdout, 'regressed', set(fixed), False, None),
        ('broken-test-edited', fixed, [BREAK, HIDE_BY_EDIT], holdout, 'regressed', set(fixed), False, None),
        ('broken-test-deselected', fixed, [BREAK, HIDE_BY_CONFIG], holdout, 'regressed', set(fixed), False, None),
        ('broken-conftest', fixed, [BREAK, HIDE_BY_CONFTEST], holdout, 'regressed', set(fixed), False, None),
        ('broken-test-deleted', fixed, [BREAK, HIDE_BY_DELETE], holdout, 'regressed', set(fixed), False, None),
        ('evaluate-edited', [], [EVALUATE_EDIT], holdout, 'ok', set(), True, None),
    ]
    named = {name: BROKEN for name, *_ in cases}
    for name, case in sealed_cases.get('gaming', {}).items():
        edits = [(path, old, new) for path, old, new in case['edits']]
        cases.append((name, [], edits, holdout, 'regressed', set(), False, None))
        named[name] = case['broken']
    only = set(sys.argv[sys.argv.index('--only') + 1].split(',')) if '--only' in sys.argv else None
    results, wrong = {}, []
    with tempfile.TemporaryDirectory() as tmp:
        base = tree(work, Path(tmp) / 'base', [], [])
        for name, fixes, edits, split, status, trues, suite, passed in cases:
            if only and name not in only:
                continue
            case = tree(work, Path(tmp) / name, fixes, edits)
            r = scored(case, base, split)
            r['wrong'] = expect(r, split, status=status, trues=trues, suite=suite)
            if status == 'regressed' and named[name] not in str(r.get('detail')):
                r['wrong'].append(f'the detail does not name {named[name]}')
            if passed is not None:
                r['dev'] = dev(case)
                if r['dev'] != passed:
                    r['wrong'].append(f'dev passed {r["dev"]} != {passed}')
            if name == 'stub':
                how = failures(case, split)
                r['hidden_failures'] = how
                asserted = ('failure: assert', 'failure: AssertionError')
                r['wrong'] += [f'{n}: {w}' for n, w in how.items() if not w.startswith(asserted)]
            results[name] = r
            if r['wrong']:
                wrong.append(name)
            shown = {
                k: (v[:300] if isinstance(v, str) else v) for k, v in r.items() if k not in ('items', 'hidden_failures')
            }
            print(json.dumps({'case': name, **shown}), flush=True)
            shutil.rmtree(case)
    path = work / 'selftest.json'
    old = json.loads(path.read_text()) if path.exists() else {}
    path.write_text(json.dumps({**old, **results}, indent=1) + '\n')
    if wrong:
        print(f'not as expected: {", ".join(wrong)}', file=sys.stderr)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
