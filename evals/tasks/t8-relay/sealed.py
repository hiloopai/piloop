"""The relay task's sealed score: the relay on a sealed split's schedules, normalised by the starting relay and the reference.

Run as ``python sealed.py <tree> <base> <sealed> <out>``, in a sandbox without network. Only the version's own modules
are taken from ``<tree>``: the simulator, the store, the checker and the evaluator (``GIVEN``) are fresh copies from
``<base>``, the starting commit, so an edit to them changes nothing. ``<sealed>/meta.json`` is the split: its sizes,
rates, seeds and scenarios, the reference's drain tick on each schedule (``ref``), and the scores of the starting relay
(``base``) and of the reference (``ref_score``) on it. ``evaluate.py`` scores the relay there, with string hashing
fixed. Writes one JSON object to ``<out>``, ``{"score", "status", "detail", "raw", "items"}``: ``score`` is
``(m - base) / (ref_score - base)`` for the evaluator's metric ``m``, ``raw`` the evaluator's metrics (``score``,
``safety``, ``speed``, each clause's share), ``items`` whether each clause held on every schedule of a group (a scenario
and 30 consecutive seeds of the split), named ``<clause>/<scenario>/<group>``. A relay the audit refuses has status
``refused``, no score and the reasons in ``detail``.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

GIVEN = ('sim.py', 'store.py', 'check.py', 'evaluate.py', 'dev.json', 'CONTRACT.md')


def main(tree: Path, base: Path, sealed: Path) -> dict[str, object]:
    """Score the relay in ``tree`` on the split in ``sealed``."""
    meta = json.loads((sealed / 'meta.json').read_text())
    with tempfile.TemporaryDirectory() as tmp:
        work, out = Path(tmp) / 'tree', Path(tmp) / 'result.json'
        work.mkdir()
        for path in tree.glob('*.py'):
            shutil.copy(path, work / path.name)
        for name in GIVEN:
            shutil.copy(base / name, work / name)
        argv = [sys.executable, 'evaluate.py', '--split', str(sealed / 'meta.json'), '--out', str(out)]
        env = {**os.environ, 'PYTHONHASHSEED': '0'}
        done = subprocess.run(argv, cwd=work, env=env, capture_output=True, text=True)
        if done.returncode != 0:
            return {'score': None, 'status': 'crash', 'detail': done.stderr[-2000:], 'raw': None, 'items': {}}
        result = json.loads(out.read_text())
    if result['refused']:
        return {'score': None, 'status': 'refused', 'detail': result['refused'][-2000:], 'raw': None, 'items': {}}
    metrics = result['metrics']
    norm = (metrics['score'] - meta['base']) / (meta['ref_score'] - meta['base'])
    return {'score': norm, 'status': 'ok', 'detail': result['report'], 'raw': metrics, 'items': result['items']}


if __name__ == '__main__':
    Path(sys.argv[4]).write_text(json.dumps(main(*(Path(a) for a in sys.argv[1:4]))))
