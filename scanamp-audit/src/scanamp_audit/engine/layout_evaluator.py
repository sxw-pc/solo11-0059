"""Partition layout statistics and counterfactual layout evaluation.

The evaluator answers a design-review question the normal audit cannot: if a
partition column were dropped from the layout, how would partition pruning for
each query change?  For each non-empty subset of partition columns it merges
the old partitions that share the remaining values, re-runs partition pruning
against the *actual* merged file sets, and reports prune ratios plus the
resulting candidate scan files and bytes.

Only partition pruning is re-evaluated; file-level statistics pruning is a
property of the files themselves and is unaffected by a layout change.
"""
from __future__ import annotations
from dataclasses import dataclass
from itertools import combinations
from typing import Any, Sequence
from ..expr.validate import analyze_predicate
from ..models import ColumnDef, Partition, Query, TableManifest
from ..schema import partition_id, round_float
from .partition_pruner import prune_partitions

def _ratio(numerator: int | float, denominator: int | float) -> float:
    return round_float(float(numerator) / float(denominator)) if denominator else 0.0
def _distribution(values: Sequence[int]) -> dict[str, Any]:
    ordered = sorted(values)
    if not ordered:
        return {"count": 0, "min": 0, "max": 0, "total": 0, "mean": 0.0, "median": 0.0}
    count = len(ordered)
    middle = count // 2
    median = ordered[middle] if count % 2 else (ordered[middle - 1] + ordered[middle]) / 2
    return {"count": count, "min": ordered[0], "max": ordered[-1], "total": sum(ordered),
            "mean": round_float(sum(ordered) / count), "median": round_float(median)}
def column_statistics(table: TableManifest) -> list[dict[str, Any]]:
    """Cardinality, per-value file/byte distribution and single-file share.

    Values of one column are treated as the partition key in isolation, which
    is the layout a reviewer evaluates when deciding whether to drop that
    column.  ``single_file_partition_ratio`` is the share of those values whose
    merged file set contains exactly one file.
    """
    stats: list[dict[str, Any]] = []
    for column in table.partition_columns:
        groups: dict[Any, dict[str, int]] = {}
        for part in table.partitions:
            value = part.values[column.name]
            group = groups.setdefault(
                value, {"partition_count": 0, "file_count": 0, "byte_size": 0, "row_count": 0}
            )
            group["partition_count"] += 1
            group["file_count"] += part.file_count
            group["byte_size"] += part.byte_size
            group["row_count"] += part.row_count
        values = [
            {"value": value, **groups[value], "single_file": groups[value]["file_count"] == 1}
            for value in sorted(groups)
        ]
        single_file_count = sum(1 for item in values if item["single_file"])
        stats.append(
            {
                "column": column.name,
                "type": column.type,
                "cardinality": len(values),
                "partition_count": len(table.partitions),
                "values": values,
                "file_count_distribution": _distribution(
                    [item["file_count"] for item in values]
                ),
                "byte_size_distribution": _distribution(
                    [item["byte_size"] for item in values]
                ),
                "single_file_partition_count": single_file_count,
                "single_file_partition_ratio": _ratio(single_file_count, len(values)),
            }
        )
    return stats

@dataclass
class LayoutView:
    """A table-like view exposing only the retained partition columns."""
    name: str
    partition_column_names: list[str]
    partitions: list[Partition]
    column_types: dict[str, str]

def _build_view(
    table: TableManifest, retained: Sequence[ColumnDef], removed: Sequence[str]
) -> tuple[LayoutView, int]:
    """Merge old partitions that share the retained values.

    Merging keeps the real ``ManifestFile`` objects so that file counts, byte
    sizes and row counts are always summed over the true merged file set.
    """
    names = [column.name for column in retained]
    groups: dict[tuple[Any, ...], list[Any]] = {}
    contributions: dict[tuple[Any, ...], int] = {}
    for part in table.partitions:
        key = tuple(part.values[name] for name in names)
        groups.setdefault(key, []).extend(part.files)
        contributions[key] = contributions.get(key, 0) + 1
    partitions = [
        Partition(partition_id(dict(zip(names, key)), names),
                  dict(zip(names, key)),
                  sorted(groups[key], key=lambda item: item.path))
        for key in sorted(groups)
    ]
    merged_groups = sum(1 for key in groups if contributions[key] > 1)
    label = ",".join(removed) if removed else "current"
    return LayoutView(label, names, partitions, table.column_types), merged_groups
