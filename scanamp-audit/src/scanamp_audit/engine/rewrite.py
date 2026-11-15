"""Degraded-path rewrite suggestions for function-wrapped partition predicates.

When a query wraps a partition column in a function (``FUNCTION_WRAPPED_PARTITION``)
and the predicate is undecidable so every partition is conservatively kept
(``UNKNOWN_PREDICATE_CONSERVATIVE``), this module derives — for each rewritable
predicate fragment — an equivalent bare-column comparison from the table's
*actual* partition value domain, plus a conservative upper bound on the bytes
the rewritten query would scan.

The rewrite is derived per fragment by evaluating the original fragment
(including any ``NOT`` chain directly above it) against every distinct
partition value of the wrapped column.  Because the replacement is
value-equivalent to the original fragment for every value in the domain, it
stays valid under any surrounding AND/OR/NOT structure.  The byte bound
re-runs partition pruning with the rewritten predicate over the full table
and sums the bytes of the selected partitions *without* file-level pruning,
so it never under-reports: when the rewrite cannot prune anything, the bound
stays at the full table bytes, which is never smaller than the bytes the
original query scanned.
"""
from __future__ import annotations
from typing import Any
from ..models import TableManifest
from ..schema import canonical_json, literal_type_name, round_float
from .partition_pruner import prune_partitions

TRIGGER_RULES = ("FUNCTION_WRAPPED_PARTITION", "UNKNOWN_PREDICATE_CONSERVATIVE")
_SUPPORTED_FUNCTIONS = ("lower", "substr", "upper")
_EMPTY_ANALYSIS: dict[str, Any] = {"undecidable_reasons": [], "partition_columns": []}

# ---------------------------------------------------------------------------
# Predicate rendering (deterministic, re-parseable)
# ---------------------------------------------------------------------------
def _render_literal(literal: dict[str, Any]) -> str:
    value = literal.get("value")
    literal_type = literal.get("literal_type")
    if literal_type == "string":
        return "'" + str(value).replace("'", "''") + "'"
    if literal_type == "boolean":
        return "TRUE" if value else "FALSE"
    if literal_type == "null":
        return "NULL"
    return str(value)
def _render_operand(operand: dict[str, Any]) -> str:
    kind = operand.get("kind")
    if kind == "column":
        return str(operand["name"])
    if kind == "literal":
        return _render_literal(operand)
    if kind == "function":
        args = ", ".join(_render_operand(arg) for arg in operand.get("args", []))
        return f"{operand['name']}({args})"
    raise ValueError(f"unknown operand kind: {kind!r}")
def _render_leaf(node: dict[str, Any]) -> str:
    kind = node["node"]
    if kind == "cmp":
        return f"{_render_operand(node['left'])} {node['op']} {_render_operand(node['right'])}"
    if kind == "in":
        values = ", ".join(_render_literal(item) for item in node["values"])
        negated = "NOT " if node.get("negated") else ""
        return f"{_render_operand(node['operand'])} {negated}IN ({values})"
    if kind == "between":
        negated = "NOT " if node.get("negated") else ""
        return (
            f"{_render_operand(node['operand'])} {negated}BETWEEN "
            f"{_render_literal(node['low'])} AND {_render_literal(node['high'])}"
        )
    if kind == "is_null":
        negated = "NOT " if node.get("negated") else ""
        return f"{_render_operand(node['operand'])} IS {negated}NULL"
    raise ValueError(f"unknown predicate node: {kind!r}")
def _render(node: dict[str, Any], min_precedence: int) -> str:
    kind = node["node"]
    if kind == "or":
        text, precedence = " OR ".join(_render(c, 1) for c in node["children"]), 1
    elif kind == "and":
        text, precedence = " AND ".join(_render(c, 2) for c in node["children"]), 2
    elif kind == "not":
        text, precedence = "NOT " + _render(node["child"], 3), 3
    else:
        return _render_leaf(node)
    return f"({text})" if precedence < min_precedence else text
def render_predicate(node: dict[str, Any]) -> str:
    """Render a predicate AST as deterministic, re-parseable WHERE text."""
    return _render(node, 0)

# ---------------------------------------------------------------------------
# Exact two-valued evaluation of one fragment against one partition value
# ---------------------------------------------------------------------------
def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)
def _apply_function(name: str, args: list[Any]) -> tuple[bool, Any]:
    if name not in _SUPPORTED_FUNCTIONS:
        return False, None
    if name == "substr":
        if not 2 <= len(args) <= 3:
            return False, None
        value, start = args[0], args[1]
        length = args[2] if len(args) == 3 else None
        if not isinstance(value, str) or not _is_int(start):
            return False, None
        if length is not None and not _is_int(length):
            return False, None
        begin = start - 1  # SQL substr is 1-based
        if begin < 0:
            if length is not None:
                length += begin
            begin = 0
        if length is None:
            return True, value[begin:]
        if length <= 0:
            return True, ""
        return True, value[begin:begin + length]
    if name == "lower":
        if len(args) != 1 or not isinstance(args[0], str):
            return False, None
        return True, args[0].lower()
    if name == "upper":
        if len(args) != 1 or not isinstance(args[0], str):
            return False, None
        return True, args[0].upper()
    return False, None
