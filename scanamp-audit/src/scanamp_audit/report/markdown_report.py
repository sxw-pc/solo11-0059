"""Human-readable Markdown report with escaped dynamic content."""
from __future__ import annotations
from pathlib import Path
from typing import Any
from ..models import AuditResult
from .format import json_pretty, md_code, md_escape

def _table(headers: list[str], rows: list[list[Any]]) -> list[str]:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    lines.extend("| " + " | ".join(md_escape(cell) for cell in row) + " |" for row in rows)
    return lines
def _query_section(query: dict[str, Any]) -> list[str]:
    metrics = query["metrics"]
    partition = query["partition"]
    lines = [
        f"### {md_escape(query['id'])}",
        "",
        f"- Description: {md_escape(query['description'])}",
        f"- Predicate: {md_code(query['where']) if query['where'] else '(none)'}",
        f"- Selected partitions: {partition['selected_count']} of {partition['partition_count']}",
        f"- Scanned: {metrics['scanned_files']} files / {metrics['scanned_bytes']} bytes / "
        f"{metrics['scanned_rows']} rows",
        f"- Scan amplification estimate: {md_escape(metrics['scan_amplification'])}",
        f"- Diagnostics: {query['diagnostic_count']}",
        "",
    ]
    if query["or_branches"]:
        info = query["or_branches"]
        lines.append(
            f"- OR branches: {info['branch_count']}, expansion factor "
            f"{md_escape(info['expansion_factor'])}"
        )
        lines.append("")
    lines.append("Partition decisions:")
    lines.append("")
    lines.extend(_table(["Partition", "Decision", "Reason"],
                        [[item["partition_id"], item["decision"], item["reason"]]
                         for item in partition["decisions"]]))
    lines.extend(["", "File decisions:", ""])
    lines.extend(_table(["File", "Decision", "Reason", "Missing stats"],
                        [[item["path"], item["decision"], item["reason"],
                          ", ".join(item["stats_missing_columns"]) or "-"]
                         for item in query["files"]["decisions"]]))
    lines.append("")
    return lines
def _layout_section(layout: dict[str, Any]) -> list[str]:
    lines = [f"### Layout {md_code(layout['layout_id'])}", ""]
    lines.extend(_table(
        ["Query", "Partition prune", "File prune", "Selected partitions",
         "Candidate files", "Files delta", "Candidate bytes", "Bytes delta", "Reasons"],
        [[item["query_id"], item["partition_prune_ratio"],
          item["candidate_file_prune_ratio"],
          f"{item['selected_partition_count']} of {item['partition_count']}",
          item["candidate_files"], item["candidate_files_delta"],
          item["candidate_bytes"], item["candidate_bytes_delta"],
          ", ".join(item["reasons"]) or "-"]
         for item in layout["queries"]],
    ))
    lines.append("")
    return lines
def _layout_evaluation_section(evaluation: dict[str, Any]) -> list[str]:
    if not evaluation:
        return []
    lines = ["## Partition layout evaluation", ""]
    lines.extend(_table(
        ["Column", "Type", "Cardinality", "Single-file partitions", "Share",
         "Files min/median/max", "Bytes min/median/max"],
        [[item["column"], item["type"], item["cardinality"],
          f"{item['single_file_partition_count']} of {item['cardinality']}",
          item["single_file_partition_ratio"],
          f"{item['file_count_distribution']['min']} / "
          f"{item['file_count_distribution']['median']} / "
          f"{item['file_count_distribution']['max']}",
          f"{item['byte_size_distribution']['min']} / "
          f"{item['byte_size_distribution']['median']} / "
          f"{item['byte_size_distribution']['max']}"]
         for item in evaluation["columns"]],
    ))
    lines.extend(["", "### Layout comparison", ""])
    lines.extend(_table(
        ["Layout", "Removed", "Remaining", "Partitions", "Merged groups", "Files",
         "Candidate files delta", "Candidate bytes delta"],
        [[item["layout_id"], ", ".join(item["removed_columns"]) or "-",
          ", ".join(item["remaining_columns"]) or "-", item["partition_count"],
          item["merged_group_count"], item["file_count"],
          item["totals"].get("candidate_files_delta", 0),
          item["totals"].get("candidate_bytes_delta", 0)]
         for item in evaluation["layouts"]],
    ))
    lines.append("")
    for layout in evaluation["layouts"]:
        lines.extend(_layout_section(layout))
    return lines
