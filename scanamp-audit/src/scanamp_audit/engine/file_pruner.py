"""File-level statistics pruning and per-file selectivity estimates."""
from __future__ import annotations
from typing import Any
from ..expr.estimate import estimate_selectivity
from ..expr.evaluator import FALSE, TRUE, UNKNOWN, evaluate
from ..models import ManifestFile, TableManifest
from ..schema import partition_id, round_float

def _resolver(file: ManifestFile, table: TableManifest) -> dict[str, dict[str, Any]]:
    resolver: dict[str, dict[str, Any]] = {
        name: {"kind": "exact", "type": table.column_types[name], "value": value}
        for name, value in file.partition_values.items()
    }
    for name, stats in file.columns.items():
        if not stats.has_stats or stats.min is None or stats.max is None:
            resolver[name] = {"kind": "unknown", "type": stats.type}
            continue
        resolver[name] = {
            "kind": "stats",
            "type": stats.type,
            "min": stats.min,
            "max": stats.max,
            "null_count": stats.null_count,
            "distinct_count": stats.distinct_count,
            "row_count": file.row_count,
        }
    return resolver
def prune_files(
    table: TableManifest,
    files: list[ManifestFile],
    predicate: dict[str, Any] | None,
    default_selectivity: float,
    filtering_columns: list[str] | None = None,
) -> dict[str, Any]:
    decisions: list[dict[str, Any]] = []
    scanned_files = scanned_bytes = scanned_rows = 0
    estimated_rows = estimated_bytes = 0
    dropped = unknown = 0
    missing_columns: set[str] = set()
    missing_files = 0
    candidate_columns = list(filtering_columns) if filtering_columns is not None else list(table.column_types)
    data_filter_columns = [
        name for name in candidate_columns
        if name in table.column_types and name not in table.partition_column_names
    ]
    for file in sorted(files, key=lambda item: item.path):
        resolver = _resolver(file, table)
        result = TRUE if predicate is None else evaluate(predicate, resolver, table.column_types)
        file_missing = sorted(
            name
            for name in data_filter_columns
            if name not in file.columns or not file.columns[name].has_stats
        )
        if file_missing:
            missing_files += 1
            missing_columns.update(file_missing)
        decision = "keep" if result == TRUE else "drop" if result == FALSE else "unknown"
        selectivity = 0.0
        rows = bytes_ = 0
        if result != FALSE:
            selectivity = (
                1.0
                if predicate is None
                else estimate_selectivity(predicate, resolver, table.column_types,
                                          default_selectivity)
            )
            rows = int(round(file.row_count * selectivity))
            bytes_ = int(round(file.byte_size * selectivity))
            scanned_files += 1
            scanned_bytes += file.byte_size
            scanned_rows += file.row_count
            estimated_rows += rows
            estimated_bytes += bytes_
            if result == UNKNOWN:
                unknown += 1
        else:
            dropped += 1
        decisions.append(
            {
                "path": file.path,
                "partition_id": partition_id(file.partition_values,
                                             table.partition_column_names),
                "decision": decision,
                "reason": "PREDICATE_TRUE" if result == TRUE else "PREDICATE_FALSE"
                if result == FALSE else "PREDICATE_UNKNOWN",
                "stats_missing_columns": file_missing,
                "estimated_selectivity": round_float(selectivity),
                "estimated_matched_rows": rows,
                "estimated_matched_bytes": bytes_,
            }
        )
    return {
        "file_count": len(files),
        "decisions": decisions,
        "scanned_files": scanned_files,
        "scanned_bytes": scanned_bytes,
        "scanned_rows": scanned_rows,
        "dropped_files": dropped,
        "unknown_files": unknown,
        "estimated_matched_rows": estimated_rows,
        "estimated_matched_bytes": estimated_bytes,
        "stats_missing_columns": sorted(missing_columns),
        "stats_missing_file_count": missing_files,
    }
