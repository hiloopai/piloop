"""Score one tree inside a scoring sandbox, with only the standard library: the dev metrics, then the sealed score.

Usage: ``python scorer.py <task dir> <data dir> <work dir>``, where the work dir holds ``tree.tgz``, ``base.tgz`` and
``sealed.tgz``. The tree gets the task's own ``evaluate.py`` and dev data (``data/``) over its own. It writes one JSON object to
``<work dir>/score.json``:
``{"dev": {"metrics": ...} | null, "dev_error": str, "sealed": <what sealed.py printed>, "cpu": str}``, ``cpu`` the
vendor, family and model of the sandbox's CPU and its AVX-512, such as ``AuthenticAMD 25/17 avx512``.
"""

import json
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

CPUINFO = Path('/proc/cpuinfo')


def cpu() -> str:
    """The vendor, family and model of this machine's CPU, whether it has AVX-512, and its model name when it has one.

    Modal's sandboxes report no model name, and a sandbox of one family and model may lack AVX-512. Daytona's name it,
    such as ``AMD EPYC 9254 24-Core Processor``.
    """
    fields: dict[str, str] = {}
    for line in CPUINFO.read_text().splitlines():
        key, _, value = line.partition(':')
        fields.setdefault(key.strip(), value.strip())
    avx512 = 'avx512' if 'avx512f' in fields.get('flags', '').split() else 'no-avx512'
    found = f'{fields.get("vendor_id", "?")} {fields.get("cpu family", "?")}/{fields.get("model", "?")} {avx512}'
    name = fields.get('model name', 'unknown')
    return found if name in ('', 'unknown') else f'{found} ({name})'


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
        'cpu': cpu(),
    }


if __name__ == '__main__':
    work = Path(sys.argv[3])
    (work / 'score.json').write_text(json.dumps(main(Path(sys.argv[1]), Path(sys.argv[2]), work)))
