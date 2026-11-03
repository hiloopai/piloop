"""Self-test the sealed scorer: a complete solution scores 1, the stub 0, and gaming is refused.

Usage: python3 -I selftest.py <work dir> <packages dir> [--only case,case]

``<work dir>`` holds what ``make_task.py`` writes: ``repo/``, ``sealed/``, ``solutions/known-good`` and, for the
private split, ``private/``, ``solutions/private-known-good`` and ``selftest-edits.json`` (``PRIVATE_EDITS``). Each case is a Lean tree, the complete solution or
the stub with some edits, scored by ``sealed.py`` in a subprocess as the runner does, on the whole task (``all``) or
on the private split alone (``private``). A ``deep`` case scores with a copy of the evaluator whose source scan
matches nothing, so that the audit alone has to catch the edit. Each case has an expected verdict; the self-test
prints one JSON line per case, writes ``<work dir>/selftest.json`` and exits 1 when any verdict differs.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
TASK = HERE.parent
OC = Path('Silmaril/Courses/Cambridge/OptimisingCompilers')
S1Q1 = OC / 'Exercises/S1Q1.lean'
SIGNS = OC / 'AbstractInterpretation.lean'

NATIVE = (S1Q1, 'theorem lva_f :', 'decide +kernel', 'native_decide')
NATIVE_HELPER = (S1Q1, 'theorem dce_f :', 'decide +kernel', 'have _h : 2 + 2 = 4 := by native_decide\n  decide +kernel')
AXIOM = (S1Q1, '/-- (b) One DCE pass', None, 'axiom cheat : ∀ p : Prop, p\n\n/-- (b) One DCE pass')
AXIOM_USE = (S1Q1, 'theorem dce_f :', 'decide +kernel', 'exact cheat _')
STATEMENT = (
    SIGNS,
    'theorem mem_γ_ofInt (n : ℤ) : n ∈ γ (ofInt n) := by',
    None,
    'theorem mem_γ_ofInt (n : ℤ) : True := by\n  trivial\nexample (n : ℤ) : n ∈ γ (ofInt n) := by',
)
OF_INT = 'def ofInt (n : ℤ) : Sign := if n < 0 then .neg else if n = 0 then .zero else .pos'
DEFINITION = (
    SIGNS,
    OF_INT,
    None,
    'def ofInt (n : ℤ) : Sign := id (if n < 0 then .neg else if n = 0 then .zero else .pos)',
)
DEFINITION_BREAKING = (
    SIGNS,
    OF_INT,
    None,
    'def ofInt (n : ℤ) : Sign := if n ≤ 0 then .neg else if n = 0 then .zero else .pos',
)
SKIP_KERNEL = (S1Q1, 'theorem lva_f :', None, 'set_option debug.skipKernelTC true in\ntheorem lva_f :')
EVAL = (S1Q1, '/-- (b) One DCE pass', None, '#eval IO.FS.writeFile "/tmp/owned" "x"\n\n/-- (b) One DCE pass')
PRIVATE_EDITS = 'selftest-edits.json'
"""In the work dir, beside the private split: the edits of the private cases, as [path, anchor, inner, new] lists.
They quote private statements, so they are in sealed storage with the split, never in this repo."""


def mutate(root: Path, edit: tuple) -> None:
    """Replace, in one file, ``anchor`` (or the first ``inner`` after it) with ``new``."""
    path, anchor, inner, new = edit
    text = (root / path).read_text()
    at = text.index(anchor)
    if inner is None:
        text = text[:at] + new + text[at + len(anchor) :]
    else:
        j = text.index(inner, at)
        text = text[:j] + new + text[j + len(inner) :]
    (root / path).write_text(text)


def tree(work: Path, name: str, sources: list[Path], edits: list[tuple]) -> Path:
    """The Lean files of ``sources``, merged in order, with ``edits`` applied, as the case ``name``."""
    out = work / 'selftest' / name
    if out.exists():
        shutil.rmtree(out)
    for source in sources:
        shutil.copytree(source / 'Silmaril', out / 'Silmaril', dirs_exist_ok=True)
    for e in edits:
        mutate(out, e)
    return out


def sealed_dir(work: Path, split: str, deep: bool) -> Path:
    """The sealed data scoring ``split``; with ``deep``, with a source scan that matches nothing."""
    data = work / f'sealed-{split}{"-deep" if deep else ""}'
    shutil.rmtree(data, ignore_errors=True)
    shutil.copytree(work / 'sealed', data)
    meta = json.loads((data / 'meta.json').read_text())
    (data / 'meta.json').write_text(json.dumps({**meta, 'split': split}) + '\n')
    if deep:
        text = (data / 'evaluate.py').read_text()
        at = text.index('BANNED = re.compile(')
        end = text.index('\n)\n', at) + 3
        (data / 'evaluate.py').write_text(text[:at] + "BANNED = re.compile(r'(?!x)x')\n" + text[end:])
    return data


def sealed(case: Path, base: Path, data: Path, packages: Path) -> dict:
    """The verdict of ``sealed.py`` on the tree ``case``."""
    out = case / 'sealed.json'
    env = {**os.environ, 'OPTCOMP_PACKAGES': str(packages)}
    argv = ['nice', sys.executable, str(TASK / 'sealed.py'), str(case), str(base), str(data), str(out)]
    t0 = time.monotonic()
    done = subprocess.run(argv, env=env, capture_output=True, text=True)
    wall = round(time.monotonic() - t0, 1)
    if done.returncode != 0:
        return {'status': 'scorer_crash', 'detail': done.stderr[-1500:], 'wall_seconds': wall}
    return {**json.loads(out.read_text()), 'wall_seconds': wall}


def imports(path: Path) -> list[str]:
    """The modules a Lean file imports."""
    return re.findall(r'^\s*(?:public\s+)?(?:meta\s+)?import\s+(?:all\s+)?([\w.]+)', path.read_text(), re.M)


def outside(work: Path, case: Path, broken: list[Path]) -> int:
    """How many targets lie outside the modules ``broken`` and every module that imports one of them."""
    mods = {str(p.relative_to(case))[:-5].replace('/', '.'): p for p in (case / 'Silmaril').rglob('*.lean')}
    down = {str(b)[:-5].replace('/', '.') for b in broken}
    while more := {m for m, p in mods.items() if m not in down and down & set(imports(p))}:
        down |= more
    spec = json.loads((work / 'sealed' / 'targets.json').read_text())
    targets = spec['targets'] + json.loads((work / 'private' / 'targets.private.json').read_text())['targets']
    of = spec['modules_of'] | json.loads((work / 'private' / 'targets.private.json').read_text())['modules_of']
    return sum(1 for t in targets if of[t] not in down)


def main() -> None:
    """Run the cases; see the module's docstring."""
    work, packages = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
    meta_path = work / 'sealed' / 'meta.json'
    meta = json.loads(meta_path.read_text())
    if 'packages_digest' not in meta:
        sys.path.insert(0, str(TASK))
        from sealed import digest

        meta['packages_digest'] = digest(packages)
        meta_path.write_text(json.dumps(meta) + '\n')
    good = [work / 'solutions' / 'known-good', work / 'solutions' / 'private-known-good']
    stub = [work / 'repo', work / 'private']
    n_all = len(json.loads((work / 'sealed' / 'targets.json').read_text())['targets'])
    n_private = len(json.loads((work / 'private' / 'targets.private.json').read_text())['targets'])
    n_all += n_private
    edits = json.loads((work / PRIVATE_EDITS).read_text())
    private_statement = [(OC / path, anchor, inner, new) for path, anchor, inner, new in edits]
    # (name, trees, edits, split, deep, expected status, expected number proved or a refusal's reason)
    cases = [
        ('scan-native-decide', good, [NATIVE], 'all', False, 'refused', 'banned command'),
        ('scan-axiom', good, [AXIOM, AXIOM_USE], 'all', False, 'refused', 'banned command'),
        ('scan-skip-kernel', good, [SKIP_KERNEL], 'all', False, 'refused', 'banned command'),
        ('scan-eval-io', good, [EVAL], 'all', False, 'refused', 'banned command'),
        ('stub', stub, [], 'all', False, 'ok', 0),
        ('stub-private', stub, [], 'private', False, 'ok', 0),
        ('deep-native-decide', good, [NATIVE_HELPER], 'all', True, 'refused', 'native_decide.ax'),
        ('deep-gaming', good, [AXIOM, AXIOM_USE, STATEMENT, DEFINITION], 'all', True, 'refused', 'changes'),
        # the 2026-11-03 08:44 case: two edits do not compile, so their modules and every importer score nothing,
        # and the axiom and the weaker statement inside them never reach the audit
        (
            'deep-gaming-uncompiled',
            good,
            [NATIVE, AXIOM, AXIOM_USE, STATEMENT, DEFINITION_BREAKING],
            'all',
            True,
            'ok',
            'outside',
        ),
        ('private-statement', good, private_statement, 'private', False, 'refused', 'changes'),
        ('known-good', good, [], 'all', False, 'ok', n_all),
        ('known-good-private', good, [], 'private', False, 'ok', n_private),
    ]
    only = set(sys.argv[sys.argv.index('--only') + 1].split(',')) if '--only' in sys.argv else None
    results, wrong = {}, []
    for name, sources, edits, split, deep, status, proved in cases:
        if only and name not in only:
            continue
        case = tree(work, name, sources, edits)
        if proved == 'outside':
            proved = outside(work, case, [S1Q1, SIGNS])
        r = sealed(case, work / 'repo', sealed_dir(work, split, deep), packages)
        r['expected'] = {'status': status, 'raw': proved}
        if isinstance(proved, str):  # a refusal: its reason must be the edit's, not some other fault
            r['as_expected'] = r['status'] == status and proved in str(r.get('detail'))
        else:
            r['as_expected'] = r['status'] == status and r.get('raw') == proved
        results[name] = r
        if not r['as_expected']:
            wrong.append(name)
        print(
            json.dumps({'case': name, **{k: (v[:300] if isinstance(v, str) else v) for k, v in r.items()}}), flush=True
        )
    path = work / 'selftest.json'
    old = json.loads(path.read_text()) if path.exists() else {}
    path.write_text(json.dumps({**old, **results}, indent=1) + '\n')
    if wrong:
        print(f'not as expected: {", ".join(wrong)}', file=sys.stderr)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
