"""Baseline: equal circles on the smallest square grid that holds n of them."""

import math

GAP = 1e-9
"""How much each radius gives up, so that rounding never makes two touching circles overlap."""


def pack(n: int) -> list[tuple[float, float, float]]:
    """``n`` circles ``(x, y, r)`` inside the unit square that do not overlap, with the largest sum of radii."""
    k = math.ceil(math.sqrt(n))
    r = (1 - GAP) / (2 * k)
    return [((i % k + 0.5) / k, (i // k + 0.5) / k, r) for i in range(n)]
