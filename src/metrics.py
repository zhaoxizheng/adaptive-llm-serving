from __future__ import annotations

import math


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    if not 0 <= fraction <= 1:
        raise ValueError("fraction must be between 0 and 1")
    ordered = sorted(values)
    index = min(len(ordered) - 1, math.ceil(fraction * len(ordered)) - 1)
    return ordered[max(index, 0)]