def _eval_operand(operand: dict[str, Any], column: str, value: Any) -> tuple[bool, Any]:
    kind = operand.get("kind")
    if kind == "literal":
        return True, operand.get("value")
    if kind == "column":
        return (True, value) if operand.get("name") == column else (False, None)
    if kind == "function":
        args: list[Any] = []
        for arg in operand.get("args", []):
            ok, resolved = _eval_operand(arg, column, value)
            if not ok:
                return False, None
            args.append(resolved)
        return _apply_function(str(operand["name"]), args)
    return False, None
def _compare(op: str, left: Any, right: Any) -> bool | None:
    try:
        if op == "=":
            return left == right
        if op == "!=":
            return left != right
        if op == "<":
            return left < right
        if op == "<=":
            return left <= right
        if op == ">":
            return left > right
        if op == ">=":
            return left >= right
    except TypeError:
        return None
    return None
def _eval_leaf(leaf: dict[str, Any], column: str, value: Any) -> bool | None:
    """Exact two-valued leaf result for ``column == value``; None if underivable."""
    kind = leaf["node"]
    if kind == "cmp":
        ok_left, left = _eval_operand(leaf["left"], column, value)
        ok_right, right = _eval_operand(leaf["right"], column, value)
        if not (ok_left and ok_right):
            return None
        if left is None or right is None:
            # NULL comparisons are three-valued UNKNOWN, not a derivable fact.
            return None
        return _compare(leaf["op"], left, right)
    if kind == "in":
        ok, resolved = _eval_operand(leaf["operand"], column, value)
        if not ok:
            return None
        values = []
        for literal in leaf["values"]:
            if literal.get("kind") != "literal":
                return None
            values.append(literal.get("value"))
        matched = resolved in values
        return matched != bool(leaf.get("negated"))
    if kind == "between":
        ok, resolved = _eval_operand(leaf["operand"], column, value)
        if not ok:
            return None
        low, high = leaf["low"], leaf["high"]
        if low.get("kind") != "literal" or high.get("kind") != "literal":
            return None
        try:
            matched = low.get("value") <= resolved <= high.get("value")
        except TypeError:
            return None
        return matched != bool(leaf.get("negated"))
    if kind == "is_null":
        ok, resolved = _eval_operand(leaf["operand"], column, value)
        if not ok:
            return None
        return (resolved is None) != bool(leaf.get("negated"))
    return None

# ---------------------------------------------------------------------------
# Fragment discovery and classification
# ---------------------------------------------------------------------------
def _leaf_operands(node: dict[str, Any]) -> list[dict[str, Any]]:
    kind = node["node"]
    if kind == "cmp":
        return [node["left"], node["right"]]
    if kind in ("in", "between", "is_null"):
        return [node["operand"]]
    return []
def _iter_fragments(node: dict[str, Any], negations: int = 0,
                    chain_top: dict[str, Any] | None = None):
    """Yield ``(leaf, negations, fragment_root)`` for every leaf predicate.

    ``fragment_root`` is the leaf itself or the outermost ``NOT`` of the chain
    directly above it; ``negations`` is the length of that chain.  ``NOT``
    nodes above an AND/OR belong to the parenthesized group, not to the leaf.
    """
    kind = node["node"]
    if kind == "not":
        top = chain_top if chain_top is not None else node
        yield from _iter_fragments(node["child"], negations + 1, top)
    elif kind in ("and", "or"):
        for child in node["children"]:
            yield from _iter_fragments(child, 0, None)
    else:
        yield node, negations, (chain_top if chain_top is not None else node)
def _leaf_column_function(
    leaf: dict[str, Any], partition_columns: set[str]
) -> tuple[str, str] | None:
    """Return ``(column, outermost_function)`` for a rewritable leaf, else None.

    A leaf is rewritable when every column it references is the same partition
    column and that column appears inside at least one function call.
    """
    columns: set[str] = set()
    functions: list[str] = []
    def walk(operand: dict[str, Any]) -> None:
        kind = operand.get("kind")
        if kind == "column":
            columns.add(operand["name"])
        elif kind == "function":
            functions.append(str(operand["name"]))
            for arg in operand.get("args", []):
                walk(arg)
    for operand in _leaf_operands(leaf):
        walk(operand)
    if len(columns) != 1 or not functions:
        return None
    column = next(iter(columns))
    if column not in partition_columns:
        return None
    return column, functions[0]
def _replace_nodes(node: dict[str, Any], mapping: dict[int, dict[str, Any]]) -> dict[str, Any]:
    replacement = mapping.get(id(node))
    if replacement is not None:
        return replacement
    kind = node["node"]
    if kind == "not":
        return {**node, "child": _replace_nodes(node["child"], mapping)}
    if kind in ("and", "or"):
        return {**node, "children": [_replace_nodes(c, mapping) for c in node["children"]]}
    return node

