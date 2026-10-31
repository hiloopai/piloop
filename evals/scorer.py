"""Score one tree inside a scoring sandbox, with only the standard library: the dev metrics, then the sealed score.

Usage: ``python scorer.py <task dir> <data dir> <work dir>``, where the work dir holds ``tree.tgz``, ``base.tgz`` and
``sealed.tgz``. The tree gets the task's own ``evaluate.py`` and dev data (``data/``) over its own. It writes one JSON object to
``<work dir>/score.json``:
``{"dev": {"metrics": ...} | null, "dev_error": str, "sealed": <what sealed.py printed>}``.
"""

import json
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path


def main(task: Path, data: Path, work: Path) -> dict[str, object]:
    """Unpack, score, and report."""
    for name in ('tree', 'base', 'sealed'):
        with tarfile.open(work / f'{name}.tgz') as tar:
            tar.extractall(work / name, filter='data')
    tree = work / 'tree'
    shutil.copy(task / 'repo' / 'evaluate.py', tree / 'evaluate.py')
    if (data / 'data').is_dir():  # the dev data only: the rest of the task's data is in the starting commit
        shutil.copytree(data / 'data', tree / 'data', dirs_exist_ok=True)
    dev = subprocess.run([sys.executable, 'evaluate.py'], cwd=tree, capture_output=True, text=True)
    lines = dev.stdout.strip().splitlines()
    out = work / 'sealed.json'
    sealed = subprocess.run(
        [sys.executable, str(task / 'sealed.py'), str(tree), str(work / 'base'), str(work / 'sealed'), str(out)],
        capture_output=True,
        text=True,
    )
    if sealed.returncode != 0:
        verdict: object = {'score': None, 'status': 'crash', 'detail': sealed.stderr[-2000:]}
    else:
        verdict = json.loads(out.read_text())
    return {
        'dev': json.loads(lines[-1]) if dev.returncode == 0 and lines else None,
        'dev_error': dev.stderr[-1000:],
        'sealed': verdict,
    }


if __name__ == '__main__':
    work = Path(sys.argv[3])
    (work / 'score.json').write_text(json.dumps(main(Path(sys.argv[1]), Path(sys.argv[2]), work)))
