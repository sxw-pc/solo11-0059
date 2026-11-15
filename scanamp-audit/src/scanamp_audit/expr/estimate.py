"""Selectivity estimation for scan-amplification metrics.

Two estimators are provided:

* :func:`estimate_selectivity` returns the legacy point estimate, where a leaf
  on a column without statistics is replaced by ``default_selectivity``.
* :func:`estimate_selectivity_interval` returns a closed ``[low, high]``
  confidence interval. Statistics-backed leaves are known point values and
  therefore degenerate intervals ``[p, p]``; any leaf whose selectivity cannot
  be derived from statistics gets the conservative interval ``[0, 1]``.

Interval composition mirrors the point estimator's independence assumption:
AND multiplies bounds, OR combines complements, NOT takes the complement.
The interval never feeds a keep/drop decision; it only bounds reporting.
"""
from __future__ import annotations
from typing import Any
from .intervals import clamp
from .stats import flip_op, leaf_selectivity
from .evaluator import (
    TRUE, _coerce, _exact_compare, _resolve, evaluate, to_nnf,
)
def _estimate_cmp(
    node: dict[str, Any],
    resolver: dict[str, dict[str, Any]],
    column_types: dict[str, str],
    default: float,
) -> float:
    op = node["op"]
    left_kind, left = _resolve(node["left"], resolver, column_types)
    right_kind, right = _resolve(node["right"], resolver, column_types)
    if "unknown" in (left_kind, right_kind):
        return clamp(default)
    if left_kind == "literal" and right_kind in ("exact", "stats"):
        op = flip_op(op)
        left_kind, right_kind, left, right = right_kind, "literal", right, left
    if right_kind == "literal" and left_kind == "exact":
        ok, value = _coerce(right, left["type"])
        if not ok:
            return clamp(default)
        return 1.0 if _exact_compare(op, left["value"], value) == TRUE else 0.0
    if right_kind == "literal" and left_kind == "stats":
        ok, value = _coerce(right, left["type"])
        if not ok:
            return clamp(default)
        return leaf_selectivity("cmp", left, {"op": op, "value": value}, default)
    if left_kind == "exact" and right_kind == "exact":
        return 1.0 if _exact_compare(op, left["value"], right["value"]) == TRUE else 0.0
    if left_kind == "literal" and right_kind == "literal":
        return 1.0 if _exact_compare(op, left["value"], right["value"]) == TRUE else 0.0
    return clamp(default)
def _estimate_leaf(
    node: dict[str, Any],
    resolver: dict[str, dict[str, Any]],
    column_types: dict[str, str],
    default: float,
) -> float:
    kind = node["node"]
    if kind == "cmp":
        return _estimate_cmp(node, resolver, column_types, default)
    operand_kind, info = _resolve(node["operand"], resolver, column_types)
    if operand_kind == "unknown":
        return clamp(default)
    if operand_kind == "exact":
        result = evaluate(node, resolver, column_types)
        return 1.0 if result == TRUE else 0.0
    if operand_kind != "stats":
        return clamp(default)
    type_name = info.get("type", "string")
    if kind == "in":
        values = []
        for literal in node["values"]:
            ok, value = _coerce(literal, type_name)
            if not ok:
                return clamp(default)
            values.append(value)
        return leaf_selectivity(
            "in",
            info,
            {"values": values, "negated": bool(node.get("negated"))},
            default,
        )
    if kind == "between":
        ok_low, low = _coerce(node["low"], type_name)
        ok_high, high = _coerce(node["high"], type_name)
        if not (ok_low and ok_high):
            return clamp(default)
        return leaf_selectivity(
            "between",
            info,
            {"low": low, "high": high, "negated": bool(node.get("negated"))},
            default,
        )
    if kind == "is_null":
        return leaf_selectivity("is_null", info, node, default)
    return clamp(default)
def estimate_selectivity(
    node: dict[str, Any] | None,
    resolver: dict[str, dict[str, Any]],
    column_types: dict[str, str],
    default: float = 0.5,
) -> float:
    if node is None:
        return 1.0
    normalized = to_nnf(node)
    kind = normalized["node"]
    if kind == "and":
        value = 1.0
        for child in normalized["children"]:
            value *= estimate_selectivity(child, resolver, column_types, default)
        return clamp(value)
    if kind == "or":
        remaining = 1.0
        for child in normalized["children"]:
            remaining *= 1.0 - estimate_selectivity(child, resolver, column_types, default)
        return clamp(1.0 - remaining)
    return clamp(_estimate_leaf(normalized, resolver, column_types, default))


# ---------------------------------------------------------------------------
# Selectivity interval estimation (selective trust bounds)
# ---------------------------------------------------------------------------
# A selectivity interval is a closed ``[low, high]`` pair with
# ``0 <= low <= high <= 1``.
WIDEST_INTERVAL = (0.0, 1.0)


def _point_interval(value: float) -> tuple[float, float]:
    point = clamp(value)
    return point, point


def _and_intervals(intervals: list[tuple[float, float]]) -> tuple[float, float]:
    """AND of independent predicates: multiply the matching bounds."""
    low = high = 1.0
    for child_low, child_high in intervals:
        low *= child_low
        high *= child_high
    return clamp(low), clamp(high)


