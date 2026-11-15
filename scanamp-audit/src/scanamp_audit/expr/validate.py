"""Schema-aware validation and static analysis of parsed predicates."""
from __future__ import annotations
from typing import Any
from ..errors import PredicateError
from ..schema import coerce_literal

def _operand_columns(operand: dict[str, Any]) -> list[str]:
    kind = operand.get("kind")
    if kind == "column":
        return [operand["name"]]
    if kind == "function":
        columns: list[str] = []
        for arg in operand.get("args", []):
            columns.extend(_operand_columns(arg))
        return columns
    return []
def _function_pairs(operand: dict[str, Any]) -> list[tuple[str, list[str]]]:
    pairs: list[tuple[str, list[str]]] = []
    if operand.get("kind") == "function":
        pairs.append((operand["name"], _operand_columns(operand)))
        for arg in operand.get("args", []):
            pairs.extend(_function_pairs(arg))
    return pairs
def _walk(node: dict[str, Any]):
    yield node
    if node["node"] == "not":
        yield from _walk(node["child"])
    elif node["node"] in ("and", "or"):
        for child in node["children"]:
            yield from _walk(child)
def _leaf_operands(node: dict[str, Any]) -> list[dict[str, Any]]:
    kind = node["node"]
    if kind == "cmp":
        return [node["left"], node["right"]]
    if kind in ("in", "is_null", "between"):
        return [node["operand"]]
    return []
def _validate_literal(literal: dict[str, Any], type_name: str, query_id: str | None) -> None:
    try:
        coerce_literal(literal, type_name)
    except ValueError as exc:
        raise PredicateError(
            str(exc),
            location=f"query:{query_id}" if query_id else "where",
            field="where",
            reason="TYPE_MISMATCH",
            details={"column_type": type_name, "literal": literal.get("value")},
        ) from exc
def _validate_node(node: dict[str, Any], column_types: dict[str, str], query_id: str | None) -> None:
    for operand in _leaf_operands(node):
        for column in _operand_columns(operand):
            if column not in column_types:
                raise PredicateError(
                    f"unknown column {column!r}",
                    location=f"query:{query_id}" if query_id else "where",
                    field="where",
                    reason="UNKNOWN_COLUMN",
                    details={"column": column, "known_columns": sorted(column_types)},
                )
    kind = node["node"]
    if kind in ("cmp", "in", "between"):
        operand = node["left"] if kind == "cmp" else node["operand"]
        literals = (
            [node["right"]] if kind == "cmp" else node.get("values") or [node["low"], node["high"]]
        )
        function_involved = operand.get("kind") == "function" or any(
            item.get("kind") == "function" for item in literals
        )
        if function_involved:
            return
        if kind == "cmp" and operand.get("kind") == "column" and node["right"].get("kind") == "column":
            left_type = column_types[operand["name"]]
            right_type = column_types[node["right"]["name"]]
            if left_type != right_type:
                raise PredicateError(
                    "column types differ in comparison",
                    location=f"query:{query_id}" if query_id else "where",
                    field="where",
                    reason="TYPE_MISMATCH",
                    details={"left": left_type, "right": right_type},
                )
            return
        if literals and literals[0].get("kind") == "column":
            if kind == "cmp" and operand.get("kind") == "literal":
                column = node["right"]["name"]
                _validate_literal(operand, column_types[column], query_id)
            return
        if operand.get("kind") == "column":
            type_name = column_types[operand["name"]]
            for literal in literals:
                if literal.get("kind") == "literal" and literal.get("literal_type") != "null":
                    _validate_literal(literal, type_name, query_id)
    elif kind == "is_null":
        if node["operand"].get("kind") == "literal":
            raise PredicateError(
                "IS NULL requires a column or function operand",
                location=f"query:{query_id}" if query_id else "where",
                field="where",
                reason="INVALID_IS_NULL",
            )
def validate_predicate(
    node: dict[str, Any] | None,
    column_types: dict[str, str],
    query_id: str | None = None,
) -> None:
    if node is None:
        return
    for item in _walk(node):
        if item["node"] in ("cmp", "in", "is_null", "between"):
            _validate_node(item, column_types, query_id)
def analyze_predicate(
    node: dict[str, Any] | None,
    column_types: dict[str, str],
    partition_columns: list[str],
    query_id: str | None = None,
) -> dict[str, Any]:
    if node is None:
        return {
            "columns": [],
            "partition_columns": [],
            "function_wrapped_partition_columns": [],
            "undecidable_reasons": [],
        }
    columns: set[str] = set()
    function_columns: dict[tuple[str, str], None] = {}
    reasons: set[str] = set()
    partition_set = set(partition_columns)
    for item in _walk(node):
        if item["node"] not in ("cmp", "in", "is_null", "between"):
            continue
        leaf_columns: set[str] = set()
        for operand in _leaf_operands(item):
            leaf_columns.update(_operand_columns(operand))
            for function_name, names in _function_pairs(operand):
                for name in names:
                    if name in partition_set:
                        function_columns[(name, function_name)] = None
                        reasons.add(f"FUNCTION_WRAPPED_PARTITION:{function_name}({name})")
        columns.update(leaf_columns)
        if leaf_columns & partition_set and leaf_columns - partition_set:
            reasons.add(
                "PARTITION_COLUMN_COMPARED_WITH_DATA:"
                + ",".join(sorted(leaf_columns & partition_set))
                + ":"
                + ",".join(sorted(leaf_columns - partition_set))
            )
    wrapped = [
        {"column": column, "function": function_name}
        for column, function_name in sorted(function_columns)
    ]
    return {
        "columns": sorted(columns),
        "partition_columns": sorted(columns & partition_set),
        "function_wrapped_partition_columns": wrapped,
        "undecidable_reasons": sorted(reasons),
    }
