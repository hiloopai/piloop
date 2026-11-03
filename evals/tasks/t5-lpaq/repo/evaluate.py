"""Build the codec, round-trip ``data/dev.bin`` through it, and print ``bytes`` and ``seconds``.

``bytes`` is the compressed size plus the xz size of the codec's sources: every file of the repo except this one, the
brief, the data, the build and the project files, so a model or a dictionary stored in any committed file counts.
``seconds`` is the CPU time of compression plus decompression, the build not counted, scaled to the reference machine:
times ``CANONICAL`` over this machine's own time of lpaq1's round trip of the same input (``ref_seconds``), which is
timed once per machine and again after ``REFRESH``. ``raw_seconds`` is the CPU time as measured. A build that fails, a
step that fails, or a round trip that does not give back the exact input exits non-zero with the reason.
"""

import fcntl
import io
import json
import lzma
import os
import re
import resource
import subprocess
import sys
import tarfile
import tempfile
import time
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
REFERENCE = ROOT / 'data' / 'lpaq1-ref.cpp'
"""lpaq1 as it starts, out of the agents' reach: the machine's yardstick."""
CANONICAL = 17.5
"""lpaq1's round trip of a 10 MB slice of enwik8, in CPU seconds, on the reference machine: the median of four quiet
gcp sandboxes of 2 and 8 CPU (17.40-17.66 s)."""
REFRESH = 1200
"""Seconds after which a machine times its reference again: a long-lived box can get busier or quieter."""
CACHE = Path(tempfile.gettempdir()) / 't5-reference'
PREFIXES = {'26', '2e', '36', '3e', '64', '65', '67'}
"""Legacy prefixes that may come before an EVEX-encoded instruction in objdump's bytes."""
INSTRUCTION = re.compile(r'^\s*[0-9a-f]+:\t((?:[0-9a-f]{2} )+)\s*\t\S')
"""A line of ``objdump -d`` that starts an instruction: its address, its bytes, then its mnemonic."""


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
                with path.open('rb') as f:
                    tar.addfile(info, f)
    return len(lzma.compress(buf.getvalue(), preset=9 | lzma.PRESET_EXTREME))


def build(root: Path) -> Path:
    """Compile every C++ file of the root into one binary."""
    out = root / 'build' / 'codec'
    out.parent.mkdir(exist_ok=True)
    cpp = sorted(str(p) for p in root.glob('*.cpp'))
    proc = subprocess.run(['g++', '-O2', '-o', str(out), *cpp], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f'build failed\n{proc.stderr[-4000:]}')
    if found := avx512(out):
        raise RuntimeError(
            f'the codec has {found} AVX-512 instructions: build for x86-64-v3 (AVX2 at most), as brief.md says, '
            'since a scoring machine may have no AVX-512'
        )
    return out


def avx512(binary: Path) -> int:
    """How many AVX-512 instructions ``binary`` has: those encoded with EVEX, whose first byte is 0x62."""
    dump = subprocess.run(['objdump', '-d', str(binary)], capture_output=True, text=True, check=True).stdout
    count = 0
    for line in dump.splitlines():
        if m := INSTRUCTION.match(line):
            first = next((b for b in m[1].split() if b not in PREFIXES), '')
            count += first == '62'
    return count


def run(codec: Path, data: Path, cwd: Path) -> float:
    """Round-trip ``data`` through ``codec`` in ``cwd``. Its CPU seconds. Raises ``RuntimeError`` when it fails."""
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    for args in (['9', str(data), str(cwd / 'blob')], ['d', str(cwd / 'blob'), str(cwd / 'output')]):
        try:
            proc = subprocess.run([str(codec), *args], capture_output=True, text=True, timeout=TIMEOUT, cwd=cwd)
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(f'codec {args[0]} took longer than {TIMEOUT} s') from e
        if proc.returncode != 0:
            raise RuntimeError(f'codec {args[0]} failed with exit code {proc.returncode}\n{proc.stderr[-4000:]}')
    if (cwd / 'output').read_bytes() != data.read_bytes():
        raise RuntimeError('decompress(compress(data)) is not equal to data')
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    return (after.ru_utime + after.ru_stime) - (before.ru_utime + before.ru_stime)


def time_reference(data: Path) -> float:
    """This machine's CPU seconds of lpaq1's round trip of ``data``."""
    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)
        proc = subprocess.run(['g++', '-O2', '-o', str(t / 'ref'), str(REFERENCE)], capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f'the reference does not build\n{proc.stderr[-2000:]}')
        return run(t / 'ref', data, t)


def reference_seconds(data: Path) -> float:
    """lpaq1's CPU seconds on this machine for ``data``'s size: timed once, then again once it is ``REFRESH`` old.

    The time is kept in ``CACHE`` under a lock, so that evaluations that start together time it once.
    """
    CACHE.mkdir(parents=True, exist_ok=True)
    kept = CACHE / f'{data.stat().st_size}.json'
    with open(CACHE / 'lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if kept.exists():
            found = json.loads(kept.read_text())
            if time.time() - found['at'] < REFRESH:
                return found['seconds']
        seconds = time_reference(data)
        kept.write_text(json.dumps({'at': time.time(), 'seconds': seconds}))
        return seconds


def score(data: Path, root: Path = ROOT) -> dict[str, float]:
    """The metrics of a round trip of ``data``. Raises ``RuntimeError`` with the reason when it fails."""
    codec = build(root)
    with tempfile.TemporaryDirectory() as tmp:
        raw = run(codec, data, Path(tmp))
        compressed = (Path(tmp) / 'blob').stat().st_size
    ref = reference_seconds(data)
    return {
        'bytes': compressed + sources(root),
        'seconds': round(raw * CANONICAL / ref, 2),
        'raw_seconds': round(raw, 2),
        'ref_seconds': round(ref, 2),
    }


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
