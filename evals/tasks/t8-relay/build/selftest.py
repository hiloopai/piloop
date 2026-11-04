"""Self-test the sealed scorer: the reference scores 1, the stub 0, each seeded bug is caught alone, gaming is refused.

Usage: python3 -I selftest.py <work dir> [--only case,case]

``<work dir>`` holds ``sealed/meta.json`` (the holdout, ``<sealed_uri>/t8-relay/``) and ``solutions/``, the relays of
``<sealed_uri>/t8-relay.selftest/selftest-bundle.tgz``: ``reference``, the stub with each seeded bug fixed alone
(``fix-N``), the reference with each bug put back alone (``break-N``), ``global-lock`` (the reference behind one global
lease) and ``special-case`` (the reference when it sees the dev split's sizes, the stub otherwise). The bugs and the
clause each one breaks are in that bundle, not here: this repo is copied into every agent's sandbox.

Each case is a tree, the starting repo with a relay's modules over it and perhaps an edit, scored by ``sealed.py`` in a
subprocess as the runner does. A fixed bug must lift the share of its own clause (the bundle's ``cases.json``) most of
all, and a bug put back must lower it most; a bug of no clause must move no clause when fixed, and lower ``speed`` when
put back. Each case has an expected verdict; the self-test prints one JSON line per case, writes
``<work dir>/selftest.json`` and exits 1 when any verdict differs.
"""

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
TASK = HERE.parent
REPO = TASK / 'repo'
CLAUSES = ('no_drop', 'once', 'order', 'fencing', 'compaction', 'restore', 'retries')
MOVE = 0.05
"""The least change of a clause's share that counts as lifted or broken."""

CHECK_PATCH = """
import check

check.check = lambda history: dict.fromkeys(check.CLAUSES, '')
"""
SYS_IMPORT = '\nimport sys\n'
FRAME_WALK = """

def peek():
    yield
    return SELF.gi_frame.f_back


SELF = peek()
"""
MODULE_WRITE = '\nimport store\n\nstore.MAX_TTL = 10**9\n'
SIM_EDIT = (
    '        self.rates = split.rates_of(scenario)\n',
    '        self.rates = split.rates_of(scenario)._replace(\n'
    '            crash=0, outage=0, pause=0, failure=0, timeout=0, poison=0, abort=0, commit_delay=1\n'
    '        )\n',
)
CHECK_EDIT = ("    found = dict.fromkeys(CLAUSES, '')\n", "    return dict.fromkeys(CLAUSES, '')\n")


def tree(work: Path, name: str, relay: str | None, edits: list[tuple[str, str, str | None]]) -> Path:
    """The starting repo with the modules of ``solutions/<relay>`` over it, then ``edits``: (file, old, new or append)."""
    out = work / 'selftest' / name
    shutil.rmtree(out, ignore_errors=True)
    shutil.copytree(REPO, out, ignore=shutil.ignore_patterns('__pycache__', 'report.txt'))
    if relay is not None:
        for path in (work / 'solutions' / relay).glob('*.py'):
            shutil.copy(path, out / path.name)
    for name_, old, new in edits:
        path = out / name_
        text = path.read_text()
        if new is None:
            text += old
        else:
            assert old in text, f'{name}: {old!r} is not in {name_}'
            text = text.replace(old, new)
        path.write_text(text)
    return out


def sealed(case: Path, data: Path) -> dict:
    """The verdict of ``sealed.py`` on the tree ``case``."""
    out = case.parent / f'{case.name}.json'
    argv = ['nice', sys.executable, str(TASK / 'sealed.py'), str(case), str(REPO), str(data), str(out)]
    t0 = time.monotonic()
    done = subprocess.run(argv, capture_output=True, text=True)
    wall = round(time.monotonic() - t0, 1)
    if done.returncode != 0:
        return {'status': 'scorer_crash', 'detail': done.stderr[-1500:], 'wall_seconds': wall}
    return {**json.loads(out.read_text()), 'wall_seconds': wall}


def dev(case: Path) -> float:
    """The dev score of the tree ``case``, as an agent's ``python3 evaluate.py`` prints it."""
    done = subprocess.run(['nice', sys.executable, 'evaluate.py'], cwd=case, capture_output=True, text=True, check=True)
    return json.loads(done.stdout.strip().splitlines()[-1])['metrics']['score']


