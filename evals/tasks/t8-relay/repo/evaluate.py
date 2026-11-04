"""Run the relay on every schedule of a split, check ``CONTRACT.md``'s clauses on each, and print the score.

The relay is ``relay.py`` and the modules of this directory that it imports, except the given ones (``GIVEN``). They
are audited first (``audit``): a relay that imports outside ``ALLOWED``, reaches into the interpreter, or writes to an
imported module is refused, and scores nothing.

For each schedule (a seed and a scenario), ``check.py`` tells whether each safety clause held, and the run's drain
tick is kept. ``S``, ``safety``, is the mean over the seven clauses of the share of schedules where the clause held.
``E``, ``speed``, is the geometric mean, over the schedules where every clause held, of the reference relay's drain
ticks over this relay's (``ref`` in the split); 0 when no schedule is safe. The score is ``S * (0.8 + 0.2 * E)``.
``report.txt`` gets the first violation of each clause, with the schedule that shows it.

Usage: ``python3 evaluate.py`` scores the dev split (``dev.json``) and prints one JSON line,
``{"metrics": {"score", "safety", "speed", "<clause>", ...}}``. ``python3 evaluate.py --trace <seed> <scenario>``
prints what the store and the sink recorded in that schedule of the dev split, and the clauses' verdicts.
``--split <file> --out <file>`` scores another split and writes the full result there.
"""

import ast
import json
import math
import multiprocessing
import os
import signal
import sys
from pathlib import Path

from check import CLAUSES, check
from sim import Program, Rates, Result, Split, World

ROOT = Path(__file__).resolve().parent
GIVEN = ('sim.py', 'store.py', 'check.py', 'evaluate.py')
ALLOWED = frozenset(
    {
        '__future__',
        'abc',
        'bisect',
        'collections',
        'collections.abc',
        'dataclasses',
        'enum',
        'functools',
        'heapq',
        'itertools',
        'json',
        'math',
        'typing',
        'store',
    }
)
"""The modules a relay may import, besides its own."""
BANNED_NAMES = frozenset(
    {
        '__import__',
        '__builtins__',
        'breakpoint',
        'compile',
        'delattr',
        'eval',
        'exec',
        'getattr',
        'globals',
        'id',
        'input',
        'locals',
        'open',
        'setattr',
        'vars',
    }
)
DUNDER_NAMES = frozenset({'__all__', '__name__', '__slots__'})
"""The only dunder names a relay may use."""
BANNED_ATTRS = frozenset(
    {
        'ag_frame',
        'cr_await',
        'cr_frame',
        'f_back',
        'f_builtins',
        'f_globals',
        'f_locals',
        'gi_frame',
        'gi_yieldfrom',
        'tb_frame',
        'tb_next',
        'with_traceback',
    }
)
"""Attributes that lead to the frames of the simulator; dunder attributes other than ``__init__`` are banned too."""
UNCATCHABLE = frozenset({'BaseException', 'GeneratorExit', 'KeyboardInterrupt', 'SystemExit'})
SECONDS_PER_RUN = 20
"""Wall seconds one schedule may take; past it, the schedule fails every clause."""


class Refused(Exception):
    """The relay breaks a rule of the audit."""


class Overrun(BaseException):
    """A schedule took longer than ``SECONDS_PER_RUN``."""


def violations(tree: ast.AST, name: str, local: set[str]) -> list[str]:
    """What module ``name`` does that the audit refuses; ``local`` are the relay's own modules."""
    out: list[str] = []
    imported: set[str] = set()
    for node in ast.walk(tree):
        where = f'{name}.py:{getattr(node, "lineno", 0)}'
        match node:
            case ast.Import(names=names):
                imported |= {a.asname or a.name.split('.')[0] for a in names}
                out += [f'{where}: imports {a.name}' for a in names if a.name not in ALLOWED | local]
            case ast.ImportFrom(names=names, level=level, module=module):
                imported |= {a.asname or a.name for a in names}
                if level or module is None:
                    out.append(f'{where}: a relative import')
                elif module not in ALLOWED | local:
                    out.append(f'{where}: imports {module}')
            case ast.Name(id=n) if n in BANNED_NAMES or (n.startswith('__') and n not in DUNDER_NAMES):
                out.append(f'{where}: {n}')
            case ast.Attribute(attr=a) if a in BANNED_ATTRS or (a.startswith('__') and a != '__init__'):
                out.append(f'{where}: .{a}')
            case ast.ExceptHandler(type=None):
                out.append(f'{where}: a bare except')
            case ast.ExceptHandler(type=t) if t is not None and UNCATCHABLE & {
                n.id for n in ast.walk(t) if isinstance(n, ast.Name)
            }:
                out.append(f'{where}: catching {ast.unparse(t)}')
    return out + writes(tree, name, imported)


