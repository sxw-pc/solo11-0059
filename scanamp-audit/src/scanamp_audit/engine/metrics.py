"""Scan-amplification metrics and OR-branch expansion analysis."""
from __future__ import annotations
from typing import Any
from ..models import TableManifest
from ..schema import round_float
from .file_pruner import prune_files

def ratio(numerator: int | float, denominator: int | float) -> float:
    return round_float(float(numerator) / float(denominator)) if denominator else 0.0
def compute_metrics(
    table: TableManifest, partition: dict[str, Any], files: dict[str, Any]
) -> dict[str, Any]:
    table_files, table_bytes, table_rows = table.file_count, table.byte_size, table.row_count
    partition_files = sum(item["file_count"] for item in partition["selected_partitions"])
    partition_bytes = sum(item["byte_size"] for item in partition["selected_partitions"])
    partition_rows = sum(item["row_count"] for item in partition["selected_partitions"])
    scanned_files = files["scanned_files"]
    scanned_bytes = files["scanned_bytes"]
    scanned_rows = files["scanned_rows"]
    matched_bytes = files["estimated_matched_bytes"]
    amplification = min(round_float(scanned_bytes / max(matched_bytes, 1)), 1_000_000.0)
    return {
        "table_files": table_files,
        "table_bytes": table_bytes,
        "table_rows": table_rows,
        "after_partition_files": partition_files,
        "after_partition_bytes": partition_bytes,
        "after_partition_rows": partition_rows,
        "scanned_files": scanned_files,
        "scanned_bytes": scanned_bytes,
        "scanned_rows": scanned_rows,
        "estimated_matched_rows": files["estimated_matched_rows"],
        "estimated_matched_bytes": matched_bytes,
        "partition_prune_ratio": ratio(table_files - partition_files, table_files),
        "stats_prune_ratio": ratio(partition_files - scanned_files, partition_files),
        "total_prune_ratio": ratio(table_files - scanned_files, table_files),
        "scan_byte_ratio": ratio(scanned_bytes, table_bytes),
        "scan_amplification": amplification,
    }
def compute_or_branches(
    predicate: dict[str, Any] | None,
    table: TableManifest,
    default_selectivity: float,
    columns: list[str],
) -> dict[str, Any] | None:
    if predicate is None or predicate.get("node") != "or":
        return None
    branches = predicate["children"]
    branch_results = []
    for index, branch in enumerate(branches):
        result = prune_files(table, table.files, branch, default_selectivity, columns)
        branch_results.append(
            {
                "index": index,
                "scanned_files": result["scanned_files"],
                "scanned_bytes": result["scanned_bytes"],
                "scanned_rows": result["scanned_rows"],
                "expression": branch,
            }
        )
    union = prune_files(table, table.files, predicate, default_selectivity, columns)
    largest = max((item["scanned_files"] for item in branch_results), default=0)
    expansion = round_float(union["scanned_files"] / largest) if largest else 1.0
    return {
        "branch_count": len(branches),
        "branches": branch_results,
        "union_scanned_files": union["scanned_files"],
        "union_scanned_bytes": union["scanned_bytes"],
        "expansion_factor": expansion,
    }
