"""Closed-interval helpers used by statistics pruning and estimation."""
from __future__ import annotations
from datetime import date
from typing import Any

def intersects(a_min: Any, a_max: Any, b_min: Any, b_max: Any) -> bool:
    """True when two closed intervals share at least one value."""
    return a_min <= b_max and b_min <= a_max
def contains(lo: Any, hi: Any, value: Any) -> bool:
    return lo <= value <= hi
def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))
def overlap_fraction(lo: Any, hi: Any, vmin: Any, vmax: Any) -> float:
    """Fraction of [lo, hi] covered by [vmin, vmax].

    Intended for numeric spans. Dates must be converted with
    :func:`sortable_number` first; strings have no meaningful arithmetic span.
    """
    if not intersects(lo, hi, vmin, vmax):
        return 0.0
    span = hi - lo
    if span <= 0:
        return 1.0 if vmin <= lo <= vmax else 0.0
    left = max(lo, vmin)
    right = min(hi, vmax)
    if right < left:
        return 0.0
    return clamp(float(right - left) / float(span))
def sortable_number(value: Any) -> float | None:
    """Convert an int/float/ISO date string to a sortable number."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(date.fromisoformat(value).toordinal())
        except ValueError:
            return None
    return None
def numeric_overlap_fraction(
    lo: Any, hi: Any, vmin: Any, vmax: Any
) -> float | None:
    left, right, low, high = (
        sortable_number(lo),
        sortable_number(hi),
        sortable_number(vmin),
        sortable_number(vmax),
    )
    if None in (left, right, low, high):
        return None
    return overlap_fraction(left, right, low, high)
def merge_min(values: list[Any]) -> Any:
    return min(values)
def merge_max(values: list[Any]) -> Any:
    return max(values)