def render_markdown(result: AuditResult) -> str:
    summary = result.summary
    counts = summary["counts"]
    lines = [
        f"# Scan Amplification Audit: {md_escape(result.run_id)}",
        "",
        f"Generated: {md_escape(result.generated_at)}",
        "",
        "## Inputs and fingerprints",
        "",
        f"- Lake: {md_escape(result.inputs['lake_name'])}",
        f"- Workload: {md_escape(result.inputs['workload_file'])}",
        f"- Config: {md_escape(result.inputs['config_file'])}",
        f"- Manifest SHA-256: {md_code(summary['fingerprints']['manifest'])}",
        f"- Workload SHA-256: {md_code(summary['fingerprints']['workload'])}",
        f"- Config SHA-256: {md_code(summary['fingerprints']['config'])}",
        "",
        "## Summary",
        "",
    ]
    lines.extend(_table(
        ["Metric", "Value"],
        [
            ["Table", summary["table"]["name"]],
            ["Files / rows / bytes", f"{summary['table']['file_count']} / "
             f"{summary['table']['row_count']} / {summary['table']['byte_size']}"],
            ["Partitions", summary["table"]["partition_count"]],
            ["Queries", counts["queries"]],
            ["Diagnostics", f"{counts['diagnostics']} (critical {counts['critical']}, "
             f"warning {counts['warning']}, info {counts['info']})"],
            ["Scanned files", summary["scan"]["scanned_files"]],
            ["Scanned bytes", summary["scan"]["scanned_bytes"]],
            ["Estimated matched bytes", summary["scan"]["estimated_matched_bytes"]],
            ["Total scan amplification", summary["scan"]["scan_amplification"]],
        ],
    ))
    lines.extend(["", "## Scan amplification ranking", ""])
    lines.extend(_table(["Rank", "Query", "Scan amplification"],
                        [[index, item["id"], item["scan_amplification"]]
                         for index, item in enumerate(summary["ranking"], 1)]))
    lines.extend(["", "## Query metrics", ""])
    lines.extend(_table(
        ["Query", "Scanned files", "Scanned bytes", "Amplification",
         "Partition prune", "Stats prune", "Diagnostics"],
        [[item["id"], item["scanned_files"], item["scanned_bytes"],
          item["scan_amplification"], item["partition_prune_ratio"],
          item["stats_prune_ratio"], item["diagnostic_count"]]
         for item in summary["queries"]],
    ))
    lines.extend(["", "## Query details", ""])
    for query in result.queries:
        lines.extend(_query_section(query))
    lines.extend(_layout_evaluation_section(result.layout_evaluation))
    lines.extend(["## Diagnostics", ""])
    lines.extend(_table(
        ["ID", "Severity", "Rule", "Query", "Message"],
        [[item["id"], item["severity"], item["rule_id"], item["query_id"] or "-",
          item["message"]] for item in result.diagnostics],
    ))
    lines.append("")
    for item in result.diagnostics:
        lines.extend([
            f"### {md_escape(item['id'])}: {md_escape(item['rule_id'])}",
            "",
            f"- Severity: {md_escape(item['severity'])}",
            f"- Query: {md_escape(item['query_id'] or '-')}",
            f"- Message: {md_escape(item['message'])}",
            f"- Suggestion: {md_escape(item['suggestion'])}",
            "",
            "Evidence:",
            "",
            "```json",
            md_escape(json_pretty(item["evidence"])),
            "```",
            "",
        ])
    lines.extend([
        "## Reproduce",
        "",
        "```text",
        "python -m scanamp_audit audit --lake <lake> --workload <workload.yaml> "
        "--config <config.yaml> --ledger var/ledger.sqlite --out var --run-id <run-id>",
        "python -m scanamp_audit serve --ledger var/ledger.sqlite --host 127.0.0.1 --port 8765",
        "```",
        "",
    ])
    return "\n".join(lines)
def write_markdown(result: AuditResult, path: str | Path) -> None:
    Path(path).write_text(render_markdown(result), encoding="utf-8")
