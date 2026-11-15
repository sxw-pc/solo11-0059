"""Self-contained HTML report; every dynamic value is HTML-escaped."""
from __future__ import annotations
from pathlib import Path
from typing import Any
from ..models import AuditResult
from .format import html_escape, json_pretty

_CSS = """
:root{color-scheme:light dark;--bg:#f7f8fa;--fg:#17202a;--muted:#5b6770;--card:#fff;
--line:#d9dee3;--accent:#155e75;--crit:#9b1c1c;--warn:#8a5a00;--info:#14507a}
@media (prefers-color-scheme:dark){:root{--bg:#11161a;--fg:#e7ecef;--muted:#a3b0b8;
--card:#1a2228;--line:#303c44;--accent:#71d1e8;--crit:#ff8f8f;--warn:#ffd479;--info:#8fc7ff}}
*{box-sizing:border-box}body{margin:0;padding:2rem;background:var(--bg);color:var(--fg);
font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1180px;margin:0 auto}h1,h2,h3{line-height:1.2;margin:1.4em 0 .5em}
h1{margin-top:0;font-size:1.8rem}h2{border-bottom:1px solid var(--line);padding-bottom:.3rem}
table{width:100%;border-collapse:collapse;margin:.6rem 0 1.2rem;background:var(--card)}
th,td{text-align:left;border:1px solid var(--line);padding:.4rem .55rem;vertical-align:top}
th{background:color-mix(in srgb,var(--accent) 10%,var(--card))}
code,pre{font-family:ui-monospace,SFMono-Regular,Consolas,monospace}
code{overflow-wrap:anywhere}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:var(--card);
border:1px solid var(--line);padding:.7rem;border-radius:6px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:.8rem;margin:1rem 0}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:.8rem}
.card b{display:block;font-size:1.25rem}.muted{color:var(--muted)}
.critical{color:var(--crit);font-weight:600}.warning{color:var(--warn);font-weight:600}
.info{color:var(--info);font-weight:600}.mono{font-family:ui-monospace,Consolas,monospace}
footer{margin-top:2rem;color:var(--muted);font-size:.85rem}
""".strip()
def _table(headers: list[str], rows: list[list[Any]]) -> str:
    head = "".join(f"<th>{html_escape(item)}</th>" for item in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{html_escape(cell)}</td>" for cell in row) + "</tr>"
        for row in rows
    )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"
def _severity(value: str) -> str:
    return f'<span class="{html_escape(value)}">{html_escape(value)}</span>'
