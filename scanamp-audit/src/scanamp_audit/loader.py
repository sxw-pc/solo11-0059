"""Manifest and workload loading, validation, inference and fingerprinting."""
from __future__ import annotations
from pathlib import Path, PurePosixPath
from typing import Any
import yaml
from .errors import ManifestError, SecurityError
from .models import ColumnDef, ColumnStats, ManifestFile, Partition, TableManifest
from .schema import SUPPORTED_TYPES, coerce_value, display_value, fingerprint, partition_id
from .security import normalize_manifest_path, safe_join

def _yaml(path: Path, error_cls: Any, label: str) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise error_cls(f"cannot read {label}", location=str(path), reason="READ_ERROR",
                        details={"error": str(exc)}) from exc
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise error_cls(f"{label} is not valid YAML", location=str(path), reason="YAML_ERROR",
                        details={"error": str(exc)}) from exc
def _mapping(value: Any, location: str, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ManifestError("expected a mapping", location=location, field=field,
                            reason="TYPE_ERROR", details={"actual": type(value).__name__})
    return value
def _int(value: Any, field: str, location: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ManifestError(f"{field} must be an integer >= {minimum}", location=location,
                            field=field, reason="INVALID_INTEGER", details={"value": value})
    return value
def _columns(raw: Any, location: str, field: str) -> list[ColumnDef]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ManifestError(f"{field} must be a list", location=location, field=field,
                            reason="TYPE_ERROR")
    result: list[ColumnDef] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        mapping = _mapping(item, location, f"{field}[{index}]")
        name, type_name = mapping.get("name"), mapping.get("type")
        if not isinstance(name, str) or not name:
            raise ManifestError("column name must be a non-empty string", location=location,
                                field=f"{field}[{index}].name", reason="INVALID_NAME")
        if type_name not in SUPPORTED_TYPES:
            raise ManifestError("unsupported column type", location=location,
                                field=f"{field}[{index}].type", reason="INVALID_TYPE",
                                details={"type": type_name, "allowed": list(SUPPORTED_TYPES)})
        if name in seen:
            raise ManifestError("duplicate column name", location=location,
                                field=f"{field}[{index}].name", reason="DUPLICATE_COLUMN",
                                details={"name": name})
        seen.add(name)
        result.append(ColumnDef(name, type_name))
    return result
def _stats(raw: Any, declared_type: str, row_count: int, location: str, field: str) -> ColumnStats:
    mapping = _mapping(raw, location, field)
    type_name = mapping.get("type", declared_type)
    if type_name != declared_type:
        raise ManifestError("statistics type does not match schema", location=location, field=field,
                            reason="TYPE_MISMATCH", details={"schema": declared_type, "stats": type_name})
    has_stats = mapping.get("has_stats", True)
    if not isinstance(has_stats, bool):
        raise ManifestError("has_stats must be boolean", location=location, field=f"{field}.has_stats",
                            reason="TYPE_ERROR")
    min_value = mapping.get("min")
    max_value = mapping.get("max")
    if has_stats and (min_value is None or max_value is None):
        raise ManifestError("has_stats=true requires min and max", location=location, field=field,
                            reason="MISSING_STATS_BOUNDS")
    coerced_min = coerced_max = None
    if min_value is not None:
        try:
            coerced_min = coerce_value(min_value, declared_type)
        except ValueError as exc:
            raise ManifestError(str(exc), location=location, field=f"{field}.min",
                                reason="INVALID_STATS_VALUE") from exc
    if max_value is not None:
        try:
            coerced_max = coerce_value(max_value, declared_type)
        except ValueError as exc:
            raise ManifestError(str(exc), location=location, field=f"{field}.max",
                                reason="INVALID_STATS_VALUE") from exc
    if coerced_min is not None and coerced_max is not None and coerced_min > coerced_max:
        raise ManifestError("stats min is greater than max", location=location, field=field,
                            reason="INVALID_STATS_RANGE")
    null_count = mapping.get("null_count")
    distinct_count = mapping.get("distinct_count")
    for key, value in (("null_count", null_count), ("distinct_count", distinct_count)):
        if value is not None:
            if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > row_count:
                raise ManifestError(f"{key} must be an integer between 0 and row_count",
                                    location=location, field=f"{field}.{key}",
                                    reason="INVALID_STATS_COUNT")
    return ColumnStats(type_name, coerced_min, coerced_max, null_count, distinct_count, has_stats)
def _infer(path: str, partition_names: set[str]) -> dict[str, str]:
    inferred: dict[str, str] = {}
    for part in PurePosixPath(path).parts[:-1]:
        if "=" in part:
            key, value = part.split("=", 1)
            if key in partition_names:
                inferred[key] = value
    return inferred
def _parse_file(
    raw: Any, index: int, root: Path, location: str, partition_cols: list[ColumnDef],
    data_cols: dict[str, ColumnDef],
) -> tuple[ManifestFile, list[dict[str, Any]]]:
    field = f"files[{index}]"
    mapping = _mapping(raw, location, field)
    raw_path = mapping.get("path")
    try:
        normalized = normalize_manifest_path(raw_path, location=location)
        actual_path = safe_join(root, normalized, location=location)
    except SecurityError as exc:
        raise ManifestError(str(exc), location=location, field=f"{field}.path",
                            reason=exc.reason or "INVALID_PATH", details=exc.details) from exc
    if not actual_path.exists():
        raise ManifestError("manifest file does not exist", location=location, field=f"{field}.path",
                            reason="FILE_NOT_FOUND", details={"path": normalized})
    if not actual_path.is_file():
        raise ManifestError("manifest path is not a regular file", location=location,
                            field=f"{field}.path", reason="NOT_A_FILE", details={"path": normalized})
    byte_size = _int(mapping.get("byte_size"), f"{field}.byte_size", location)
    actual_size = actual_path.stat().st_size
    if actual_size != byte_size:
        raise ManifestError("byte_size does not match the file on disk", location=location,
                            field=f"{field}.byte_size", reason="BYTE_SIZE_MISMATCH",
                            details={"declared": byte_size, "actual": actual_size, "path": normalized})
    row_count = _int(mapping.get("row_count"), f"{field}.row_count", location)
    raw_partition = mapping.get("partition_values")
    if not isinstance(raw_partition, dict):
        raise ManifestError("partition_values must be a mapping", location=location,
                            field=f"{field}.partition_values", reason="TYPE_ERROR")
    partition_names = [col.name for col in partition_cols]
    missing = [name for name in partition_names if name not in raw_partition]
    extra = [name for name in raw_partition if name not in partition_names]
    if missing or extra:
        raise ManifestError("partition_values must exactly match declared partition columns",
                            location=location, field=f"{field}.partition_values",
                            reason="PARTITION_SCHEMA_MISMATCH",
                            details={"missing": sorted(missing), "extra": sorted(extra)})
    partition_values: dict[str, Any] = {}
    for column in partition_cols:
        value = raw_partition[column.name]
        if value is None:
            raise ManifestError("partition value must not be null", location=location,
                                field=f"{field}.partition_values.{column.name}",
                                reason="NULL_PARTITION_VALUE")
        try:
            partition_values[column.name] = coerce_value(value, column.type)
        except ValueError as exc:
            raise ManifestError(str(exc), location=location,
                                field=f"{field}.partition_values.{column.name}",
                                reason="INVALID_PARTITION_VALUE") from exc
    raw_columns = mapping.get("columns", {})
    if not isinstance(raw_columns, dict):
        raise ManifestError("columns must be a mapping", location=location,
                            field=f"{field}.columns", reason="TYPE_ERROR")
    stats: dict[str, ColumnStats] = {}
    known_columns = {**{col.name: col for col in partition_cols}, **data_cols}
    for name, raw_stat in raw_columns.items():
        if name not in known_columns:
            raise ManifestError("statistics reference an unknown column", location=location,
                                field=f"{field}.columns.{name}", reason="UNKNOWN_COLUMN",
                                details={"column": name})
        stats[name] = _stats(raw_stat, known_columns[name].type, row_count, location,
                             f"{field}.columns.{name}")
    inferred_raw = _infer(normalized, set(partition_names))
    mismatches: list[dict[str, Any]] = []
    for name in sorted(inferred_raw):
        column = next(col for col in partition_cols if col.name == name)
        raw_value = inferred_raw[name]
        try:
            inferred_value: Any = coerce_value(raw_value, column.type)
        except ValueError:
            inferred_value = raw_value
        declared = partition_values[name]
        if display_value(inferred_value) != display_value(declared):
            mismatches.append({"file": normalized, "column": name,
                               "inferred": display_value(inferred_value),
                               "declared": display_value(declared),
                               "reason": "PATH_MANIFEST_MISMATCH"})
    return ManifestFile(normalized, partition_values, row_count, byte_size, stats,
                        {name: display_value(value) for name, value in sorted(inferred_raw.items())}), mismatches
def load_table(lake_root: str | Path) -> TableManifest:
    root = Path(lake_root)
    manifest_path = root / "_metadata" / "manifest.yaml"
    location = str(manifest_path)
    if not root.exists():
        raise ManifestError("lake directory does not exist", location=str(root),
                            field="lake", reason="NOT_FOUND")
    if not manifest_path.is_file():
        raise ManifestError("manifest file not found under _metadata/", location=location,
                            field="manifest", reason="NOT_FOUND")
    data = _yaml(manifest_path, ManifestError, "manifest")
    mapping = _mapping(data, location, "$")
    name = mapping.get("table")
    if not isinstance(name, str) or not name:
        raise ManifestError("table must be a non-empty string", location=location,
                            field="table", reason="INVALID_NAME")
    description = mapping.get("description", "")
    if not isinstance(description, str):
        raise ManifestError("description must be a string", location=location,
                            field="description", reason="TYPE_ERROR")
    partition_cols = _columns(mapping.get("partition_columns"), location, "partition_columns")
    data_cols_list = _columns(mapping.get("columns"), location, "columns")
    overlap = {col.name for col in partition_cols} & {col.name for col in data_cols_list}
    if overlap:
        raise ManifestError("columns appear in both partition_columns and columns",
                            location=location, field="columns", reason="DUPLICATE_COLUMN",
                            details={"columns": sorted(overlap)})
    data_cols = {col.name: col for col in data_cols_list}
    raw_files = mapping.get("files")
    if not isinstance(raw_files, list):
        raise ManifestError("files must be a list", location=location, field="files",
                            reason="TYPE_ERROR")
    files: list[ManifestFile] = []
    mismatches: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    for index, raw in enumerate(raw_files):
        item, item_mismatches = _parse_file(raw, index, root, location, partition_cols,
                                            data_cols)
        if item.path in seen_paths:
            raise ManifestError("duplicate manifest file path", location=location,
                                field=f"files[{index}].path", reason="DUPLICATE_PATH",
                                details={"path": item.path})
        seen_paths.add(item.path)
        files.append(item)
        mismatches.extend(item_mismatches)
    files.sort(key=lambda item: item.path)
    mismatches.sort(key=lambda item: (item["file"], item["column"]))
    partition_names = [col.name for col in partition_cols]
    groups: dict[tuple[Any, ...], list[ManifestFile]] = {}
    for item in files:
        groups.setdefault(tuple(item.partition_values[name] for name in partition_names), []).append(item)
    partitions = [
        Partition(partition_id(dict(zip(partition_names, key)), partition_names),
                  dict(zip(partition_names, key)), sorted(items, key=lambda item: item.path))
        for key, items in sorted(groups.items(), key=lambda pair: pair[0])
    ]
    payload = {
        "table": name,
        "description": description,
        "partition_columns": [col.to_dict() for col in partition_cols],
        "columns": [col.to_dict() for col in data_cols_list],
        "files": [item.to_dict() for item in files],
        "path_mismatches": mismatches,
    }
    return TableManifest(name, description, partition_cols, data_cols_list, files, partitions,
                         fingerprint(payload), mismatches, root.name, root)
