"""Three-valued predicate evaluation and selectivity estimation.

Leaf evaluation uses *possible-match* semantics: ``FALSE`` means the file or
partition provably cannot match, ``TRUE`` means a match is possible, and
``UNKNOWN`` means statistics are insufficient. Therefore only ``FALSE`` may
ever be used to prune. Negation is eliminated into negation-normal form before
evaluation so a ``NOT`` never turns an unknown into a false drop.
"""
from __future__ import annotations
from typing import Any
from ..schema import coerce_literal
from .intervals import clamp
from .stats import (
    between_possible,
    eq_possible,
    flip_op,
    ge_possible,
    gt_possible,
    in_possible,
    is_not_null_possible,
    is_null_possible,
    leaf_selectivity,
    le_possible,
    lt_possible,
    neq_possible,
    not_between_possible,
    not_in_possible,
)
TRUE = "true"
FALSE = "false"
UNKNOWN = "unknown"
_NEG_CMP = {
    "=": "!=",
    "!=": "=",
    "<": ">=",
    "<=": ">",
    ">": "<=",
    ">=": "<",
}
def to_nnf(node: dict[str, Any] | None, negate: bool = False) -> dict[str, Any] | None:
    if node is None:
        return None
    kind = node["node"]
    if kind == "not":
        return to_nnf(node["child"], not negate)
    if kind in ("and", "or"):
        combined = kind
        if negate:
            combined = "or" if kind == "and" else "and"
        return {
            "node": combined,
            "children": [to_nnf(child, negate) for child in node["children"]],
        }
    if kind == "cmp":
        return {
            "node": "cmp",
            "op": _NEG_CMP[node["op"]] if negate else node["op"],
            "left": node["left"],
            "right": node["right"],
        }
    if kind == "in":
        return {**node, "negated": bool(node.get("negated")) != negate}
    if kind == "between":
        return {**node, "negated": bool(node.get("negated")) != negate}
    if kind == "is_null":
        return {**node, "negated": bool(node.get("negated")) != negate}
    raise ValueError(f"unknown predicate node: {kind!r}")
def _and(values: list[str]) -> str:
    if FALSE in values:
        return FALSE
    if all(value == TRUE for value in values):
        return TRUE
    return UNKNOWN
def _or(values: list[str]) -> str:
    if TRUE in values:
        return TRUE
    if all(value == FALSE for value in values):
        return FALSE
    return UNKNOWN
def _resolve(
    operand: dict[str, Any],
    resolver: dict[str, dict[str, Any]],
    column_types: dict[str, str],
) -> tuple[str, Any]:
    kind = operand.get("kind")
    if kind == "literal":
        return "literal", operand
    if kind == "column":
        info = resolver.get(operand["name"])
        if info is None:
            return "unknown", None
        resolved = dict(info)
        resolved.setdefault("type", column_types.get(operand["name"], "string"))
        if resolved.get("kind") == "stats":
            return "stats", resolved
        if resolved.get("kind") == "exact":
            return "exact", resolved
        return "unknown", None
    return "unknown", None
def _exact_compare(op: str, left: Any, right: Any) -> str:
    try:
        if op == "=":
            result = left == right
        elif op == "!=":
            result = left != right
        elif op == "<":
            result = left < right
        elif op == "<=":
            result = left <= right
        elif op == ">":
            result = left > right
        else:
            result = left >= right
    except TypeError:
        return UNKNOWN
    return TRUE if result else FALSE
def _coerce(literal: dict[str, Any], type_name: str) -> tuple[bool, Any]:
    try:
        return True, coerce_literal(literal, type_name)
    except ValueError:
        return False, None
def _stats_cmp(op: str, stats: dict[str, Any], value: Any) -> str:
    function = {
        "=": eq_possible,
        "!=": neq_possible,
        "<": lt_possible,
        "<=": le_possible,
        ">": gt_possible,
        ">=": ge_possible,
    }.get(op)
    if function is None:
        return UNKNOWN
    result = function(stats, value)
    if result is None:
        return UNKNOWN
    return TRUE if result else FALSE