def _layout_summary(view: LayoutView) -> dict[str, Any]:
    total = len(view.partitions)
    single = sum(1 for part in view.partitions if part.file_count == 1)
    return {
        "partition_count": total,
        "file_count": sum(part.file_count for part in view.partitions),
        "byte_size": sum(part.byte_size for part in view.partitions),
        "single_file_partition_count": single,
        "single_file_partition_ratio": _ratio(single, total),
    }
def _reasons(
    referenced: list[str], lost: list[str], lost_all: bool, removed: Sequence[str],
    baseline: dict[str, Any], current: dict[str, Any],
) -> list[str]:
    codes: set[str] = set()
    if not referenced:
        codes.add("QUERY_HAD_NO_PARTITION_FILTER")
    if lost:
        joined = ",".join(lost)
        codes.add("REMOVED_PARTITION_FILTER_COLUMNS:" + joined)
        codes.add("MERGED_PARTITION_PREDICATE_UNDECIDABLE:" + joined)
    elif removed:
        codes.add("REMOVED_COLUMNS_NOT_REFERENCED_BY_QUERY")
    if lost_all:
        joined = ",".join(lost)
        codes.add("QUERY_LOST_ONLY_PARTITION_FILTER:" + joined)
        codes.add("CONSERVATIVE_ALL_PARTITIONS_RETAINED:" + joined)
    if current["partition_count"] < baseline["partition_count"]:
        codes.add(f"PARTITIONS_MERGED:{baseline['partition_count']}->{current['partition_count']}")
    for key, label in (("partition_prune_ratio", "PARTITION_PRUNE_RATIO"),
                       ("candidate_file_prune_ratio", "CANDIDATE_FILE_PRUNE_RATIO"),
                       ("candidate_files", "CANDIDATE_FILES"),
                       ("candidate_bytes", "CANDIDATE_BYTES")):
        if current[key] > baseline[key]:
            codes.add(label + "_INCREASED")
        elif current[key] < baseline[key]:
            codes.add(label + "_DECREASED")
        else:
            codes.add(label + "_UNCHANGED")
    return sorted(codes)
def _evaluate_query(
    query: Query,
    view: LayoutView,
    table: TableManifest,
    removed: Sequence[str],
    baseline: dict[str, Any] | None,
) -> dict[str, Any]:
    current_analysis = analyze_predicate(
        query.predicate, table.column_types, table.partition_column_names, query.id)
    referenced = current_analysis["partition_columns"]
    retained = set(view.partition_column_names)
    lost = sorted(set(referenced) & set(removed))
    still = sorted(set(referenced) & retained)
    lost_all = bool(referenced) and not still
    analysis = analyze_predicate(
        query.predicate, view.column_types, view.partition_column_names, query.id)
    result = prune_partitions(view, query.predicate, analysis)
    partition_count = result["partition_count"]
    if lost_all:
        # The removed column carried the query's only partition filter.  The
        # merged layout cannot decide the predicate, so conservatively retain
        # every merged partition and recompute candidates from their real
        # file sets instead of simply zeroing the prune ratio.
        selected = [part.to_dict() for part in view.partitions]
        selected_count = len(view.partitions)
        unknown_count = partition_count
        conservative = True
    else:
        selected = result["selected_partitions"]
        selected_count = result["selected_count"]
        unknown_count = result["unknown_count"]
        conservative = False
    entry = {
        "query_id": query.id,
        "referenced_partition_columns": referenced,
        "retained_partition_filter_columns": still,
        "removed_partition_filter_columns": lost,
        "partition_count": partition_count,
        "selected_partition_count": selected_count,
        "unknown_partition_count": unknown_count,
        # Partition-count perspective, matching partition_pruner's prune_ratio.
        "partition_prune_ratio": _ratio(partition_count - selected_count, partition_count),
        "candidate_files": sum(item["file_count"] for item in selected),
        "candidate_bytes": sum(item["byte_size"] for item in selected),
        "candidate_rows": sum(item["row_count"] for item in selected),
        "conservative_all_partitions": conservative,
    }
    # File-based perspective with the same definition as the audit report's
    # query-level partition_prune_ratio, so the two can be compared directly.
    entry["candidate_file_prune_ratio"] = _ratio(
        table.file_count - entry["candidate_files"], table.file_count)
    if baseline is None:
        entry.update(partition_prune_ratio_delta=0.0, candidate_file_prune_ratio_delta=0.0,
                     candidate_files_delta=0, candidate_bytes_delta=0, reasons=[])
    else:
        entry["partition_prune_ratio_delta"] = round_float(
            entry["partition_prune_ratio"] - baseline["partition_prune_ratio"])
        entry["candidate_file_prune_ratio_delta"] = round_float(
            entry["candidate_file_prune_ratio"] - baseline["candidate_file_prune_ratio"])
        entry["candidate_files_delta"] = entry["candidate_files"] - baseline["candidate_files"]
        entry["candidate_bytes_delta"] = entry["candidate_bytes"] - baseline["candidate_bytes"]
        entry["reasons"] = _reasons(referenced, lost, lost_all, removed, baseline, entry)
    return entry
