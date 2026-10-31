"""Build the program, time it on the measurements, and print ``cpu_seconds``: the median CPU time of its runs.

``--quick`` times the 10M-row dev file, five runs; without it, the 50M-row file, three runs. Each run must print
exactly the expected output, or this exits non-zero with the reason. CPU time (user + system of the program alone)
changes little when other jobs share the machine; ``wall_seconds`` is reported beside it. The data file is copied
once to memory (``/dev/shm``) and timed from there, and cargo builds incrementally, so a repeated evaluation costs
little more than its runs.
"""

import json
import os
import resource
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent
BINARY = ROOT / 'target' / 'release' / 'brc'
RUN_SECONDS = 60
BUILD_SECONDS = 600
MODES = {'quick': ('quick.txt', 'quick-expected.txt', 5), 'full': ('measurements.txt', 'expected.txt', 3)}


def cached(source: Path) -> Path:
    """``source`` in memory, copied the first time; the source itself when there is no room."""
    target = Path('/dev/shm') / f'brc-{source.name}'
    try:
        if not target.exists() or target.stat().st_size != source.stat().st_size:
            partial = target.with_name(f'{target.name}.{os.getpid()}')
            shutil.copyfile(source, partial)
            partial.replace(target)
        return target
    except OSError:
        return source


def cpu() -> float:
    """The CPU time of the children that this process has reaped."""
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    return usage.ru_utime + usage.ru_stime


def run(data: Path, expected: bytes) -> tuple[float, float]:
    """CPU and wall seconds of one run. Raises ``RuntimeError`` when it fails or prints the wrong output."""
    before, started = cpu(), time.monotonic()
    try:
        proc = subprocess.run([str(BINARY), str(data)], capture_output=True, timeout=RUN_SECONDS)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f'a run took longer than {RUN_SECONDS} s') from e
    seconds, wall = cpu() - before, time.monotonic() - started
    if proc.returncode != 0:
        raise RuntimeError(f'the program exited {proc.returncode}: {proc.stderr.decode(errors="replace")[-2000:]}')
    if proc.stdout != expected:
        raise RuntimeError('the output is not the expected output')
    return seconds, wall


def main() -> int:
    """Build, run, and print the metrics as one JSON line, or the reason of a failure on stderr."""
    name, expected_name, runs = MODES['quick' if '--quick' in sys.argv else 'full']
    data_dir = ROOT / 'data'
    built = subprocess.run(
        ['cargo', 'build', '--release', '--offline', '--quiet'],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=BUILD_SECONDS,
    )
    if built.returncode != 0:
        print(f'cargo build failed:\n{built.stderr[-4000:]}', file=sys.stderr)
        return 1
    data, expected = cached(data_dir / name), (data_dir / expected_name).read_bytes()
    try:
        timings = [run(data, expected) for _ in range(runs)]
    except RuntimeError as e:
        print(e, file=sys.stderr)
        return 1
    metrics = {
        'cpu_seconds': round(statistics.median(c for c, _ in timings), 4),
        'wall_seconds': round(statistics.median(w for _, w in timings), 4),
    }
    line = json.dumps({'metrics': metrics})
    if path := os.environ.get('PILOOP_RESULT_FILE'):
        with open(path, 'a') as f:
            f.write(line + '\n')
    else:
        print(line)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
