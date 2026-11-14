"""Selectivity estimation for scan-amplification metrics."""
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