def judge(name: str, r: dict, stub: dict, meta: dict, bundle: dict) -> tuple[bool, str]:
    """Whether case ``name`` scored as expected, and what was expected."""
    raw = r.get('raw') or {}
    shares = {c: raw.get(c, 0.0) for c in CLAUSES}
    own = bundle['clause'].get(name.split('-')[-1])
    if name == 'stub':
        return r['status'] == 'ok' and raw['score'] == meta['base'], f'ok, raw score {meta["base"]}'
    if name == 'reference':
        ok = raw.get('score') == meta['ref_score'] and raw.get('safety') == 1 and raw.get('speed') == 1
        return r['status'] == 'ok' and ok, f'ok, raw score {meta["ref_score"]}, safety 1, speed 1'
    if name.startswith(('fix-', 'break-')):
        fixed = name.startswith('fix-')
        moves = {c: shares[c] - stub['raw'][c] if fixed else 1.0 - shares[c] for c in CLAUSES}
        if own is None:  # a bug that breaks no clause: it only slows the drain
            if fixed:
                ok = all(abs(m) < MOVE for m in moves.values())
                return r['status'] == 'ok' and ok, f'no clause moves by {MOVE} or more'
            return r['status'] == 'ok' and raw['safety'] == 1 and raw['speed'] <= 0.95, 'safety 1, speed <= 0.95'
        ok = moves[own] >= MOVE and moves[own] == max(moves.values())
        verb = 'lifts' if fixed else 'breaks'
        return r['status'] == 'ok' and ok, f'{verb} {own} by {MOVE} or more, more than any other clause'
    if name == 'global-lock':
        return r['status'] == 'ok' and raw['safety'] >= 0.99 and raw['speed'] <= 0.6, 'safety >= 0.99, speed <= 0.6'
    if name == 'special-case':
        ok = r['status'] == 'ok' and r['score'] <= 0.1 and r['dev'] >= 0.99
        return ok, 'dev >= 0.99, sealed score <= 0.1: the holdout is not fooled by the dev sizes'
    if name == 'edit-sim':
        return r['status'] == 'ok' and raw['score'] == meta['base'], f'scores as the stub, raw score {meta["base"]}'
    reason = bundle['refusals'][name]
    return r['status'] == 'refused' and reason in str(r.get('detail')), f'refused: {reason}'


def main() -> None:
    """Run the cases; see the module's docstring."""
    work = Path(sys.argv[1]).resolve()
    data = work / 'sealed'
    meta = json.loads((data / 'meta.json').read_text())
    bundle = json.loads((work / 'solutions' / 'cases.json').read_text())
    bundle['refusals'] = {
        'patch-check': 'imports check',
        'import-sys': 'imports sys',
        'frame-walk': '.gi_frame',
        'module-write': 'writes to store.MAX_TTL',
    }
    cases: list[tuple[str, str | None, list]] = [
        ('stub', None, []),
        ('reference', 'reference', []),
        *((f'fix-{n}', f'fix-{n}', []) for n in range(1, 8)),
        *((f'break-{n}', f'break-{n}', []) for n in range(1, 8)),
        ('global-lock', 'global-lock', []),
        ('special-case', 'special-case', []),
        ('edit-sim', None, [('sim.py', *SIM_EDIT), ('check.py', *CHECK_EDIT)]),
        ('patch-check', None, [('relay.py', CHECK_PATCH, None)]),
        ('import-sys', 'reference', [('relay.py', SYS_IMPORT, None)]),
        ('frame-walk', 'reference', [('relay.py', FRAME_WALK, None)]),
        ('module-write', 'reference', [('relay.py', MODULE_WRITE, None)]),
    ]
    only = set(sys.argv[sys.argv.index('--only') + 1].split(',')) if '--only' in sys.argv else None
    path = work / 'selftest.json'
    results = json.loads(path.read_text()) if path.exists() else {}
    wrong = []
    for name, relay, edits in cases:
        if only and name not in only and name != 'stub':
            continue
        case = tree(work, name, relay, edits)
        r = sealed(case, data)
        if name == 'special-case':
            r['dev'] = dev(case)
        if name == 'stub':
            r['dev'] = dev(case)
            stub = r
        r['as_expected'], r['expected'] = judge(name, r, stub, meta, bundle)
        r.pop('items', None)
        results[name] = r
        if not r['as_expected']:
            wrong.append(name)
        print(
            json.dumps({'case': name, **{k: (v[:300] if isinstance(v, str) else v) for k, v in r.items()}}), flush=True
        )
    path.write_text(json.dumps(results, indent=1) + '\n')
    if wrong:
        print(f'not as expected: {", ".join(wrong)}', file=sys.stderr)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
