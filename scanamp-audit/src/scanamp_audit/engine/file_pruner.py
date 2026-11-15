"""File-level statistics pruning and per-file selectivity estimates."""
from __future__ import annotations
import math
from typing import Any
from ..expr.estimate import estimate_selectivity, estimate_selectivity_interval
from ..expr.evaluator import FALSE, TRUE, UNKNOWN, evaluate
from ..models import ManifestFile, TableManifest
from ..schema import partition_id, round_float

DEFAULT_INTERVAL_WIDTH_THRESHOLD = 0.8


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
    *,
    interval_width_threshold: float = DEFAULT_INTERVAL_WIDTH_THRESHOLD,
) -> dict[str, Any]:
    decisions: list[dict[str, Any]] = []
    scanned_files = scanned_bytes = scanned_rows = 0
    estimated_rows = estimated_bytes = 0
    estimated_matched_bytes_low = estimated_matched_bytes_high = 0
    estimated_matched_rows_low = estimated_matched_rows_high = 0
    wide_interval_files = 0
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
        selectivity_low = selectivity_high = 0.0
        rows = bytes_ = 0
        rows_low = rows_high = bytes_low = bytes_high = 0
        wide_interval = False
        interval_width = 0.0
        if result != FALSE:
            if predicate is None:
                selectivity = 1.0
                selectivity_low = selectivity_high = 1.0
            else:
                selectivity = estimate_selectivity(
                    predicate, resolver, table.column_types, default_selectivity
                )
                selectivity_low, selectivity_high = estimate_selectivity_interval(
                    predicate, resolver, table.column_types, default_selectivity
                )
            rows = int(round(file.row_count * selectivity))
            bytes_ = int(round(file.byte_size * selectivity))
            # Confidence bounds on matched volume. The floor/ceil pair keeps
            # the true value bracketed regardless of rounding direction.
            rows_low = math.floor(file.row_count * selectivity_low)
            rows_high = math.ceil(file.row_count * selectivity_high)
            bytes_low = math.floor(file.byte_size * selectivity_low)
            bytes_high = math.ceil(file.byte_size * selectivity_high)
            interval_width = clamp_width(selectivity_high - selectivity_low)
            wide_interval = interval_width > interval_width_threshold
            if wide_interval:
                wide_interval_files += 1
            scanned_files += 1
            scanned_bytes += file.byte_size
            scanned_rows += file.row_count
            estimated_rows += rows
            estimated_bytes += bytes_
            estimated_matched_rows_low += rows_low
            estimated_matched_rows_high += rows_high
            estimated_matched_bytes_low += bytes_low
            estimated_matched_bytes_high += bytes_high
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
                # Selective-trust interval evidence. These fields describe
                # estimation uncertainty only and never influence decision,
                # reason or the keep/drop outcome above.
                "selectivity_interval": [
                    round_float(selectivity_low),
                    round_float(selectivity_high),
                ],
                "selectivity_interval_width": round_float(interval_width),
                "wide_selectivity_interval": wide_interval,
                "estimated_matched_bytes_low": bytes_low,
                "estimated_matched_bytes_high": bytes_high,
                "estimated_matched_rows_low": rows_low,
                "estimated_matched_rows_high": rows_high,
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
        "estimated_matched_rows_low": estimated_matched_rows_low,
        "estimated_matched_rows_high": estimated_matched_rows_high,
        "estimated_matched_bytes_low": estimated_matched_bytes_low,
        "estimated_matched_bytes_high": estimated_matched_bytes_high,
        "wide_interval_file_count": wide_interval_files,
        "selectivity_interval_width_threshold": round_float(interval_width_threshold),
        "stats_missing_columns": sorted(missing_columns),
        "stats_missing_file_count": missing_files,
    }


def clamp_width(value: float) -> float:
    """Keep an interval width inside [0, 1] despite float drift."""
    return max(0.0, min(1.0, float(value)))
