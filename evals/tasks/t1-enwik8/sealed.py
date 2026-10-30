"""T1's sealed score: the round trip of the holdout slice, plus every byte of every file the change added or modified.

Run as ``python sealed.py <tree> <base> <sealed> <out>``, in a sandbox without network, with ``<tree>/evaluate.py`` the
task's own. ``<sealed>`` holds ``holdout.bin``, and ``meta.json`` with ``base`` and ``ref``, the bytes that score 0 and
1. Writes one JSON object to ``<out>``, ``{"score": float | null, "status": "ok" | "crash", "detail": str, "raw": bytes | null}``.
"""

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

IGNORED = {'evaluate.py', 'data', '.venv', '.git', '__pycache__'}
ALWAYS = 'compressor.py'


def files(root: Path) -> dict[str, str]:
    """The sha256 of each file under ``root``, by its relative path, outside ``IGNORED``."""
    found = {}
    for path in root.rglob('*'):
        rel = path.relative_to(root)
        if path.is_file() and not IGNORED & set(rel.parts):
            found[str(rel)] = hashlib.sha256(path.read_bytes()).hexdigest()
    return found


def diff_bytes(tree: Path, base: Path) -> int:
    """The size of each file of ``tree`` that ``base`` lacks or has with other content, and of ``ALWAYS``."""
    mine, theirs = files(tree), files(base)
    changed = {rel for rel, digest in mine.items() if theirs.get(rel) != digest} | {ALWAYS}
    return sum((tree / rel).stat().st_size for rel in changed if (tree / rel).is_file())


def main(tree: Path, base: Path, sealed: Path) -> dict[str, object]:
    """Score the holdout of ``sealed`` with the evaluator in ``tree``."""
    spec = importlib.util.spec_from_file_location('evaluate', tree / 'evaluate.py')
    assert spec is not None and spec.loader is not None
    evaluate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(evaluate)
    meta = json.loads((sealed / 'meta.json').read_text())
    try:
        metrics = evaluate.score(sealed / 'holdout.bin')
    except RuntimeError as e:
        return {'score': None, 'status': 'crash', 'detail': str(e)[-2000:], 'raw': None}
    compressed = metrics['bytes'] - (tree / ALWAYS).stat().st_size
    extra = diff_bytes(tree, base)
    raw = compressed + extra
    score = (raw - meta['base']) / (meta['ref'] - meta['base']) if 'ref' in meta else None
    return {'score': score, 'status': 'ok', 'detail': f'{compressed} compressed + {extra} changed bytes', 'raw': raw}


if __name__ == '__main__':
    Path(sys.argv[4]).write_text(json.dumps(main(*(Path(a) for a in sys.argv[1:4]))))