def writes(tree: ast.AST, name: str, imported: set[str]) -> list[str]:
    """The assignments and deletions in module ``name`` that change an attribute or an item of ``imported`` names."""
    out: list[str] = []
    for node in ast.walk(tree):
        targets = []
        match node:
            case ast.Assign(targets=ts) | ast.Delete(targets=ts):
                targets = ts
            case ast.AugAssign(target=t) | ast.AnnAssign(target=t):
                targets = [t]
        for t in targets:
            base = t
            while isinstance(base, ast.Attribute | ast.Subscript):
                base = base.value
            if base is not t and isinstance(base, ast.Name) and base.id in imported:
                line = getattr(node, 'lineno', 0)
                out.append(f'{name}.py:{line}: writes to {ast.unparse(t)}, part of an imported module')
    return out


def modules(root: Path) -> dict[str, ast.Module]:
    """``relay`` and every module of ``root`` it imports, parsed."""
    own = {p.stem for p in root.glob('*.py') if p.name not in GIVEN}
    found: dict[str, ast.Module] = {}
    todo = ['relay']
    while todo:
        name = todo.pop()
        if name in found:
            continue
        found[name] = tree = ast.parse((root / f'{name}.py').read_text(), f'{name}.py')
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                todo += [a.name for a in node.names if a.name in own]
            elif isinstance(node, ast.ImportFrom) and node.module in own:
                todo.append(node.module)
    return found


def audit(root: Path) -> dict[str, str]:
    """The relay's sources by module; raises ``Refused`` with every reason when the audit refuses them."""
    if not (root / 'relay.py').is_file():
        raise Refused('there is no relay.py')
    try:
        found = modules(root)
    except SyntaxError as e:
        raise Refused(f'{e.filename}:{e.lineno}: {e.msg}') from e
    reasons = [r for name, tree in found.items() for r in violations(tree, name, set(found))]
    if reasons:
        raise Refused('; '.join(reasons))
    return {name: (root / f'{name}.py').read_text() for name in found}


def load_split(path: Path) -> tuple[Split, dict[str, int]]:
    """A split file: the split, and the reference relay's drain tick for each schedule (``seed/scenario``)."""
    raw = json.loads(path.read_text())
    rates = Rates(**raw['rates'])
    keys = ('workers', 'keys', 'events', 'span', 'cap', 'seeds', 'scenarios')
    return Split(rates=rates, **{k: raw[k] for k in keys}), raw.get('ref', {})


def dump_split(split: Split, ref: dict[str, int]) -> dict[str, object]:
    """``split`` as its file holds it, with the reference's drain ticks: what ``load_split`` reads."""
    return {**split._asdict(), 'rates': split.rates._asdict(), 'ref': ref}


type Run = tuple[str, dict[str, str], int, bool, str]
"""One schedule's name (``seed/scenario``), the clauses' verdicts, the drain tick, whether it drained, the error."""


PROGRAM: Program | None = None


def on_alarm(signum: int, frame: object) -> None:
    """End the schedule that runs past ``SECONDS_PER_RUN``."""
    raise Overrun


def run_one(job: tuple[Split, int, str]) -> Run:
    """Run one schedule in a pool process: its name, the clauses' verdicts, the drain tick, drained, the error."""
    split, seed, scenario = job
    assert PROGRAM is not None
    signal.setitimer(signal.ITIMER_REAL, SECONDS_PER_RUN)
    try:
        r = World(PROGRAM, split, seed, scenario).run()
        verdict = check(r.history)
    except Overrun:
        return f'{seed}/{scenario}', dict.fromkeys(CLAUSES, 'overrun'), split.cap, False, 'overrun'
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
    return f'{seed}/{scenario}', verdict, r.drain, r.drained, r.error


def start_pool(sources: dict[str, str]) -> None:
    """Set up a pool process: the relay compiled once, the alarm, and its prints silenced."""
    global PROGRAM
    PROGRAM = Program(sources)
    signal.signal(signal.SIGALRM, on_alarm)
    sys.stdout = open(os.devnull, 'w')  # noqa: SIM115 - a relay's prints would bury the result line