def _eval_cmp(
    node: dict[str, Any],
    resolver: dict[str, dict[str, Any]],
    column_types: dict[str, str],
) -> str:
    op = node["op"]
    left_kind, left = _resolve(node["left"], resolver, column_types)
    right_kind, right = _resolve(node["right"], resolver, column_types)
    if "unknown" in (left_kind, right_kind):
        return UNKNOWN
    if left_kind == "literal" and right_kind == "literal":
        return _exact_compare(op, left["value"], right["value"])
    if left_kind == "exact" and right_kind == "exact":
        return _exact_compare(op, left["value"], right["value"])
    if left_kind == "literal" and right_kind in ("exact", "stats"):
        op = flip_op(op)
        left_kind, right_kind, left, right = right_kind, "literal", right, left
    if right_kind == "literal" and left_kind == "exact":
        ok, value = _coerce(right, left["type"])
        return UNKNOWN if not ok else _exact_compare(op, left["value"], value)
    if right_kind == "literal" and left_kind == "stats":
        ok, value = _coerce(right, left["type"])
        return UNKNOWN if not ok else _stats_cmp(op, left, value)
    return UNKNOWN
def _eval_in(
    node: dict[str, Any],
    resolver: dict[str, dict[str, Any]],
    column_types: dict[str, str],
) -> str:
    kind, info = _resolve(node["operand"], resolver, column_types)
    if kind not in ("exact", "stats", "literal"):
        return UNKNOWN
    type_name = info.get("type", "string") if kind != "literal" else None
    values: list[Any] = []
    for literal in node["values"]:
        if type_name is None:
            values.append(literal["value"])
            continue
        ok, value = _coerce(literal, type_name)
        if not ok:
            return UNKNOWN
        values.append(value)
    negated = bool(node.get("negated"))
    if kind == "exact":
        result = info["value"] in values
        return TRUE if result != negated else FALSE
    if kind == "literal":
        result = info["value"] in values
        return TRUE if result != negated else FALSE
    result = not_in_possible(info, values) if negated else in_possible(info, values)
    if result is None:
        return UNKNOWN
    return TRUE if result else FALSE
def _eval_between(
    node: dict[str, Any],
    resolver: dict[str, dict[str, Any]],
    column_types: dict[str, str],
) -> str:
    kind, info = _resolve(node["operand"], resolver, column_types)
    if kind not in ("exact", "stats", "literal"):
        return UNKNOWN
    type_name = info.get("type", "string") if kind != "literal" else None
    if type_name is None:
        try:
            low, high = node["low"]["value"], node["high"]["value"]
        except (KeyError, TypeError):
            return UNKNOWN
    else:
        ok_low, low = _coerce(node["low"], type_name)
        ok_high, high = _coerce(node["high"], type_name)
        if not (ok_low and ok_high):
            return UNKNOWN
    negated = bool(node.get("negated"))
    if kind in ("exact", "literal"):
        value = info["value"]
        matched = low <= value <= high
        return TRUE if matched != negated else FALSE
    result = (
        not_between_possible(info, low, high)
        if negated
        else between_possible(info, low, high)
    )
    if result is None:
        return UNKNOWN
    return TRUE if result else FALSE
def _eval_is_null(
    node: dict[str, Any],
    resolver: dict[str, dict[str, Any]],
    column_types: dict[str, str],
) -> str:
    kind, info = _resolve(node["operand"], resolver, column_types)
    negated = bool(node.get("negated"))
    if kind == "exact":
        matched = info["value"] is None
        return TRUE if matched != negated else FALSE
    if kind == "stats":
        result = is_not_null_possible(info) if negated else is_null_possible(info)
        if result is None:
            return UNKNOWN
        return TRUE if result else FALSE
    return UNKNOWN
def evaluate(
    node: dict[str, Any] | None,
    resolver: dict[str, dict[str, Any]],
    column_types: dict[str, str],
) -> str:
    """Evaluate a predicate with possible-match three-valued semantics."""
    if node is None:
        return TRUE
    normalized = to_nnf(node)
    kind = normalized["node"]
    if kind == "and":
        return _and([evaluate(child, resolver, column_types) for child in normalized["children"]])
    if kind == "or":
        return _or([evaluate(child, resolver, column_types) for child in normalized["children"]])
    if kind == "cmp":
        return _eval_cmp(normalized, resolver, column_types)
    if kind == "in":
        return _eval_in(normalized, resolver, column_types)
    if kind == "between":
        return _eval_between(normalized, resolver, column_types)
    if kind == "is_null":
        return _eval_is_null(normalized, resolver, column_types)
    return UNKNOWN
