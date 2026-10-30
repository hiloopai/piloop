"""Score ``compressor.py`` on ``data/dev.bin``, the first 10 MB of enwik8, and print ``bytes`` and ``seconds``.

``bytes`` is the compressed size plus the size of ``compressor.py``: the decompressor counts. ``seconds`` is the CPU
time (user + system) of compression plus decompression, each in its own process, so the compressor cannot change the
result. A round trip that does not give back the exact input, or a failed step, exits non-zero with the reason.
"""

import json
import os
import resource
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent
COMPRESSOR = ROOT / 'compressor.py'
TIMEOUT = 300
STEP = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import compressor
src, dst = Path(sys.argv[3]), Path(sys.argv[4])
dst.write_bytes(getattr(compressor, sys.argv[2])(src.read_bytes()))
"""


def score(data: Path) -> dict[str, float]:
    """The metrics of a round trip of ``data``. Raises ``RuntimeError`` with the reason when it fails."""
    original = data.read_bytes()
    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)
        (t / 'input').write_bytes(original)
        for step, src, dst in (('compress', 'input', 'blob'), ('decompress', 'blob', 'output')):
            try:
                proc = subprocess.run(
                    [sys.executable, '-c', STEP, str(ROOT), step, str(t / src), str(t / dst)],
                    capture_output=True,
                    text=True,
                    timeout=TIMEOUT,
                    cwd=t,
                )
            except subprocess.TimeoutExpired as e:
                raise RuntimeError(f'{step} took longer than {TIMEOUT} s') from e
            if proc.returncode != 0:
                raise RuntimeError(f'{step} failed with exit code {proc.returncode}\n{proc.stderr[-4000:]}')
        if (t / 'output').read_bytes() != original:
            raise RuntimeError('decompress(compress(data)) is not equal to data')
        compressed = (t / 'blob').stat().st_size
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    return {'bytes': compressed + COMPRESSOR.stat().st_size, 'seconds': round(usage.ru_utime + usage.ru_stime, 2)}


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