def run_all(sources: dict[str, str], split: Split, jobs: int | None = None) -> list[Run]:
    """Run the relay on every schedule of ``split``, in ``jobs`` processes (default: one per CPU)."""
    work = [(split, seed, scenario) for seed in split.seeds for scenario in split.scenarios]
    jobs = jobs or len(os.sched_getaffinity(0))
    with multiprocessing.get_context('fork').Pool(jobs, start_pool, (sources,)) as pool:
        return pool.map(run_one, work, chunksize=max(1, len(work) // (jobs * 8)))


def score(root: Path, split_path: Path, jobs: int | None = None) -> dict[str, object]:
    """Score the relay of ``root`` on a split: ``{"metrics", "items", "report", "refused"}``."""
    try:
        sources = audit(root)
    except Refused as e:
        return {'metrics': None, 'items': {}, 'report': '', 'refused': str(e)}
    split, ref = load_split(split_path)
    runs = run_all(sources, split, jobs)
    shares = {c: sum(not v[c] for _, v, *_ in runs) / len(runs) for c in CLAUSES}
    ratios = [math.log(ref[name] / drain) for name, v, drain, _, _ in runs if not any(v.values()) and name in ref]
    safety = sum(shares.values()) / len(CLAUSES)
    speed = math.exp(sum(ratios) / len(ratios)) if ratios else 0.0
    metrics = {
        'score': safety * (0.8 + 0.2 * speed),
        'safety': safety,
        'speed': speed,
        **shares,
        'safe_share': len(ratios) / len(runs),
        'drained_share': sum(r[3] for r in runs) / len(runs),
    }
    index = {seed: i for i, seed in enumerate(split.seeds)}
    items: dict[str, bool] = {}
    for name, verdict, *_ in runs:
        seed, scenario = name.split('/')
        for c in CLAUSES:
            item = f'{c}/{scenario}/{index[int(seed)] // 30:03d}'
            items[item] = items.get(item, True) and not verdict[c]
    return {'metrics': metrics, 'items': items, 'report': report(runs), 'refused': ''}


def report(runs: list[Run]) -> str:
    """The first violation of each clause, the first relay error, and how to trace them."""
    lines = []
    for c in CLAUSES:
        bad = [(name, v[c]) for name, v, *_ in runs if v[c]]
        if bad:
            lines.append(f'{c}: {len(bad)} of {len(runs)} schedules; first {bad[0][0]}: {bad[0][1]}')
        else:
            lines.append(f'{c}: holds on all {len(runs)} schedules')
    errors = [(name, e) for name, _, _, _, e in runs if e]
    if errors:
        lines.append(f'relay errors in {len(errors)} schedules; first {errors[0][0]}: {errors[0][1]}')
    lines.append('trace one: python3 evaluate.py --trace <seed> <scenario>')
    return '\n'.join(lines) + '\n'


def trace(root: Path, seed: int, scenario: str) -> None:
    """Print one dev schedule's records in order, and the clauses' verdicts."""
    split, ref = load_split(root / 'dev.json')
    r: Result = World(Program(audit(root)), split, seed, scenario).run()
    h = r.history
    records = sorted([*h.commits, *h.attempts, *h.saves, *h.compactions], key=lambda x: x.seq)
    for x in records:
        print(f'{x.tick:5} {type(x).__name__.lower():10} {dict(x._asdict())}')
    print(f'poison: {sorted(h.poison)}')
    print(f'drained: {r.drained} at tick {r.drain} (reference: {ref.get(f"{seed}/{scenario}")}); calls: {r.calls}')
    if r.error:
        print(f'first relay error: {r.error}')
    for c, why in check(h).items():
        print(f'{c}: {why or "holds"}')


def main() -> int:
    """Score the dev split, trace one schedule, or score another split; see the module's docstring."""
    if sys.flags.hash_randomization:  # string hashes, and so set orders, must not vary between runs
        os.execve(sys.executable, [sys.executable, *sys.argv], {**os.environ, 'PYTHONHASHSEED': '0'})
    args = sys.argv[1:]
    if args[:1] == ['--trace']:
        trace(ROOT, int(args[1]), args[2])
        return 0
    split = Path(args[args.index('--split') + 1]) if '--split' in args else ROOT / 'dev.json'
    result = score(ROOT, split)
    if '--out' in args:
        Path(args[args.index('--out') + 1]).write_text(json.dumps(result))
        return 0
    if result['refused']:
        print(f'refused: {result["refused"]}', file=sys.stderr)
        return 1
    (ROOT / 'report.txt').write_text(str(result['report']))
    line = json.dumps({'metrics': result['metrics']})
    if path := os.environ.get('PILOOP_RESULT_FILE'):
        with open(path, 'a') as f:
            f.write(line + '\n')
    else:
        print(line)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
