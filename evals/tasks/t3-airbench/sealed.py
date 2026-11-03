"""T3's sealed score: the change's training time over the starting code's, paired on one GPU, on a split no agent reads.

Run as ``python sealed.py <tree> <base> <sealed> <out>``, with ``<tree>/evaluate.py`` the task's own and ``<base>`` the
starting code. ``<sealed>/meta.json`` names the split: ``train``, a list of files, and ``test``, each in ``<sealed>`` or
an absolute path, such as the task's dev data in the image, which no version can change; ``seeds``; the accuracy
``floor``; and the ``base`` and ``ref`` ratios that score 0 and 1. The base runs first, then the change, both through
the GPU lock where the machine has one: timings differ by a few percent from one GPU to the next, so only a ratio
taken on one GPU is comparable. Writes ``{"score", "status", "detail", "raw"}`` to ``<out>``, ``raw`` being the ratio; ``status`` is ``ok``, ``crash``
or ``infra_error``.

The floors in the metas agree with the dev floor in ``evaluate.py``, each measured on L4:

- piloop's split (``t3-airbench.campaign``, 5 seeds): 0.9296, the starting code's mean of 0.9319 over 20 evaluations,
  less 3 standard errors of a 5-seed mean, as the dev floor (0.9300, from 0.9322). The starting code passed 20 of 20.
- The holdout (``t3-airbench``, 10 seeds): 0.9354, the starting code's 0.9402 less 2.2 times the dev floor's margin:
  the test set loses about 2.2 times the accuracy that the agents' 5k images lose.
"""

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

LOCK = Path('/opt/evals/gpu-lock')
START = Path('/opt/evals/data/t3-airbench')
"""The starting code that the image fetched, which a base made from the task's repo alone lacks."""


def where(name: str, sealed: Path) -> str:
    """The file that ``name`` in the meta names: an absolute path, or one in ``sealed``."""
    return str(sealed / name)


def seconds(tree: Path, evaluate: Path, meta: dict, sealed: Path) -> tuple[dict | None, str]:
    """The metrics of ``tree`` on the split, or None and why it failed."""
    argv = [sys.executable, str(evaluate), '--train', *(where(n, sealed) for n in meta['train'])]
    argv += ['--test', where(meta['test'], sealed), '--seeds', str(meta['seeds']), '--floor', str(meta['floor'])]
    done = subprocess.run([str(LOCK), *argv] if LOCK.exists() else argv, cwd=tree, capture_output=True, text=True)
    if done.returncode != 0:
        return None, done.stderr.strip()[-2000:]
    return json.loads(done.stdout.strip().splitlines()[-1])['metrics'], ''


OOM = 'out of memory'
"""In the reason of a failed run: the GPU ran out of memory, which says nothing of the change."""


def main(tree: Path, base: Path, sealed: Path) -> dict[str, object]:
    """Score ``tree`` against ``base`` on the split of ``sealed``."""
    meta = json.loads((sealed / 'meta.json').read_text())
    with tempfile.TemporaryDirectory() as tmp:
        start = Path(tmp) / 'base'
        shutil.copytree(base, start, ignore=shutil.ignore_patterns('.git', 'data'))
        for f in START.glob('*.py') if START.is_dir() else []:
            if not (start / f.name).exists():
                shutil.copy(f, start / f.name)
        reference, why = seconds(start, tree / 'evaluate.py', meta, sealed)
        if reference is None:
            return {'score': None, 'status': 'infra_error', 'detail': f'the starting code failed: {why}', 'raw': None}
        mine, why = seconds(tree, tree / 'evaluate.py', meta, sealed)
    if mine is None:
        status = (
            'infra_error' if OOM in why else 'crash'
        )  # a GPU out of memory is the machine's fault, not the change's
        return {'score': None, 'status': status, 'detail': why, 'raw': None}
    raw = mine['seconds'] / reference['seconds']
    score = (raw - meta['base']) / (meta['ref'] - meta['base'])
    detail = f'{mine["seconds"]:.3f} s against {reference["seconds"]:.3f} s, accuracy {mine["accuracy"]:.4f}'
    return {'score': score, 'status': 'ok', 'detail': detail, 'raw': raw}


if __name__ == '__main__':
    Path(sys.argv[4]).write_text(json.dumps(main(*(Path(a) for a in sys.argv[1:4]))))
