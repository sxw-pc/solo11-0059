"""Workload parsing and per-query static predicate analysis."""
from __future__ import annotations
import re
from pathlib import Path
from typing import Any
import yaml
from .errors import PredicateError, WorkloadError
from .models import Query, TableManifest, Workload
from .schema import fingerprint
from .expr.parser import parse_predicate
from .expr.validate import analyze_predicate, validate_predicate

_QUERY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
def _yaml(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise WorkloadError("cannot read workload", location=str(path),
                            reason="READ_ERROR", details={"error": str(exc)}) from exc
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise WorkloadError("workload is not valid YAML", location=str(path),
                            reason="YAML_ERROR", details={"error": str(exc)}) from exc
def _mapping(value: Any, location: str, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise WorkloadError("expected a mapping", location=location, field=field,
                            reason="TYPE_ERROR", details={"actual": type(value).__name__})
    return value
def load_workload(path: str | Path, table: TableManifest) -> Workload:
    workload_path = Path(path)
    location = str(workload_path)
    if not workload_path.is_file():
        raise WorkloadError("workload file does not exist", location=location,
                            field="workload", reason="NOT_FOUND")
    data = _yaml(workload_path)
    if isinstance(data, list):
        data = {"queries": data}
    mapping = _mapping(data, location, "$")
    unknown = set(mapping) - {"schema_version", "description", "queries"}
    if unknown:
        raise WorkloadError("unknown workload key", location=location, field="$",
                            reason="UNKNOWN_KEY", details={"keys": sorted(unknown)})
    raw_queries = mapping.get("queries")
    if not isinstance(raw_queries, list) or not raw_queries:
        raise WorkloadError("queries must be a non-empty list", location=location,
                            field="queries", reason="TYPE_ERROR")
    queries: list[Query] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_queries):
        field = f"queries[{index}]"
        if not isinstance(raw, dict):
            raise WorkloadError("query must be a mapping", location=location, field=field,
                                reason="TYPE_ERROR")
        query_id = raw.get("id")
        if not isinstance(query_id, str) or not _QUERY_ID_RE.match(query_id):
            raise WorkloadError("invalid query id", location=location, field=f"{field}.id",
                                reason="INVALID_QUERY_ID", details={"id": query_id})
        if query_id in seen:
            raise WorkloadError("duplicate query id", location=location, field=f"{field}.id",
                                reason="DUPLICATE_QUERY_ID", details={"id": query_id})
        seen.add(query_id)
        description = raw.get("description", "")
        if not isinstance(description, str):
            raise WorkloadError("description must be a string", location=location,
                                field=f"{field}.description", reason="TYPE_ERROR")
        where_raw = raw.get("where")
        predicate_raw = raw.get("predicate")
        if where_raw is not None and predicate_raw is not None and where_raw != predicate_raw:
            raise WorkloadError("where and predicate disagree", location=location, field=field,
                                reason="CONFLICTING_PREDICATE")
        text = where_raw if where_raw is not None else predicate_raw
        if text is not None and not isinstance(text, str):
            raise WorkloadError("predicate must be a string", location=location,
                                field=f"{field}.where", reason="TYPE_ERROR")
        try:
            predicate = parse_predicate(text, query_id) if text is not None else None
            validate_predicate(predicate, table.column_types, query_id)
        except PredicateError as exc:
            raise WorkloadError(str(exc), location=location, field=f"{field}.where",
                                reason=exc.reason,
                                details={"query_id": query_id, **exc.details}) from exc
        queries.append(Query(query_id, description, text, predicate))
    queries.sort(key=lambda item: item.id)
    payload = [{"id": item.id, "description": item.description, "where": item.where}
               for item in queries]
    return Workload(queries, fingerprint(payload), workload_path.name)
def query_analysis(query: Query, table: TableManifest) -> dict[str, Any]:
    return analyze_predicate(query.predicate, table.column_types,
                             table.partition_column_names, query.id)
