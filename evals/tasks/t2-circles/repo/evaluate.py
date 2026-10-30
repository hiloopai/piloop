"""Score ``packing.pack(26)`` and print ``sum_radii``, checked exactly.

``pack`` runs in its own process, limited to 60 s of CPU. The check takes each float as the exact rational number it
is: every circle has a positive radius and lies inside the unit square, and no two circles overlap. There is no
tolerance. A packing that fails the check, or a failed run, exits non-zero with the reason.
"""

import json
import math
import os
import resource
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).parent
N = 26
CPU_SECONDS = 60
RUN = """
import json, sys
sys.path.insert(0, sys.argv[1])
import packing
print(json.dumps([[float(v) for v in c] for c in packing.pack(int(sys.argv[2]))]))
"""


def limit_cpu() -> None:
    """Limit the child's CPU time."""
    resource.setrlimit(resource.RLIMIT_CPU, (CPU_SECONDS, CPU_SECONDS + 1))


def check(circles: object, n: int) -> float:
    """The sum of the radii of ``circles``. Raises ``ValueError`` with the reason when they are not a valid packing."""
    if not isinstance(circles, list) or len(circles) != n:
        raise ValueError(f'pack({n}) must return {n} circles')
    exact = []
    for c in circles:
        if not isinstance(c, list) or len(c) != 3 or not all(isinstance(v, float) and math.isfinite(v) for v in c):
            raise ValueError(f'a circle is not three finite numbers: {c!r}')
        x, y, r = (Fraction(v) for v in c)
        if r <= 0 or x - r < 0 or y - r < 0 or x + r > 1 or y + r > 1:
            raise ValueError(f'circle {c} is not inside the unit square with a positive radius')
        exact.append((x, y, r))
    for i, (x1, y1, r1) in enumerate(exact):
        for j in range(i + 1, n):
            x2, y2, r2 = exact[j]
            if (x1 - x2) ** 2 + (y1 - y2) ** 2 < (r1 + r2) ** 2:
                raise ValueError(f'circles {i} and {j} overlap')
    return math.fsum(c[2] for c in circles)


def score(n: int) -> float:
    """The checked sum of radii of ``pack(n)``. Raises ``RuntimeError`` with the reason when it fails."""
    try:
        proc = subprocess.run(
            [sys.executable, '-c', RUN, str(ROOT), str(n)],
            capture_output=True,
            text=True,
            timeout=CPU_SECONDS * 3,
            preexec_fn=limit_cpu,
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f'pack({n}) took longer than {CPU_SECONDS * 3} s of wall time') from e
    if proc.returncode != 0:
        raise RuntimeError(f'pack({n}) failed with exit code {proc.returncode}\n{proc.stderr[-4000:]}')
    try:
        return check(json.loads(proc.stdout.strip().splitlines()[-1]), n)
    except (ValueError, IndexError) as e:
        raise RuntimeError(str(e)) from e


def main() -> int:
    """Print the metric as one JSON line, or the reason of the failure on stderr."""
    try:
        metrics = {'sum_radii': score(N)}
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
