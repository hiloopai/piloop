"""T2's sealed score: the same ``pack`` at the hidden values of ``n``, each normalised, then averaged.

Run as ``python sealed.py <tree> <base> <sealed> <out>``, in a sandbox without network, with ``<tree>/evaluate.py`` the
task's own. ``<sealed>/meta.json`` holds ``n``, a list, and ``base`` and ``ref``, the sums of radii that score 0 and 1 at
each ``n``. Writes one JSON object to ``<out>``, ``{"score": float | null, "status": "ok" | "crash", "detail": str, "raw": {n: sum}}``.
"""

import importlib.util
import json
import sys
from pathlib import Path


def main(tree: Path, base: Path, sealed: Path) -> dict[str, object]:
    """Score ``pack`` of ``tree`` at each hidden ``n``."""
    meta = json.loads((sealed / 'meta.json').read_text())
    spec = importlib.util.spec_from_file_location('evaluate', tree / 'evaluate.py')
    assert spec is not None and spec.loader is not None
    evaluate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(evaluate)
    raw = {}
    for n in meta['n']:
        try:
            raw[str(n)] = evaluate.score(n)
        except RuntimeError as e:
            return {'score': None, 'status': 'crash', 'detail': f'n={n}: {str(e)[-2000:]}', 'raw': raw}
    if 'ref' not in meta:
        return {'score': None, 'status': 'ok', 'detail': 'no reference: raw sums only', 'raw': raw}
    parts = [(raw[k] - meta['base'][k]) / (meta['ref'][k] - meta['base'][k]) for k in raw]
    return {'score': sum(parts) / len(parts), 'status': 'ok', 'detail': '', 'raw': raw}


if __name__ == '__main__':
    Path(sys.argv[4]).write_text(json.dumps(main(*(Path(a) for a in sys.argv[1:4]))))
