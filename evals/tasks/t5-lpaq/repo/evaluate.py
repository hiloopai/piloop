"""Build the codec, round-trip ``data/dev.bin`` through it, and print ``bytes`` and ``seconds``.

``bytes`` is the compressed size plus the xz size of the codec's sources: every file of the repo except this one, the
brief, the data, the build and the project files, so a model or a dictionary stored in any committed file counts. ``seconds`` is
the CPU time of compression plus decompression. A build that fails, a step that fails, or a round trip that does not
give back the exact input exits non-zero with the reason.
"""

import io
import json
import lzma
import os
import resource
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent
SKIP = {
    'evaluate.py',
    'brief.md',
    'data',
    'build',
    '.git',
    '.venv',
    '__pycache__',
    'pyproject.toml',
    'uv.lock',
    '.gitignore',
}
TIMEOUT = 300


def files(root: Path) -> list[Path]:
    """The files of the version: git's tracked files in a checkout, else every file of the tree."""
    listed = subprocess.run(['git', '-C', str(root), 'ls-files', '-z'], capture_output=True)
    if listed.returncode == 0:
        return [root / name for name in listed.stdout.decode().split('\0') if name]
    return [path for path in root.rglob('*') if path.is_file()]


def sources(root: Path) -> int:
    """The xz size of a deterministic tar of the codec's files."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w') as tar:
        for path in sorted(files(root)):
            rel = path.relative_to(root)
            if path.is_file() and not SKIP & set(rel.parts):
                info = tarfile.TarInfo(str(rel))
                info.size = path.stat().st_size
                tar.addfile(info, path.open('rb'))
    return len(lzma.compress(buf.getvalue(), preset=9 | lzma.PRESET_EXTREME))


def build(root: Path) -> Path:
    """Compile every C++ file of the root into one binary."""
    out = root / 'build' / 'codec'
    out.parent.mkdir(exist_ok=True)
    cpp = sorted(str(p) for p in root.glob('*.cpp'))
    proc = subprocess.run(['g++', '-O2', '-o', str(out), *cpp], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f'build failed\n{proc.stderr[-4000:]}')
    return out


def score(data: Path, root: Path = ROOT) -> dict[str, float]:
    """The metrics of a round trip of ``data``. Raises ``RuntimeError`` with the reason when it fails."""
    codec = build(root)
    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)
        for args in (['9', str(data), str(t / 'blob')], ['d', str(t / 'blob'), str(t / 'output')]):
            try:
                proc = subprocess.run([str(codec), *args], capture_output=True, text=True, timeout=TIMEOUT, cwd=t)
            except subprocess.TimeoutExpired as e:
                raise RuntimeError(f'codec {args[0]} took longer than {TIMEOUT} s') from e
            if proc.returncode != 0:
                raise RuntimeError(f'codec {args[0]} failed with exit code {proc.returncode}\n{proc.stderr[-4000:]}')
        if (t / 'output').read_bytes() != data.read_bytes():
            raise RuntimeError('decompress(compress(data)) is not equal to data')
        compressed = (t / 'blob').stat().st_size
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    return {'bytes': compressed + sources(root), 'seconds': round(usage.ru_utime + usage.ru_stime, 2)}


def main() -> int:
    """Print the metrics as one JSON line, or the reason of the failure on stderr."""
    try:
        metrics = score(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / 'data' / 'dev.bin')
    except RuntimeError as e:
        print(e, file=sys.stderr)
        return 1
    line = json.dumps({'metrics': metrics})
    if path := os.environ.get('PILOOP_RESULT_FILE'):
        with open(path, 'a') as f:
            f.write(line + '\n')
    else:
        print(line)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