def render_html(result: AuditResult) -> str:
    summary = result.summary
    counts = summary["counts"]
    parts = [
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        '<meta http-equiv="Content-Security-Policy" '
        'content="default-src \'none\'; style-src \'unsafe-inline\'; frame-ancestors \'none\'">',
        f"<title>Scan Amplification Audit: {html_escape(result.run_id)}</title>",
        f"<style>{_CSS}</style></head><body><main>",
        f"<h1>Scan Amplification Audit: {html_escape(result.run_id)}</h1>",
        f'<p class="muted">Generated {html_escape(result.generated_at)} | offline static audit</p>',
        '<div class="cards">',
        f'<div class="card"><b>{html_escape(counts["queries"])}</b>queries</div>',
        f'<div class="card"><b>{html_escape(counts["diagnostics"])}</b>diagnostics</div>',
        f'<div class="card"><b>{html_escape(counts["critical"])}</b>critical</div>',
        f'<div class="card"><b>{html_escape(summary["scan"]["scanned_bytes"])}</b>scanned bytes</div>',
        f'<div class="card"><b>{html_escape(summary["scan"]["scan_amplification"])}</b>'
        "scan amplification</div>",
        "</div>",
        "<h2>Inputs</h2>",
        _table(["Input", "Value"], [
            ["Lake", result.inputs["lake_name"]],
            ["Workload", result.inputs["workload_file"]],
            ["Config", result.inputs["config_file"]],
            ["Manifest SHA-256", summary["fingerprints"]["manifest"]],
            ["Workload SHA-256", summary["fingerprints"]["workload"]],
            ["Config SHA-256", summary["fingerprints"]["config"]],
        ]),
        "<h2>Summary</h2>",
        _table(["Metric", "Value"], [
            ["Table", summary["table"]["name"]],
            ["Partition columns", ", ".join(summary["table"]["partition_columns"]) or "-"],
            ["Files", summary["table"]["file_count"]],
            ["Rows", summary["table"]["row_count"]],
            ["Bytes", summary["table"]["byte_size"]],
            ["Partitions", summary["table"]["partition_count"]],
            ["Scanned files", summary["scan"]["scanned_files"]],
            ["Scanned bytes", summary["scan"]["scanned_bytes"]],
            ["Estimated matched bytes", summary["scan"]["estimated_matched_bytes"]],
            ["Total scan amplification", summary["scan"]["scan_amplification"]],
        ]),
        "<h2>Scan amplification ranking</h2>",
        _table(["Rank", "Query", "Scan amplification"],
               [[index, item["id"], item["scan_amplification"]]
                for index, item in enumerate(summary["ranking"], 1)]),
        "<h2>Queries</h2>",
        _table(["Query", "Where", "Scanned files", "Scanned bytes", "Amplification",
                "Partition prune", "Stats prune"],
               [[item["id"], item["where"] or "-", item["scanned_files"],
                 item["scanned_bytes"], item["scan_amplification"],
                 item["partition_prune_ratio"], item["stats_prune_ratio"]]
                for item in summary["queries"]]),
    ]
    for query in result.queries:
        metrics, partition = query["metrics"], query["partition"]
        parts.extend([
            f"<h3>{html_escape(query['id'])}</h3>",
            f'<p class="mono">{html_escape(query["where"] or "(no predicate)")}</p>',
            _table(["Metric", "Value"], [
                ["Selected partitions", f"{partition['selected_count']} of {partition['partition_count']}"],
                ["Scanned files", metrics["scanned_files"]],
                ["Scanned bytes", metrics["scanned_bytes"]],
                ["Scanned rows", metrics["scanned_rows"]],
                ["Estimated matched rows", metrics["estimated_matched_rows"]],
                ["Scan amplification", metrics["scan_amplification"]],
                ["Diagnostics", query["diagnostic_count"]],
            ]),
        ])
        if query["or_branches"]:
            info = query["or_branches"]
            parts.append(
                "<p>OR branches: " + html_escape(info["branch_count"]) + ", expansion factor "
                + html_escape(info["expansion_factor"]) + "</p>"
            )
        parts.append("<h4>Partition decisions</h4>")
        parts.append(_table(["Partition", "Decision", "Reason"],
                            [[item["partition_id"], item["decision"], item["reason"]]
                             for item in partition["decisions"]]))
        parts.append("<h4>File decisions</h4>")
        parts.append(_table(["File", "Decision", "Reason", "Missing stats"],
                            [[item["path"], item["decision"], item["reason"],
                              ", ".join(item["stats_missing_columns"]) or "-"]
                             for item in query["files"]["decisions"]]))
    parts.extend(["<h2>Diagnostics</h2>"])
    for item in result.diagnostics:
        parts.extend([
            f"<h3>{html_escape(item['id'])}: {html_escape(item['rule_id'])}</h3>",
            f"<p>Severity: {_severity(item['severity'])} | Query: "
            f"{html_escape(item['query_id'] or '-')}</p>",
            f"<p>{html_escape(item['message'])}</p>",
            f"<p><strong>Suggestion:</strong> {html_escape(item['suggestion'])}</p>",
            f"<pre>{html_escape(json_pretty(item['evidence']))}</pre>",
        ])
    parts.extend([
        "<h2>Reproduce</h2>",
        '<pre>python -m scanamp_audit audit --lake &lt;lake&gt; --workload &lt;workload.yaml&gt; '
        '--config &lt;config.yaml&gt; --ledger var/ledger.sqlite --out var --run-id &lt;run-id&gt;</pre>',
        "<footer>Self-contained report. No external resources, no scripts, no network access.</footer>",
        "</main></body></html>",
    ])
    return "\n".join(parts) + "\n"
def write_html(result: AuditResult, path: str | Path) -> None:
    Path(path).write_text(render_html(result), encoding="utf-8")
