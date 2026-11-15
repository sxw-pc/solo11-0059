"""Configurable scan-amplification diagnostic rules."""
from __future__ import annotations
from typing import Any
from ..models import AuditConfig, Diagnostic, TableManifest

def _make(rule_id: str, config: AuditConfig, message: str, suggestion: str,
          query_id: str | None = None, evidence: dict[str, Any] | None = None) -> Diagnostic:
    return Diagnostic(rule_id, config.rule(rule_id).severity, message, suggestion,
                      query_id, evidence or {})
def _partition_unfiltered(q: dict[str, Any], table: TableManifest, config: AuditConfig):
    if q["partition_columns_referenced"]:
        return None
    return _make(
        "PARTITION_KEY_UNFILTERED", config,
        "Query does not filter any partition column, so every partition remains a candidate.",
        "Add a predicate on a partition column (for example dt or region) to enable pruning.",
        q["id"],
        {"partition_columns": table.partition_column_names,
         "referenced_columns": q["columns_referenced"], "where": q["where"]},
    )
def _low_selectivity(q: dict[str, Any], table: TableManifest, config: AuditConfig):
    cfg = config.rule("LOW_SELECTIVITY_PARTITION")
    metrics = q["metrics"]
    scanned_ratio = metrics["scanned_files"] / metrics["table_files"] if metrics["table_files"] else 0.0
    if (metrics["partition_prune_ratio"] >= cfg.thresholds["min_partition_prune_ratio"]
            and scanned_ratio >= cfg.thresholds["min_scanned_file_ratio"]
            and metrics["scanned_files"] >= cfg.thresholds["min_scanned_files"]):
        return _make(
            "LOW_SELECTIVITY_PARTITION", config,
            "Partition pruning is effective but statistics pruning still scans many files.",
            "Add or tighten min/max statistics on the filter column, or repartition by the filter key.",
            q["id"],
            {"partition_prune_ratio": metrics["partition_prune_ratio"],
             "scanned_file_ratio": round(scanned_ratio, 6),
             "scanned_files": metrics["scanned_files"],
             "table_files": metrics["table_files"]},
        )
    return None
def _overpartitioned(q: dict[str, Any], table: TableManifest, config: AuditConfig):
    cfg = config.rule("HIGH_CARDINALITY_OVERPARTITION")
    total = len(table.partitions)
    selected = q["partition"]["selected_count"]
    selected_ratio = selected / total if total else 0.0
    if (total >= cfg.thresholds["min_partition_count"]
            and selected_ratio >= cfg.thresholds["min_selected_partition_ratio"]
            and selected >= cfg.thresholds["min_selected_partitions"]):
        return _make(
            "HIGH_CARDINALITY_OVERPARTITION", config,
            "A high-cardinality partition layout still leaves many partitions per query.",
            "Review the partition key grain; consider a coarser key or bucketing.",
            q["id"],
            {"partition_count": total, "selected_partitions": selected,
             "selected_ratio": round(selected_ratio, 6),
             "partition_columns": table.partition_column_names},
        )
    return None
def _function_wrapped(q: dict[str, Any], table: TableManifest, config: AuditConfig):
    wrapped = q["function_wrapped_partition_columns"]
    if not wrapped:
        return None
    return _make(
        "FUNCTION_WRAPPED_PARTITION", config,
        "A partition column is wrapped in a function and cannot be statically pruned.",
        "Filter partition columns with bare comparisons, or precompute the derived value as a column.",
        q["id"],
        {"functions": wrapped, "where": q["where"],
         "partition_columns": table.partition_column_names},
    )
def _stats_missing(q: dict[str, Any], table: TableManifest, config: AuditConfig):
    cfg = config.rule("STATS_MISSING")
    files = q["files"]
    columns = files["stats_missing_columns"]
    if len(columns) >= cfg.thresholds["min_missing_filter_columns"] and columns:
        return _make(
            "STATS_MISSING", config,
            "Filter columns lack usable min/max statistics, forcing conservative file retention.",
            "Collect sidecar min/max statistics for these columns or refresh stale statistics.",
            q["id"],
            {"columns": columns, "affected_files": files["stats_missing_file_count"],
             "scanned_files": files["scanned_files"]},
        )
    return None
def _or_blowup(q: dict[str, Any], table: TableManifest, config: AuditConfig):
    cfg = config.rule("OR_BRANCH_BLOWUP")
    info = q["or_branches"]
    if (info and info["branch_count"] >= cfg.thresholds["min_branches"]
            and info["expansion_factor"] >= cfg.thresholds["min_expansion_factor"]):
        return _make(
            "OR_BRANCH_BLOWUP", config,
            "OR branches expand the candidate set well beyond the largest single branch.",
            "Consider splitting the query into per-branch reads or rewriting the OR as a union of filters.",
            q["id"],
            {"branch_count": info["branch_count"],
             "branch_scanned_files": [item["scanned_files"] for item in info["branches"]],
             "union_scanned_files": info["union_scanned_files"],
             "expansion_factor": info["expansion_factor"]},
        )
    return None
def _unknown_conservative(q: dict[str, Any], table: TableManifest, config: AuditConfig):
    partition = q["partition"]
    if not (partition["all_selected"] and partition["undecidable_reasons"]):
        return None
    return _make(
        "UNKNOWN_PREDICATE_CONSERVATIVE", config,
        "An undecidable partition predicate forces all partitions to be scanned.",
        "Replace function-wrapped or non-literal partition predicates with static comparisons.",
        q["id"],
        {"reasons": partition["undecidable_reasons"],
         "selected_partitions": partition["selected_count"],
         "total_partitions": partition["partition_count"], "where": q["where"]},
    )
def _path_mismatches(table: TableManifest, config: AuditConfig) -> list[Diagnostic]:
    return [
        _make(
            "PATH_MANIFEST_MISMATCH", config,
            f"Path partition value disagrees with the manifest for file {item['file']}.",
            "Make the Hive-style directory names and manifest partition_values agree.",
            None,
            dict(item),
        )
        for item in table.path_mismatches
    ]
_RULES = (_partition_unfiltered, _low_selectivity, _overpartitioned, _function_wrapped,
          _stats_missing, _or_blowup, _unknown_conservative)
def evaluate_diagnostics(
    table: TableManifest, queries: list[dict[str, Any]], config: AuditConfig
) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    for query in queries:
        for rule in _RULES:
            rule_id = {
                _partition_unfiltered: "PARTITION_KEY_UNFILTERED",
                _low_selectivity: "LOW_SELECTIVITY_PARTITION",
                _overpartitioned: "HIGH_CARDINALITY_OVERPARTITION",
                _function_wrapped: "FUNCTION_WRAPPED_PARTITION",
                _stats_missing: "STATS_MISSING",
                _or_blowup: "OR_BRANCH_BLOWUP",
                _unknown_conservative: "UNKNOWN_PREDICATE_CONSERVATIVE",
            }[rule]
            if not config.enabled(rule_id):
                continue
            diagnostic = rule(query, table, config)
            if diagnostic is not None:
                diagnostics.append(diagnostic)
    if config.enabled("PATH_MANIFEST_MISMATCH"):
        diagnostics.extend(_path_mismatches(table, config))
    diagnostics.sort(key=lambda item: item.sort_key())
    return diagnostics
