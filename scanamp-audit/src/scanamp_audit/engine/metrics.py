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
def _file_key(decision: dict[str, Any]) -> tuple[str, str]:
    """File identity used for coverage sets: partition id plus path."""
    return (decision["partition_id"], decision["path"])
def _coverage(result: dict[str, Any]) -> dict[tuple[str, str], str]:
    """Candidate coverage of one prune run: non-dropped files mapped to their decision.

    Mirrors the keep/drop/unknown semantics of ``prune_files``: ``keep`` and
    ``unknown`` files are scanned, only ``drop`` removes a file from the set.
    """
    return {
        _file_key(decision): decision["decision"]
        for decision in result["decisions"]
        if decision["decision"] != "drop"
    }
def compute_or_attribution(
    branch_results: list[dict[str, Any]], union_result: dict[str, Any]
) -> dict[str, Any]:
    """Attribute an OR union's expansion to shared vs exclusive file coverage.

    ``branch_results``/``union_result`` are ``prune_files`` results for the OR
    branches and the whole OR predicate over the same file set. A file covered
    by two or more branches is *shared*; a file covered by exactly one branch
    is *exclusive*. The expansion delta (union size minus the largest branch)
    is decomposed into the files the largest branch misses: ``shared_contribution``
    counts missed files still covered by multiple branches, ``exclusive_contribution``
    missed files covered by exactly one branch. ``dominant_cause`` is:

    - ``none``: no expansion and the union is not unknown-dominated;
    - ``stats_missing``: no expansion, but the largest branch keeps files only
      because every covered file evaluates UNKNOWN (typically missing stats),
      so the union is dominated by conservative retention, not OR splitting;
    - ``exclusive`` / ``shared``: which contribution dominates the expansion
      (ties attribute to ``exclusive``, the actionable case for splitting).
    """
    coverages = [_coverage(result) for result in branch_results]
    union_coverage = _coverage(union_result)
    counts: dict[tuple[str, str], int] = {}
    for coverage in coverages:
        for key in coverage:
            counts[key] = counts.get(key, 0) + 1
    shared_files = sum(1 for key in union_coverage if counts.get(key, 0) >= 2)
    exclusive_files = sum(1 for key in union_coverage if counts.get(key, 0) == 1)
    largest_index = 0
    for index, coverage in enumerate(coverages):
        if len(coverage) > len(coverages[largest_index]):
            largest_index = index
    largest = coverages[largest_index] if coverages else {}
    union_files = len(union_coverage)
    expansion_delta = union_files - len(largest)
    shared_contribution = exclusive_contribution = 0
    for key in union_coverage:
        if key in largest:
            continue
        if counts.get(key, 0) >= 2:
            shared_contribution += 1
        else:
            exclusive_contribution += 1
    branches = []
    for index, coverage in enumerate(coverages):
        unknown = sum(1 for decision in coverage.values() if decision == "unknown")
        exclusive = sum(1 for key in coverage if counts.get(key, 0) == 1)
        branches.append(
            {
                "index": index,
                "covered_files": len(coverage),
                "keep_files": len(coverage) - unknown,
                "unknown_files": unknown,
                "exclusive_files": exclusive,
                "shared_files": len(coverage) - exclusive,
                "all_unknown": bool(coverage) and unknown == len(coverage),
            }
        )
    stats_missing_branches = [item["index"] for item in branches if item["all_unknown"]]
    if expansion_delta <= 0:
        cause = (
            "stats_missing"
            if branches and branches[largest_index]["all_unknown"]
            else "none"
        )
    elif exclusive_contribution >= shared_contribution:
        cause = "exclusive"
    else:
        cause = "shared"
    return {
        "union_files": union_files,
        "largest_branch_index": largest_index if coverages else None,
        "largest_branch_files": len(largest),
        "expansion_delta": expansion_delta,
        "shared_files": shared_files,
        "exclusive_files": exclusive_files,
        "shared_contribution": shared_contribution,
        "exclusive_contribution": exclusive_contribution,
        "dominant_cause": cause,
        "stats_missing_branches": stats_missing_branches,
        "branches": branches,
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
    branch_prunes = [
        prune_files(table, table.files, branch, default_selectivity, columns)
        for branch in branches
    ]
    union = prune_files(table, table.files, predicate, default_selectivity, columns)
    attribution = compute_or_attribution(branch_prunes, union)
    branch_results = []
    for index, (branch, result, coverage) in enumerate(
        zip(branches, branch_prunes, attribution["branches"])
    ):
        branch_results.append(
            {
                "index": index,
                "scanned_files": result["scanned_files"],
                "scanned_bytes": result["scanned_bytes"],
                "scanned_rows": result["scanned_rows"],
                "keep_files": coverage["keep_files"],
                "unknown_files": coverage["unknown_files"],
                "exclusive_files": coverage["exclusive_files"],
                "shared_files": coverage["shared_files"],
                "all_unknown": coverage["all_unknown"],
                "expression": branch,
            }
        )
    largest = attribution["largest_branch_files"]
    expansion = round_float(union["scanned_files"] / largest) if largest else 1.0
    return {
        "branch_count": len(branches),
        "branches": branch_results,
        "union_scanned_files": union["scanned_files"],
        "union_scanned_bytes": union["scanned_bytes"],
        "expansion_factor": expansion,
        "attribution": attribution,
    }
