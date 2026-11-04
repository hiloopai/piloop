"""Build t8-relay's splits: the dev split of the repo, and the campaign and holdout splits for sealed storage.

Usage: ``python3 -I make_task.py <reference dir> <work dir> --salt <secret> [--jobs N]``

``<reference dir>`` holds the reference relay (its ``relay.py`` and any modules it imports), which is in no repository:
it lives in ``<sealed_uri>/t8-relay.selftest/``. Every split's seeds come from this file and ``--salt``: the dev seeds
are 1 to 300, the others are drawn from the salt, which only sealed storage keeps (in the splits themselves). For each
split, the reference must hold every clause on every schedule and drain before the cap; its drain tick on each
schedule becomes the split's ``ref``, which ``speed`` divides by. Then the starting relay and the reference are
scored on the split, and their scores become the split's ``base`` and ``ref_score``: ``sealed.py`` normalises with
them. Writes ``repo/dev.json`` in the task, and ``<work dir>/sealed/meta.json`` (the holdout) and
``<work dir>/campaign/meta.json``, which go to ``<sealed_uri>/t8-relay/`` and ``<sealed_uri>/t8-relay.campaign/``.
"""

import importlib
import json
import os
import random
import shutil
import sys
import tempfile
from pathlib import Path

TASK = Path(__file__).resolve().parent.parent
REPO = TASK / 'repo'
sys.path.insert(0, str(REPO))
evaluate = importlib.import_module('evaluate')
sim = importlib.import_module('sim')
Rates, Split = sim.Rates, sim.Split

DEV_RATES = Rates(
    crash=1, outage=0.25, pause=1, pause_ticks=100, failure=0.05, timeout=0.03, poison=0.03, abort=0.1, commit_delay=30
)
"""A quarter of the highest rates, and the same workload faults: most dev schedules meet few faults."""
MAX_RATES = Rates(
    crash=4, outage=1, pause=4, pause_ticks=100, failure=0.2, timeout=0.1, poison=0.1, abort=0.2, commit_delay=30
)
"""The highest rates ``CONTRACT.md`` allows: the holdout's."""
SMALL = {'workers': 2, 'keys': 3, 'events': 24, 'span': 200, 'cap': 3000}
LARGE = {'workers': 4, 'keys': 6, 'events': 48, 'span': 300, 'cap': 4000}


def splits(salt: str) -> dict[str, object]:
    """The three splits: dev, campaign and holdout, with disjoint seeds."""
    drawn = random.Random(f'{salt}/seeds').sample(range(1000, 10**9), 3300)
    scenarios = list(sim.SCENARIOS)
    return {
        'dev': Split(rates=DEV_RATES, seeds=list(range(1, 301)), scenarios=scenarios, **SMALL),
        'campaign': Split(rates=DEV_RATES, seeds=drawn[:300], scenarios=scenarios, **SMALL),
        'holdout': Split(rates=MAX_RATES, seeds=drawn[300:], scenarios=scenarios, **LARGE),
    }


def build(name: str, split: object, reference: Path, jobs: int | None) -> dict[str, object]:
    """``split`` with the reference's drain ticks and the two scores; exits when the reference fails a schedule."""
    try:
        sources = evaluate.audit(reference)
    except evaluate.Refused as e:
        raise SystemExit(f'the reference is refused: {e}') from e
    runs = evaluate.run_all(sources, split, jobs)
    bad = [(n, v, e) for n, v, _, drained, e in runs if any(v.values()) or not drained]
    if bad:
        raise SystemExit(f'{name}: the reference fails {len(bad)} schedules, the first {bad[0]}')
    meta = evaluate.dump_split(split, {n: drain for n, _, drain, _, _ in runs})
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / 'split.json'
        path.write_text(json.dumps(meta))
        found = {}
        for who, root in (('base', REPO), ('ref_score', reference)):
            tree = Path(tmp) / who
            shutil.copytree(root, tree)
            for given in ('sim.py', 'store.py', 'check.py', 'evaluate.py'):
                shutil.copy(REPO / given, tree / given)
            result = evaluate.score(tree, path, jobs)
            assert isinstance(result['metrics'], dict)
            found[who] = result['metrics']['score']
            shares = {c: round(result['metrics'][c], 4) for c in evaluate.CLAUSES}
            print(f'{name} {who}: {found[who]:.6f} {shares}', flush=True)
    return {**meta, **found}


def main() -> None:
    """Build the splits; see the module's docstring."""
    if sys.flags.hash_randomization:  # as evaluate.py: set orders must not vary between runs
        os.execve(sys.executable, [sys.executable, *sys.argv], {**os.environ, 'PYTHONHASHSEED': '0'})
    reference, work = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
    salt = sys.argv[sys.argv.index('--salt') + 1]
    jobs = int(sys.argv[sys.argv.index('--jobs') + 1]) if '--jobs' in sys.argv else None
    for name, split in splits(salt).items():
        meta = build(name, split, reference, jobs)
        if name == 'dev':
            keep = ('workers', 'keys', 'events', 'span', 'cap', 'rates', 'seeds', 'scenarios', 'ref')
            (REPO / 'dev.json').write_text(json.dumps({k: meta[k] for k in keep}) + '\n')
        else:
            out = work / ('sealed' if name == 'holdout' else 'campaign')
            out.mkdir(parents=True, exist_ok=True)
            (out / 'meta.json').write_text(json.dumps(meta) + '\n')


if __name__ == '__main__':
    main()