def _or_intervals(intervals: list[tuple[float, float]]) -> tuple[float, float]:
    """OR via complements: 1 - product of the per-branch non-match bounds."""
    remaining_low = remaining_high = 1.0
    for child_low, child_high in intervals:
        remaining_low *= 1.0 - child_high
        remaining_high *= 1.0 - child_low
    return clamp(1.0 - remaining_high), clamp(1.0 - remaining_low)


def _not_interval(interval: tuple[float, float]) -> tuple[float, float]:
    """Complement of an interval, clamped back onto [0, 1]."""
    low, high = interval
    return clamp(1.0 - high), clamp(1.0 - low)


def _estimate_cmp_interval(
    node: dict[str, Any],
    resolver: dict[str, dict[str, Any]],
    column_types: dict[str, str],
    default: float,
) -> tuple[float, float]:
    op = node["op"]
    left_kind, left = _resolve(node["left"], resolver, column_types)
    right_kind, right = _resolve(node["right"], resolver, column_types)
    # A column without usable statistics can match anywhere in the file.
    if "unknown" in (left_kind, right_kind):
        return WIDEST_INTERVAL
    if left_kind == "literal" and right_kind in ("exact", "stats"):
        op = flip_op(op)
        left_kind, right_kind, left, right = right_kind, "literal", right, left
    if right_kind == "literal" and left_kind == "exact":
        ok, value = _coerce(right, left["type"])
        if not ok:
            return WIDEST_INTERVAL
        point = 1.0 if _exact_compare(op, left["value"], value) == TRUE else 0.0
        return _point_interval(point)
    if right_kind == "literal" and left_kind == "stats":
        ok, value = _coerce(right, left["type"])
        if not ok:
            return WIDEST_INTERVAL
        return _point_interval(
            leaf_selectivity("cmp", left, {"op": op, "value": value}, default)
        )
    if left_kind in ("exact", "literal") and right_kind in ("exact", "literal"):
        point = 1.0 if _exact_compare(op, left["value"], right["value"]) == TRUE else 0.0
        return _point_interval(point)
    return WIDEST_INTERVAL


def _estimate_leaf_interval(
    node: dict[str, Any],
    resolver: dict[str, dict[str, Any]],
    column_types: dict[str, str],
    default: float,
) -> tuple[float, float]:
    kind = node["node"]
    if kind == "cmp":
        return _estimate_cmp_interval(node, resolver, column_types, default)
    operand_kind, info = _resolve(node["operand"], resolver, column_types)
    if operand_kind == "unknown":
        return WIDEST_INTERVAL
    if operand_kind == "exact":
        result = evaluate(node, resolver, column_types)
        return _point_interval(1.0 if result == TRUE else 0.0)
    if operand_kind != "stats":
        return WIDEST_INTERVAL
    type_name = info.get("type", "string")
    if kind == "in":
        values = []
        for literal in node["values"]:
            ok, value = _coerce(literal, type_name)
            if not ok:
                return WIDEST_INTERVAL
            values.append(value)
        point = leaf_selectivity(
            "in",
            info,
            {"values": values, "negated": bool(node.get("negated"))},
            default,
        )
        return _point_interval(point)
    if kind == "between":
        ok_low, low = _coerce(node["low"], type_name)
        ok_high, high = _coerce(node["high"], type_name)
        if not (ok_low and ok_high):
            return WIDEST_INTERVAL
        point = leaf_selectivity(
            "between",
            info,
            {"low": low, "high": high, "negated": bool(node.get("negated"))},
            default,
        )
        return _point_interval(point)
    if kind == "is_null":
        point = leaf_selectivity("is_null", info, node, default)
        return _point_interval(point)
    return WIDEST_INTERVAL


def estimate_selectivity_interval(
    node: dict[str, Any] | None,
    resolver: dict[str, dict[str, Any]],
    column_types: dict[str, str],
    default: float = 0.5,
) -> tuple[float, float]:
    """Return the conservative ``[low, high]`` selectivity interval.

    Known-statistics leaves collapse to a degenerate ``[p, p]`` interval;
    leaves depending on a column without statistics stay fully open at
    ``[0, 1]``. AND/OR/NOT compose the bounds with the same independence
    assumption as the point estimator. The result always satisfies
    ``0 <= low <= high <= 1``.
    """
    if node is None:
        return 1.0, 1.0
    kind = node["node"]
    if kind == "not":
        return _not_interval(
            estimate_selectivity_interval(node["child"], resolver, column_types, default)
        )
    normalized = to_nnf(node)
    kind = normalized["node"]
    if kind == "and":
        return _and_intervals([
            estimate_selectivity_interval(child, resolver, column_types, default)
            for child in normalized["children"]
        ])
    if kind == "or":
        return _or_intervals([
            estimate_selectivity_interval(child, resolver, column_types, default)
            for child in normalized["children"]
        ])
    low, high = _estimate_leaf_interval(normalized, resolver, column_types, default)
    return clamp(low), clamp(high)
