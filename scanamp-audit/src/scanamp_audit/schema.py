"""Column type rules, canonical values and deterministic JSON helpers.

Canonical values are JSON-friendly: dates stay as ``YYYY-MM-DD`` strings
(ISO dates sort lexicographically), integers stay integers, floats stay
floats and strings stay strings.
"""
from __future__ import annotations
import hashlib
import json
import re
from datetime import date
from typing import Any

SUPPORTED_TYPES = ("string", "integer", "float", "date")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)
def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)
def coerce_value(value: Any, type_name: str, *, source: str = "data") -> Any:
    """Coerce a manifest/stat value to its canonical Python representation.

    ``source='data'`` allows numeric strings for partition directories and
    sidecar stats. Literals are handled by :func:`coerce_literal` instead.
    """
    if type_name not in SUPPORTED_TYPES:
        raise ValueError(f"unsupported column type: {type_name!r}")
    if type_name == "string":
        if not isinstance(value, str):
            raise ValueError(f"expected string for string column, got {type(value).__name__}")
        return value
    if type_name == "date":
        if not isinstance(value, str) or not _DATE_RE.match(value):
            raise ValueError(f"expected ISO date YYYY-MM-DD, got {value!r}")
        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"invalid ISO date {value!r}: {exc}") from exc
        return value
    if type_name == "integer":
        if _is_int(value):
            return value
        if source == "data" and isinstance(value, str):
            try:
                return int(value, 10)
            except ValueError as exc:
                raise ValueError(f"expected integer, got {value!r}") from exc
        if source == "data" and isinstance(value, float) and value.is_integer():
            return int(value)
        raise ValueError(f"expected integer, got {value!r}")
    if type_name == "float":
        if _is_number(value):
            return float(value)
        if source == "data" and isinstance(value, str):
            try:
                return float(value)
            except ValueError as exc:
                raise ValueError(f"expected float, got {value!r}") from exc
        raise ValueError(f"expected float, got {value!r}")
    raise ValueError(f"unsupported column type: {type_name!r}")
def coerce_literal(literal: dict[str, Any], type_name: str) -> Any:
    """Coerce a parsed literal AST node to the target column type."""
    if literal.get("kind") != "literal":
        raise ValueError("operand is not a literal")
    literal_type = literal.get("literal_type")
    value = literal.get("value")
    if literal_type == "null":
        raise ValueError("NULL literal is only valid with IS [NOT] NULL")
    if type_name == "string":
        if literal_type != "string":
            raise ValueError(f"expected string literal for string column, got {literal_type}")
        return value
    if type_name == "date":
        if literal_type != "string":
            raise ValueError(f"expected date string literal, got {literal_type}")
        return coerce_value(value, "date")
    if type_name == "integer":
        if literal_type != "integer":
            raise ValueError(f"expected integer literal, got {literal_type}")
        return int(value)
    if type_name == "float":
        if literal_type not in ("integer", "float"):
            raise ValueError(f"expected numeric literal, got {literal_type}")
        return float(value)
    raise ValueError(f"unsupported column type: {type_name!r}")
def literal_type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "string"
    raise ValueError(f"unsupported literal value: {value!r}")
def display_value(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)
def partition_id(values: dict[str, Any], column_names: list[str]) -> str:
    parts = []
    for name in column_names:
        parts.append(f"{name}={display_value(values.get(name))}")
    return "/".join(parts)
def canonical_json(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
def fingerprint(data: Any) -> str:
    return sha256_text(canonical_json(data))
def round_float(value: float, digits: int = 6) -> float:
    return round(float(value), digits)
