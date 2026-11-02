"""The lpaq task's sealed score: the codec's round trip of the holdout, plus the xz size of its sources.

Run as ``python sealed.py <tree> <base> <sealed> <out>``, in a sandbox without network, with ``<tree>/evaluate.py`` the
task's own. ``<sealed>`` holds ``holdout.bin``, and ``meta.json`` with ``base`` and ``ref``, the bytes that score 0 and
1. Writes one JSON object to ``<out>``, ``{"score", "status", "detail", "raw"}``. A round trip over ``SECONDS`` of CPU
breaks the brief's rule, and scores nothing (``over_limit``).
"""

import importlib.util
import json
import sys
from pathlib import Path

SECONDS = 120
"""The most CPU seconds of compression plus decompression: the brief's rule, the dev evaluator's guardrail floor."""


def main(tree: Path, base: Path, sealed: Path) -> dict[str, object]:
    """Score the holdout of ``sealed`` with the evaluator in ``tree``."""
    spec = importlib.util.spec_from_file_location('evaluate', tree / 'evaluate.py')
    assert spec is not None and spec.loader is not None
    evaluate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(evaluate)
    meta = json.loads((sealed / 'meta.json').read_text())
    try:
        metrics = evaluate.score(sealed / 'holdout.bin', tree)
    except RuntimeError as e:
        return {'score': None, 'status': 'crash', 'detail': str(e)[-2000:], 'raw': None}
    raw = metrics['bytes']
    detail = f'{raw} bytes, {metrics["seconds"]} s'
    if metrics['seconds'] > SECONDS:
        return {'score': None, 'status': 'over_limit', 'detail': f'{detail}: over {SECONDS} s of CPU', 'raw': None}
    score = (raw - meta['base']) / (meta['ref'] - meta['base']) if 'ref' in meta else None
    return {'score': score, 'status': 'ok', 'detail': detail, 'raw': raw}


if __name__ == '__main__':
    Path(sys.argv[4]).write_text(json.dumps(main(*(Path(a) for a in sys.argv[1:4]))))
