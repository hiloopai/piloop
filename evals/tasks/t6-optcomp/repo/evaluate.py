"""Build the library, audit what Lean compiled, and print ``proved``: the share of the targets proved.

A target is proved when its module builds, its statement is the one in ``spec.json`` (the hash of
the elaborated type), and its proof uses no axiom beyond ``propext``, ``Classical.choice`` and
``Quot.sound`` — so no ``sorry``, no ``native_decide``, no axiom of your own. The audit
(``check/Audit.lean``) reads the compiled ``.olean`` files and replays every constant of the library
through Lean's kernel.

The whole version is refused, and scores nothing, when the kernel rejects a constant, a definition
or a statement that ``spec.json`` protects has changed, the library declares an axiom, or a source
uses a command that runs code or skips the kernel (``BANNED``) more often than the starting repo does
(``spec.json``'s ``allowed``). The reasons are printed on stderr and
written, with every unproved target, to ``build/report.txt``.

A run may also get ``spec.private.json`` and its modules, a private split whose statements are in no
repository: its targets count in ``proved`` too, and alone in ``proved_private``.

Usage: ``python3 evaluate.py`` at the root of the repo, with Mathlib's built packages in
``.lake/packages``. Prints one JSON line, ``{"metrics": {"proved", "proved_count", "build_seconds"}}``.
"""

import contextlib
import json
import os
import re
import resource
import shutil
import signal
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BUILD_TIMEOUT = 3600
"""Wall seconds for ``lake build``; the reference builds in about five minutes on four cores."""
AUDIT_TIMEOUT = 1800
BANNED = re.compile(
    r'debug\.skipKernelTC|implemented_by|@\[\s*extern|\bextern\s+"|\binitialize\b|builtin_initialize|'
    r'run_cmd|run_elab|run_meta|#eval|#exit|\bIO\.|\bunsafe\b|native_decide|ofReduceBool|trustCompiler|'
    r'^\s*axiom\b|\belab\b|elab_rules|addDecl|Environment',
    re.M,
)
"""Commands that run code while building, or that trust something other than the kernel. A proof of
these targets needs none of them."""


def code_of(text: str) -> str:
    """The Lean source without its comments."""
    out, i, depth = [], 0, 0
    while i < len(text):
        if text.startswith('/-', i):
            depth, i = depth + 1, i + 2
        elif depth and text.startswith('-/', i):
            depth, i = depth - 1, i + 2
        elif depth:
            i += 1
        elif text.startswith('--', i):
            j = text.find('\n', i)
            i = len(text) if j < 0 else j
        else:
            out.append(text[i])
            i += 1
    return ''.join(out)


def banned(text: str) -> Counter[str]:
    """How many times each ``BANNED`` command occurs in a Lean source, outside its comments."""
    return Counter(m.group(0).strip() for m in BANNED.finditer(code_of(text)))


def sources(root: Path) -> dict[str, Path]:
    """Every module of the library, by name."""
    return {str(p.relative_to(root))[:-5].replace('/', '.'): p for p in sorted((root / 'Silmaril').rglob('*.lean'))}


def imports(path: Path) -> list[str]:
    """The modules a Lean file imports."""
    return re.findall(r'^\s*(?:public\s+)?(?:meta\s+)?import\s+(?:all\s+)?([\w.]+)', path.read_text(), re.M)


PACKAGES = Path(os.environ.get('OPTCOMP_PACKAGES', '/opt/lean/packages'))
"""Mathlib and its dependencies, built, where the task's image keeps them."""


def lean_env(root: Path) -> dict[str, str]:
    """The environment that runs Lean on ``root``: its packages' and its own build directories on ``LEAN_PATH``."""
    packages = root / '.lake' / 'packages'
    if not packages.exists() and PACKAGES.is_dir():  # a checkout without them, as a scorer's tree is
        packages.parent.mkdir(exist_ok=True)
        packages.symlink_to(PACKAGES)
    paths = [str(p / '.lake/build/lib/lean') for p in sorted(packages.iterdir())] + [str(root / '.lake/build/lib/lean')]
    return {**os.environ, 'LEAN_PATH': ':'.join(paths)}


