"""Write a seeded 1BRC measurements file and its expected output, with only the standard library.

Each line is ``<station>;<temperature>``: a station of the set, drawn uniformly, and its mean plus an offset from a
normal distribution truncated to ±45.0, in tenths. The expected output is one ``name=min/mean/max`` line per station,
sorted by the bytes of the name, the mean rounded half up to a tenth. The same parameters give the same bytes.

Usage: ``python3 gen.py <rows> <stations> <long names: 0 or 1> <seed> <measurements file> <expected file>``.
"""

import random
import sys
from collections import Counter
from pathlib import Path

CHUNK = 1_000_000
SPREAD = 450
"""Offsets go from -45.0 to 45.0; means from -30.0 to 40.0, so no temperature reaches ±99.9."""
SYLLABLES = ['ka', 'lo', 'ma', 'ri', 'sun', 'te', 'vo', 'ba', 'nel', 'dor', 'shi', 'an', 'qu', 'ze', 'por', 'el']
LETTERS = 'aäåbcçdeéèfghiíjklmnñoöøpqrsßtuüvwxyzабвгдежзийклмнопрстуфхцчшщэюяαβγδεζηθλμξπστφχψωあいうえおかきくけこ'


def names(rng: random.Random, n: int, long: bool) -> list[bytes]:
    """``n`` distinct station names: short ASCII ones, or long UTF-8 ones of up to 100 bytes."""
    found: dict[bytes, None] = {}
    while len(found) < n:
        if long:
            text = ''.join(rng.choice(LETTERS) for _ in range(rng.randint(3, 40))).capitalize()
            name = text.encode()[:100].decode(errors='ignore').encode()
        else:
            name = ''.join(rng.choice(SYLLABLES) for _ in range(rng.randint(2, 5))).capitalize().encode()
        if name and b';' not in name and b'\n' not in name:
            found[name] = None
    return list(found)


def tenths(t: int) -> str:
    """``t`` tenths as text: ``-1.5``, ``0.0``."""
    return f'{"-" if t < 0 else ""}{abs(t) // 10}.{abs(t) % 10}'


def generate(rows: int, stations: int, long: bool, seed: int, measurements: Path, expected: Path) -> None:
    """Write the file and its expected output."""
    rng = random.Random(seed)
    station_names = names(rng, stations, long)
    means = [rng.randint(-300, 400) for _ in station_names]
    prefix = [name + b';' for name in station_names]
    lowest = min(means) - SPREAD
    text = [f'{tenths(t)}\n'.encode() for t in range(lowest, max(means) + SPREAD + 1)]
    offsets = list(range(-SPREAD, SPREAD + 1))
    weights = [2.718281828 ** (-((o / 100) ** 2) / 2) for o in offsets]
    stats: dict[int, list[int]] = {}
    with measurements.open('wb') as f:
        for done in range(0, rows, CHUNK):
            k = min(CHUNK, rows - done)
            picks = rng.choices(range(stations), k=k)
            temps = [means[s] + o - lowest for s, o in zip(picks, rng.choices(offsets, weights, k=k), strict=True)]
            f.write(b''.join([prefix[s] + text[t] for s, t in zip(picks, temps, strict=True)]))
            for (s, t), c in Counter(zip(picks, temps, strict=True)).items():
                v = t + lowest
                st = stats.setdefault(s, [v, v, 0, 0])
                st[0], st[1], st[2], st[3] = min(st[0], v), max(st[1], v), st[2] + c * v, st[3] + c
    lines: list[tuple[bytes, bytes]] = []
    for s, (lo, hi, total, count) in stats.items():
        mean = (2 * total + count) // (2 * count)
        lines.append((station_names[s], f'={tenths(lo)}/{tenths(mean)}/{tenths(hi)}\n'.encode()))
    expected.write_bytes(b''.join(name + rest for name, rest in sorted(lines)))


if __name__ == '__main__':
    rows, stations, long, seed = (int(a) for a in sys.argv[1:5])
    generate(rows, stations, bool(long), seed, Path(sys.argv[5]), Path(sys.argv[6]))
