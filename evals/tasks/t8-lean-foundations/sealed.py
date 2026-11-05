"""The optcomp task's sealed score: the share of the targets proved, checked from a clean build.

Run as ``python sealed.py <tree> <base> <sealed> <out>``, in a sandbox without network. Only the
``.lean`` files under ``<tree>/Silmaril`` are taken from the version: ``evaluate.py`` comes from ``<base>``, the
starting commit, and the project files, the audit and the ``spec*.json`` files from ``<sealed>``, and Mathlib
from the built packages at
``$OPTCOMP_PACKAGES``, whose ``.olean`` digest must match ``<sealed>/meta.json`` before and after the
build (a build runs the version's code, and must not leave Mathlib changed). Writes one JSON object
to ``<out>``, ``{"score", "status", "detail", "raw"}``: ``score`` is the share of the targets of
``meta.json``'s ``split`` proved (``all``, or ``private``: the split whose statements are in no repository),
``raw`` the number proved; a refused version (``evaluate.py``'s rules) scores nothing.
"""

import hashlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

PACKAGES = Path(os.environ.get('OPTCOMP_PACKAGES', '/opt/lean/packages'))
FROZEN = ('lakefile.toml', 'lake-manifest.json', 'lean-toolchain')


ARTIFACTS = ('.olean', '.olean.server', '.olean.private')
"""What Lean imports. Lake rewrites its ``.hash`` trace files on a build, and nothing reads them but Lake."""


def digest(packages: Path) -> str:
    """SHA-256 over the names and bytes of every compiled module of the packages."""
    h = hashlib.sha256()
    files = (p for p in packages.glob('*/.lake/build/lib/lean/**/*.olean*') if p.name.endswith(ARTIFACTS))
    for path in sorted(files):
        h.update(str(path.relative_to(packages)).encode() + b'\0')
        with path.open('rb') as f:
            while chunk := f.read(1 << 20):
                h.update(chunk)
    return h.hexdigest()


def main(tree: Path, base: Path, sealed: Path) -> dict[str, object]:
    """Score the version in ``tree`` on the targets in ``sealed``."""
    meta = json.loads((sealed / 'meta.json').read_text())
    if 'packages_digest' in meta and digest(PACKAGES) != meta['packages_digest']:
        return {'score': None, 'status': 'infra_error', 'detail': 'the packages differ before the build', 'raw': None}
    spec = importlib.util.spec_from_file_location('evaluate', base / 'evaluate.py')
    assert spec is not None and spec.loader is not None
    evaluate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(evaluate)
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        for path in (tree / 'Silmaril').rglob('*.lean'):
            dst = work / path.relative_to(tree)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, dst)
        for name in FROZEN + tuple(p.name for p in sealed.glob('spec*.json')):
            shutil.copyfile(sealed / name, work / name)
        shutil.copyfile(base / 'evaluate.py', work / 'evaluate.py')
        (work / 'check').mkdir()
        shutil.copyfile(sealed / 'Audit.lean', work / 'check' / 'Audit.lean')
        (work / '.lake').mkdir()
        (work / '.lake' / 'packages').symlink_to(PACKAGES)
        spec = evaluate.load_spec(work)
        try:
            metrics = evaluate.score(work, spec)
        except RuntimeError as e:
            return {'score': None, 'status': 'refused', 'detail': str(e)[-2000:], 'raw': None}
    if 'packages_digest' in meta and digest(PACKAGES) != meta['packages_digest']:
        return {'score': None, 'status': 'refused', 'detail': 'the build changed the packages', 'raw': None}
    split = meta.get('split', 'all')
    share = metrics['proved'] if split == 'all' else metrics[f'proved_{split}']
    targets = spec['targets'] if split == 'all' else spec['splits'][split]
    raw = round(share * len(targets))
    others = ', '.join(f'{k} {v}' for k, v in metrics.items() if k.startswith('proved_') and k != f'proved_{split}')
    detail = f'{split}: {raw} of {len(targets)} targets proved ({others}); {metrics["build_seconds"]} s of build CPU'
    return {'score': share, 'status': 'ok', 'detail': detail, 'raw': raw}


if __name__ == '__main__':
    Path(sys.argv[4]).write_text(json.dumps(main(*(Path(a) for a in sys.argv[1:4]))))