def run(argv: list[str], root: Path, timeout: int) -> subprocess.CompletedProcess[str]:
    """Run in a session of its own, and kill whatever it leaves behind."""
    proc = subprocess.Popen(
        argv,
        cwd=root,
        env=lean_env(root),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as e:
        os.killpg(proc.pid, signal.SIGKILL)
        raise RuntimeError(f'{argv[0]} took longer than {timeout} s') from e
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


def build(root: Path) -> tuple[set[str], list[str], float]:
    """Build the library: the modules that failed, their errors, and the CPU seconds."""
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    done = run(['lake', 'build'], root, BUILD_TIMEOUT)
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    failed = set(re.findall(r'✖ \[\d+/\d+\] Building ([\w.]+)', done.stdout))
    errors = [line for line in done.stdout.splitlines() if line.startswith('error:')]
    cpu = after.ru_utime + after.ru_stime - before.ru_utime - before.ru_stime
    return failed, errors, round(cpu, 1)


def audit(root: Path, modules: list[str]) -> dict:
    """The audit of ``modules`` as built in ``root`` (``check/Audit.lean``)."""
    done = run(['lean', '--run', str(root / 'check' / 'Audit.lean'), *modules], root, AUDIT_TIMEOUT)
    if done.returncode != 0:
        raise RuntimeError(f'the audit failed\n{done.stderr[-2000:]}')
    return json.loads(done.stdout)


def verdict(root: Path, spec: dict) -> dict:
    """Build and audit the version at ``root`` against ``spec`` (a ``spec.json``)."""
    mods = sources(root)
    allowed = spec.get('allowed', {})
    refused = [
        f'{name}: {match!r}'
        for name, path in mods.items()
        for match, n in banned(path.read_text()).items()
        if n > allowed.get(name, {}).get(match, 0)
    ]
    if refused:
        return {'status': 'refused', 'reasons': ['banned command in ' + r for r in refused], 'proved': []}
    shutil.rmtree(root / '.lake' / 'build', ignore_errors=True)  # never audit an olean that this build did not write
    started = time.monotonic()
    failed, errors, cpu = build(root)
    broken = set(failed)
    while True:  # a module that imports a broken one is broken
        more = {m for m, p in mods.items() if m not in broken and any(i in broken for i in imports(p))}
        if not more:
            break
        broken |= more
    built = [m for m in mods if m not in broken]
    report = audit(root, built)
    reasons = []
    if report['replay'] != 'ok':
        reasons.append(f'the kernel rejects a constant: {report["replay"][:500]}')
    consts = {c['name']: c for c in report['constants']}
    reasons += [f'declares the axiom {c["name"]}' for c in report['constants'] if c['kind'] == 'axiom']
    for name, want in spec['protected'].items():
        have = consts.get(name)
        if have is not None and (have['kind'], have['hash']) != (want['kind'], want['hash']):
            reasons.append(f'changes {name}')
        elif have is None and want['kind'] != 'theorem' and spec['modules_of'].get(name) in built:
            reasons.append(f'deletes {name}')
    axioms = {a['name']: set(a['axioms']) for a in report['axioms']}
    allowed = set(spec['standard_axioms'])
    proved, unproved = [], {}
    for t in spec['targets']:
        if t not in consts:
            unproved[t] = 'missing (its module did not build)' if spec['modules_of'].get(t) in broken else 'missing'
        elif not axioms.get(t, set()) <= allowed:
            unproved[t] = 'uses ' + ', '.join(sorted(axioms[t] - allowed))
        else:
            proved.append(t)
    return {
        'status': 'refused' if reasons else 'ok',
        'reasons': reasons,
        'proved': proved,
        'unproved': unproved,
        'build_errors': errors,
        'broken_modules': sorted(broken),
        'build_seconds': cpu,
        'wall_seconds': round(time.monotonic() - started, 1),
    }


def load_spec(root: Path) -> dict:
    """``spec.json`` with ``spec.private.json`` merged in when it is there, and each split's targets."""
    specs = [
        json.loads((root / name).read_text()) for name in ('spec.json', 'spec.private.json') if (root / name).exists()
    ]
    merged = {
        'targets': [],
        'protected': {},
        'modules_of': {},
        'splits': {},
        'standard_axioms': specs[0]['standard_axioms'],
        'allowed': {},
    }
    for spec in specs:
        merged['targets'] += spec['targets']
        for module, constants in spec['protected'].items():  # {module: {name: "kind hash"}}: each name once
            for name, meaning in constants.items():
                kind, hash_ = meaning.split()
                merged['protected'][name] = {'kind': kind, 'hash': hash_}
                merged['modules_of'][name] = module
        merged['allowed'].update(spec.get('allowed', {}))
        merged['splits'][spec.get('split', 'public')] = spec['targets']
    return merged


def score(root: Path, spec: dict) -> dict:
    """The metrics of the version at ``root``. Raises ``RuntimeError`` with the reasons when it is refused."""
    v = verdict(root, spec)
    lines = [f'status: {v["status"]}', *v['reasons'], f'proved {len(v["proved"])} of {len(spec["targets"])}']
    lines += [f'unproved {t}: {why}' for t, why in sorted(v.get('unproved', {}).items())]
    lines += v.get('build_errors', [])[:200]
    (root / 'build').mkdir(exist_ok=True)
    (root / 'build' / 'report.txt').write_text('\n'.join(lines) + '\n')
    if v['status'] != 'ok':
        raise RuntimeError('refused: ' + '; '.join(v['reasons'][:20]))
    n, proved = len(spec['targets']), set(v['proved'])
    metrics = {'proved': round(len(proved) / n, 6), 'proved_count': len(proved), 'build_seconds': v['build_seconds']}
    for split, targets in spec.get('splits', {}).items():
        if split != 'public':
            metrics[f'proved_{split}'] = round(len(proved & set(targets)) / len(targets), 6)
    return metrics


def main() -> int:
    """Print the metrics as one JSON line, or the reason of the refusal on stderr."""
    spec = load_spec(ROOT)
    try:
        metrics = score(ROOT, spec)
    except RuntimeError as e:
        print(e, file=sys.stderr)
        return 1
    line = json.dumps({'metrics': metrics})
    if path := os.environ.get('PILOOP_RESULT_FILE'):
        with open(path, 'a') as f:
            f.write(line + '\n')
    else:
        print(line)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