# ---------------------------------------------------------------------------
# Rewrite construction from the actual partition value domain
# ---------------------------------------------------------------------------
def _literal(value: Any) -> dict[str, Any]:
    return {"kind": "literal", "value": value, "literal_type": literal_type_name(value)}
def _column(column: str) -> dict[str, Any]:
    return {"kind": "column", "name": column}
def _rewrite_node(column: str, matched: list[Any], domain: list[Any]) -> tuple[dict[str, Any], str]:
    """Bare comparison selecting exactly ``matched`` out of ``domain``."""
    if not matched:
        return {"node": "cmp", "op": "=", "left": _literal(1), "right": _literal(0)}, "none"
    if len(matched) == 1:
        return {"node": "cmp", "op": "=", "left": _column(column),
                "right": _literal(matched[0])}, "value"
    if len(matched) == len(domain):
        return {"node": "cmp", "op": "=", "left": _literal(1), "right": _literal(1)}, "all"
    low, high = matched[0], matched[-1]
    if [value for value in domain if low <= value <= high] == matched:
        return {"node": "between", "operand": _column(column),
                "low": _literal(low), "high": _literal(high), "negated": False}, "range"
    return {"node": "in", "operand": _column(column),
            "values": [_literal(value) for value in matched], "negated": False}, "set"
def _domain_values(table: TableManifest, column: str) -> list[Any]:
    return sorted({partition.values[column] for partition in table.partitions})

# ---------------------------------------------------------------------------
# Aggregated degraded-path suggestion block
# ---------------------------------------------------------------------------
def build_degraded_path_rewrite(
    table: TableManifest,
    predicate: dict[str, Any] | None,
    analysis: dict[str, Any],
    partition: dict[str, Any],
    scanned_bytes: int,
) -> dict[str, Any] | None:
    """Aggregate bare-comparison rewrites for a query on the degraded path.

    Returns None unless the query hits both ``FUNCTION_WRAPPED_PARTITION``
    (function-wrapped partition column present) and
    ``UNKNOWN_PREDICATE_CONSERVATIVE`` (all partitions kept with undecidable
    reasons).  Each suggestion rewrites one fragment — including any ``NOT``
    chain directly above it — into a bare comparison that is value-equivalent
    over the table's actual partition domain, and reports a conservative
    upper bound on the bytes the rewritten query would scan.
    """
    wrapped = analysis.get("function_wrapped_partition_columns") or []
    reasons = analysis.get("undecidable_reasons") or []
    if predicate is None or not wrapped or not reasons or not partition.get("all_selected"):
        return None
    partition_columns = set(table.partition_column_names)
    groups: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for leaf, negations, root in _iter_fragments(predicate):
        info = _leaf_column_function(leaf, partition_columns)
        if info is None:
            continue
        column, function = info
        domain = _domain_values(table, column)
        if not domain:
            continue
        matched: list[Any] = []
        sound = True
        for value in domain:
            result = _eval_leaf(leaf, column, value)
            if result is None:
                sound = False
                break
            if result:
                matched.append(value)
        if not sound:
            continue
        if negations % 2 == 1:
            excluded = set(matched)
            matched = [value for value in domain if value not in excluded]
        signature = canonical_json({"leaf": leaf, "negated": negations % 2})
        group = groups.get(signature)
        if group is None:
            group = {
                "column": column,
                "function": function,
                "fragment_root": root,
                "matched": matched,
                "domain": domain,
                "roots": [],
            }
            groups[signature] = group
            order.append(signature)
        group["roots"].append(root)
    suggestions: list[dict[str, Any]] = []
    for signature in order:
        group = groups[signature]
        node, match_kind = _rewrite_node(group["column"], group["matched"], group["domain"])
        mapping = {id(root): node for root in group["roots"]}
        rewritten = _replace_nodes(predicate, mapping)
        pruned = prune_partitions(table, rewritten, _EMPTY_ANALYSIS)
        bound = sum(item["byte_size"] for item in pruned["selected_partitions"])
        suggestions.append({
            "column": group["column"],
            "function": group["function"],
            "fragment": render_predicate(group["fragment_root"]),
            "rewrite": render_predicate(node),
            "rewritten_where": render_predicate(rewritten),
            "match": match_kind,
            "matched_partition_values": list(group["matched"]),
            "domain_size": len(group["domain"]),
            "selected_partitions": pruned["selected_count"],
            "total_partitions": pruned["partition_count"],
            "scanned_bytes_upper_bound": bound,
            "scan_byte_ratio": round_float(bound / table.byte_size) if table.byte_size else 0.0,
        })
    suggestions.sort(key=lambda item: (item["column"], item["function"], item["fragment"]))
    return {
        "trigger_rules": list(TRIGGER_RULES),
        "table_bytes": table.byte_size,
        "current_scanned_bytes": scanned_bytes,
        "suggestions": suggestions,
    }
