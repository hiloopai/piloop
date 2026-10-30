# Circle packing: the largest sum of radii

Make `pack(n)` in `packing.py` place `n` circles in the unit square, without overlap, so that the sum of their radii
is as large as possible. It is scored at `n = 26`.

## Rules

- `pack(n)` returns `n` tuples `(x, y, r)` of floats. Every circle lies inside the unit square with `r > 0`, and no
  two circles overlap. The check is exact: each float is taken as the rational number it is, with no tolerance.
- `pack(n)` gets 60 s of CPU time.
- Use only the Python standard library, numpy and scipy. Do not edit `evaluate.py`.
- `pack` must work for any `n`: the final version is also run at other values of `n`, which you are not told.
  Coordinates written down for one `n` gain nothing there.

## Measure

`uv run python evaluate.py` prints `sum_radii`. Higher is better. The baseline, a square grid of equal circles, gives
2.1667. The best known value at `n = 26` is about 2.636.
