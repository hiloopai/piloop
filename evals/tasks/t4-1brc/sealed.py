"""t4-1brc's sealed score: the change's CPU time over the starting program's, measured in turn on the same machine.

Run as ``python sealed.py <tree> <base> <sealed> <out>``, without network. ``<sealed>/meta.json`` holds the parameters
of the sealed file (``rows``, ``stations``, ``long``, ``seed``) and the sha256 of its expected output. The file is
generated into ``<sealed>`` once and kept there. Both programs are built offline in copies, then, after one uncounted run each, timed in turn, five
runs each; every run must print the expected output. The score is ``2 * (1 - ratio)``: 0 at the starting program, 1
at half its CPU time. ``raw`` is that ratio, lower being better: a campaign's sealed split compares it across
versions measured at different times, and pairing each with the starting program cancels the machine's drift.
"""

import hashlib
import importlib.util
import json
import resource
import shutil
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path

RUNS = 5
RUN_SECONDS = 120
BUILD_SECONDS = 600
GEN = Path(__file__).parent / 'build' / 'gen.py'


def data(sealed: Path) -> tuple[Path, bytes]:
    """The sealed file and its expected output, generated on the first call."""
    meta = json.loads((sealed / 'meta.json').read_text())
    measurements, expected = sealed / 'measurements.txt', sealed / 'expected.txt'
    if not expected.exists():
        spec = importlib.util.spec_from_file_location('gen', GEN)
        assert spec is not None and spec.loader is not None
        gen = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gen)
        partial = sealed / 'expected.partial'
        gen.generate(meta['rows'], meta['stations'], bool(meta['long']), meta['seed'], measurements, partial)
        partial.replace(expected)
    out = expected.read_bytes()
    if hashlib.sha256(out).hexdigest() != meta['expected_sha256']:
        raise SystemExit('the sealed file is not the one of meta.json: the generator changed')
    return measurements, out


def build(source: Path, into: Path) -> Path | str:
    """A release build of a copy of ``source``: its binary, or why it failed."""
    shutil.copytree(source, into, ignore=shutil.ignore_patterns('target', 'data', '.git', '.venv'))
    done = subprocess.run(
        ['cargo', 'build', '--release', '--offline', '--quiet'],
        cwd=into,
        capture_output=True,
        text=True,
        timeout=BUILD_SECONDS,
    )
    return into / 'target' / 'release' / 'brc' if done.returncode == 0 else done.stderr[-2000:]


def cpu() -> float:
    """The CPU time of the children that this process has reaped."""
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    return usage.ru_utime + usage.ru_stime


def timed(binary: Path, measurements: Path, expected: bytes) -> float | str:
    """CPU seconds of one run, or why it failed."""
    before = cpu()
    try:
        proc = subprocess.run([str(binary), str(measurements)], capture_output=True, timeout=RUN_SECONDS)
    except subprocess.TimeoutExpired:
        return f'a run took longer than {RUN_SECONDS} s'
    seconds = cpu() - before
    if proc.returncode != 0:
        return f'exited {proc.returncode}: {proc.stderr.decode(errors="replace")[-500:]}'
    return seconds if proc.stdout == expected else 'the output is not the expected output'


def main(tree: Path, base: Path, sealed: Path) -> dict[str, object]:
    """Score ``tree`` against ``base`` on the sealed file."""
    measurements, expected = data(sealed)
    with tempfile.TemporaryDirectory() as tmp:
        mine, theirs = build(tree, Path(tmp) / 'tree'), build(base, Path(tmp) / 'base')
        if isinstance(theirs, str):
            raise SystemExit(f'the starting program does not build: {theirs}')
        if isinstance(mine, str):
            return {'score': None, 'status': 'crash', 'detail': f'build: {mine}', 'raw': None}
        times: dict[str, list[float]] = {'tree': [], 'base': []}
        for binary in (mine, theirs):  # a first run of each warms the page cache and the CPU: not counted
            timed(binary, measurements, expected)
        for _ in range(RUNS):
            for key, binary in (('tree', mine), ('base', theirs)):
                t = timed(binary, measurements, expected)
                if isinstance(t, str):
                    if key == 'base':
                        raise SystemExit(f'the starting program failed: {t}')
                    return {'score': None, 'status': 'crash', 'detail': t, 'raw': None}
                times[key].append(t)
    raw, reference = statistics.median(times['tree']), statistics.median(times['base'])
    ratio = raw / reference
    detail = f'{raw:.3f} s against {reference:.3f} s for the starting program'
    return {'score': 2 * (1 - ratio), 'status': 'ok', 'detail': detail, 'raw': ratio}


if __name__ == '__main__':
    Path(sys.argv[4]).write_text(json.dumps(main(*(Path(a) for a in sys.argv[1:4]))))