def _evaluate_layout(
    view: LayoutView,
    table: TableManifest,
    queries: Sequence[Query],
    removed: Sequence[str],
    baseline_queries: dict[str, dict[str, Any]] | None,
    merged_groups: int,
) -> dict[str, Any]:
    entries = [
        _evaluate_query(query, view, table, removed, (baseline_queries or {}).get(query.id))
        for query in queries
    ]
    layout: dict[str, Any] = {
        "removed_columns": list(removed),
        "remaining_columns": list(view.partition_column_names),
        **_layout_summary(view),
        "merged_group_count": merged_groups,
        "queries": entries,
        "totals": {
            "candidate_files": sum(item["candidate_files"] for item in entries),
            "candidate_bytes": sum(item["candidate_bytes"] for item in entries),
        },
    }
    if baseline_queries is not None:
        layout["totals"]["candidate_files_delta"] = sum(
            item["candidate_files_delta"] for item in entries
        )
        layout["totals"]["candidate_bytes_delta"] = sum(
            item["candidate_bytes_delta"] for item in entries
        )
    return layout
def _removal_subsets(columns: Sequence[ColumnDef]) -> list[tuple[str, ...]]:
    names = [column.name for column in columns]
    subsets = [combo for size in range(1, len(names) + 1)
               for combo in combinations(names, size)]
    return sorted(subsets, key=lambda combo: (len(combo), combo))
def evaluate_layouts(table: TableManifest, queries: Sequence[Query]) -> dict[str, Any]:
    """Evaluate the current layout and every counterfactual removal layout.

    Removal subsets are ordered by size and then by column name, so the output
    is deterministic for any declared partition-column order.
    """
    columns = table.partition_columns
    current_view = LayoutView("current", list(table.partition_column_names),
                               list(table.partitions), table.column_types)
    baseline = _evaluate_layout(current_view, table, queries, [], None, 0)
    baseline_queries = {item["query_id"]: item for item in baseline["queries"]}
    baseline["layout_id"] = "current"
    counterfactuals = []
    for removed in _removal_subsets(columns):
        removed_set = set(removed)
        retained = [column for column in columns if column.name not in removed_set]
        view, merged_groups = _build_view(table, retained, removed)
        layout = _evaluate_layout(view, table, queries, removed, baseline_queries, merged_groups)
        layout["layout_id"] = "without:" + ",".join(removed)
        counterfactuals.append(layout)
    total = len(table.partitions)
    single = sum(1 for part in table.partitions if part.file_count == 1)
    return {
        "schema_version": "1.0",
        "table": {
            "name": table.name,
            "partition_columns": list(table.partition_column_names),
            "partition_count": total,
            "file_count": table.file_count,
            "byte_size": table.byte_size,
            "single_file_partition_count": single,
            "single_file_partition_ratio": _ratio(single, total),
        },
        "columns": column_statistics(table),
        "layouts": [baseline, *counterfactuals],
    }
