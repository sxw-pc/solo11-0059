"""Audit orchestration: prune, measure, diagnose, summarize."""
from __future__ import annotations
from datetime import datetime, timezone
from typing import Any
from ..config import AuditConfig
from ..models import AuditResult, TableManifest, Workload
from ..schema import round_float
from ..workload import query_analysis
from .diagnostics import evaluate_diagnostics
from .file_pruner import prune_files
from .metrics import compute_metrics, compute_or_branches
from .partition_pruner import prune_partitions

SCHEMA_VERSION = "1.0"
def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
def _query_result(
    query: Any, table: TableManifest, config: AuditConfig
) -> dict[str, Any]:
    analysis = query_analysis(query, table)
    partition = prune_partitions(table, query.predicate, analysis)
    selected = set(partition.pop("selected_ids"))
    files = [
        item
        for part in table.partitions
        if part.partition_id in selected
        for item in part.files
    ]
    file_result = prune_files(table, files, query.predicate, config.default_selectivity,
                              analysis["columns"])
    metrics = compute_metrics(table, partition, file_result)
    branches = compute_or_branches(query.predicate, table, config.default_selectivity,
                                   analysis["columns"])
    return {
        "id": query.id,
        "description": query.description,
        "where": query.where,
        "predicate": query.predicate,
        "columns_referenced": analysis["columns"],
        "partition_columns_referenced": analysis["partition_columns"],
        "function_wrapped_partition_columns": analysis["function_wrapped_partition_columns"],
        "partition": partition,
        "files": file_result,
        "metrics": metrics,
        "or_branches": branches,
        "diagnostic_ids": [],
        "diagnostic_count": 0,
    }
def _summary(query_results: list[dict[str, Any]], table: TableManifest,
             diagnostics: list[dict[str, Any]], config: AuditConfig,
             run_id: str, fingerprints: dict[str, str]) -> dict[str, Any]:
    scanned_files = sum(item["metrics"]["scanned_files"] for item in query_results)
    scanned_bytes = sum(item["metrics"]["scanned_bytes"] for item in query_results)
    matched_bytes = sum(item["metrics"]["estimated_matched_bytes"] for item in query_results)
    queries = [
        {
            "id": item["id"],
            "where": item["where"],
            "scanned_files": item["metrics"]["scanned_files"],
            "scanned_bytes": item["metrics"]["scanned_bytes"],
            "scanned_rows": item["metrics"]["scanned_rows"],
            "estimated_matched_rows": item["metrics"]["estimated_matched_rows"],
            "estimated_matched_bytes": item["metrics"]["estimated_matched_bytes"],
            "scan_amplification": item["metrics"]["scan_amplification"],
            "partition_prune_ratio": item["metrics"]["partition_prune_ratio"],
            "stats_prune_ratio": item["metrics"]["stats_prune_ratio"],
            "selected_partitions": item["partition"]["selected_count"],
            "diagnostic_count": item["diagnostic_count"],
        }
        for item in query_results
    ]
    ranking = [
        {"id": item["id"], "scan_amplification": item["metrics"]["scan_amplification"]}
        for item in sorted(query_results, key=lambda q: (-q["metrics"]["scan_amplification"], q["id"]))
    ][: config.top_n]
    counts = {
        "queries": len(query_results),
        "diagnostics": len(diagnostics),
        "critical": sum(1 for item in diagnostics if item["severity"] == "critical"),
        "warning": sum(1 for item in diagnostics if item["severity"] == "warning"),
        "info": sum(1 for item in diagnostics if item["severity"] == "info"),
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "table": {
            "name": table.name,
            "file_count": table.file_count,
            "row_count": table.row_count,
            "byte_size": table.byte_size,
            "partition_count": len(table.partitions),
            "partition_columns": table.partition_column_names,
        },
        "fingerprints": fingerprints,
        "counts": counts,
        "scan": {
            "table_files": table.file_count,
            "table_bytes": table.byte_size,
            "table_rows": table.row_count,
            "scanned_files": scanned_files,
            "scanned_bytes": scanned_bytes,
            "estimated_matched_bytes": matched_bytes,
            "scan_amplification": min(round_float(scanned_bytes / max(matched_bytes, 1)), 1_000_000.0),
        },
        "queries": queries,
        "ranking": ranking,
        "diagnostics": [
            {"id": item["id"], "rule_id": item["rule_id"],
             "severity": item["severity"], "query_id": item["query_id"]}
            for item in diagnostics
        ],
    }
def run_audit(
    table: TableManifest,
    workload: Workload,
    config: AuditConfig,
    run_id: str,
    generated_at: str | None = None,
) -> AuditResult:
    query_results = [_query_result(query, table, config) for query in workload.queries]
    diagnostics = evaluate_diagnostics(table, query_results, config)
    serialized = [item.to_dict(f"diag-{index:04d}") for index, item in enumerate(diagnostics, 1)]
    for query in query_results:
        ids = [item["id"] for item in serialized if item["query_id"] == query["id"]]
        query["diagnostic_ids"] = ids
        query["diagnostic_count"] = len(ids)
    fingerprints = {
        "manifest": table.fingerprint,
        "workload": workload.fingerprint,
        "config": config.fingerprint,
    }
    summary = _summary(query_results, table, serialized, config, run_id, fingerprints)
    inputs = {
        "lake_name": table.source_name,
        "workload_file": workload.source_name,
        "config_file": config.source_name,
        "fingerprints": fingerprints,
    }
    return AuditResult(
        run_id=run_id,
        generated_at=generated_at or utc_now(),
        table=table,
        workload=workload,
        config=config,
        queries=query_results,
        diagnostics=serialized,
        summary=summary,
        inputs=inputs,
    )
