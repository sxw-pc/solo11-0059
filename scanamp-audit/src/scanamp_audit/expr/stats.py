"""Statistics-possible-match tests and deterministic selectivity estimates."""
from __future__ import annotations
from typing import Any
from .intervals import clamp, intersects, numeric_overlap_fraction

Stats = dict[str, Any]
_OP_FLIP = {
    "<": ">",
    "<=": ">=",
    ">": "<",
    ">=": "<=",
    "=": "=",
    "!=": "!=",
}
def flip_op(op: str) -> str:
    return _OP_FLIP[op]
def has_rows(stats: Stats) -> bool:
    return int(stats.get("row_count", 0)) > 0
def all_null(stats: Stats) -> bool:
    null_count = stats.get("null_count")
    return (
        null_count is not None
        and has_rows(stats)
        and int(null_count) >= int(stats.get("row_count", 0))
    )
def non_null_possible(stats: Stats) -> bool:
    return has_rows(stats) and not all_null(stats)
def non_null_fraction(stats: Stats) -> float:
    rows = int(stats.get("row_count", 0))
    if rows <= 0:
        return 0.0
    null_count = stats.get("null_count")
    if null_count is None:
        return 1.0
    return clamp(1.0 - float(null_count) / float(rows))
def _bounds(stats: Stats) -> tuple[Any, Any] | None:
    low, high = stats.get("min"), stats.get("max")
    if low is None or high is None:
        return None
    return low, high
def eq_possible(stats: Stats, value: Any) -> bool | None:
    if not non_null_possible(stats):
        return False
    bounds = _bounds(stats)
    if bounds is None:
        return None
    return bounds[0] <= value <= bounds[1]
def neq_possible(stats: Stats, value: Any) -> bool | None:
    if not non_null_possible(stats):
        return False
    bounds = _bounds(stats)
    if bounds is None:
        return None
    return not (bounds[0] == bounds[1] == value)
def lt_possible(stats: Stats, value: Any) -> bool | None:
    if not non_null_possible(stats):
        return False
    bounds = _bounds(stats)
    return None if bounds is None else bounds[0] < value
def le_possible(stats: Stats, value: Any) -> bool | None:
    if not non_null_possible(stats):
        return False
    bounds = _bounds(stats)
    return None if bounds is None else bounds[0] <= value
def gt_possible(stats: Stats, value: Any) -> bool | None:
    if not non_null_possible(stats):
        return False
    bounds = _bounds(stats)
    return None if bounds is None else bounds[1] > value
def ge_possible(stats: Stats, value: Any) -> bool | None:
    if not non_null_possible(stats):
        return False
    bounds = _bounds(stats)
    return None if bounds is None else bounds[1] >= value
def in_possible(stats: Stats, values: list[Any]) -> bool | None:
    if not values or not non_null_possible(stats):
        return False
    bounds = _bounds(stats)
    if bounds is None:
        return None
    return any(bounds[0] <= value <= bounds[1] for value in values)
def not_in_possible(stats: Stats, values: list[Any]) -> bool | None:
    if not non_null_possible(stats):
        return False
    bounds = _bounds(stats)
    if bounds is None:
        return None
    if bounds[0] == bounds[1] and bounds[0] in values:
        return False
    return True
def between_possible(stats: Stats, low: Any, high: Any) -> bool | None:
    if not non_null_possible(stats):
        return False
    bounds = _bounds(stats)
    if bounds is None:
        return None
    return intersects(bounds[0], bounds[1], low, high)
def not_between_possible(stats: Stats, low: Any, high: Any) -> bool | None:
    if not non_null_possible(stats):
        return False
    bounds = _bounds(stats)
    if bounds is None:
        return None
    return bounds[0] < low or bounds[1] > high
def is_null_possible(stats: Stats) -> bool | None:
    if not has_rows(stats):
        return False
    null_count = stats.get("null_count")
    return None if null_count is None else int(null_count) > 0
def is_not_null_possible(stats: Stats) -> bool | None:
    if not has_rows(stats):
        return False
    null_count = stats.get("null_count")
    return None if null_count is None else int(null_count) < int(stats.get("row_count", 0))
# ---------------------------------------------------------------------------
# Selectivity estimates (used only for scan-amplification reporting)
# ---------------------------------------------------------------------------
def _eq_selectivity(stats: Stats, value: Any, default: float) -> float:
    possible = eq_possible(stats, value)
    if possible is False:
        return 0.0
    if possible is None:
        return _unknown(default, stats)
    distinct = stats.get("distinct_count")
    base = 1.0 / max(int(distinct), 1) if distinct is not None else default
    return clamp(base) * non_null_fraction(stats)
def _unknown(default: float, stats: Stats) -> float:
    return clamp(default) * non_null_fraction(stats)
def _range_selectivity(stats: Stats, op: str, value: Any, default: float) -> float:
    possible = {
        "<": lt_possible,
        "<=": le_possible,
        ">": gt_possible,
        ">=": ge_possible,
    }[op](stats, value)
    if possible is False:
        return 0.0
    if possible is None:
        return _unknown(default, stats)
    bounds = _bounds(stats)
    if bounds is None:
        return _unknown(default, stats)
    lo, hi = bounds
    if isinstance(op, str) and not isinstance(lo, (int, float)):
        base = default
    else:
        span = float(hi) - float(lo)
        if span <= 0:
            base = 1.0
        elif op in ("<", "<="):
            base = clamp((float(value) - float(lo)) / span)
        else:
            base = clamp((float(hi) - float(value)) / span)
        if base == 0.0 and possible:
            base = min(1.0, 1.0 / max(int(stats.get("row_count", 1)), 1) * 2)
    return clamp(base) * non_null_fraction(stats)
def leaf_selectivity(
    kind: str,
    stats: Stats,
    payload: dict[str, Any],
    default: float,
) -> float:
    """Estimate selectivity for one statistics-backed leaf predicate."""
    if kind == "cmp":
        op, value = payload["op"], payload["value"]
        if op == "=":
            return _eq_selectivity(stats, value, default)
        if op == "!=":
            return clamp(1.0 - _eq_selectivity(stats, value, default))
        return _range_selectivity(stats, op, value, default)
    if kind == "in":
        values = payload["values"]
        possible = not_in_possible(stats, values) if payload.get("negated") else in_possible(stats, values)
        if possible is False:
            return 0.0
        if possible is None:
            return _unknown(default, stats)
        distinct = stats.get("distinct_count")
        base = min(1.0, len(values) / max(int(distinct), 1)) if distinct else default
        if payload.get("negated"):
            base = 1.0 - base
        return clamp(base) * non_null_fraction(stats)
    if kind == "between":
        low, high = payload["low"], payload["high"]
        possible = (
            not_between_possible(stats, low, high)
            if payload.get("negated")
            else between_possible(stats, low, high)
        )
        if possible is False:
            return 0.0
        if possible is None:
            return _unknown(default, stats)
        bounds = _bounds(stats)
        fraction = None
        if bounds is not None:
            fraction = numeric_overlap_fraction(bounds[0], bounds[1], low, high)
        base = default if fraction is None else fraction
        if payload.get("negated"):
            base = 1.0 - base
        return clamp(base) * non_null_fraction(stats)
    if kind == "is_null":
        fraction = is_not_null_possible(stats) if payload.get("negated") else is_null_possible(stats)
        if fraction is False:
            return 0.0
        if fraction is None:
            return clamp(default)
        null_count = stats.get("null_count") or 0
        rows = max(int(stats.get("row_count", 0)), 1)
        base = float(null_count) / rows
        return clamp(1.0 - base) if payload.get("negated") else clamp(base)
    return clamp(default)
