"""Local, read-only Flask workbench backed by the SQLite ledger."""
from __future__ import annotations
from pathlib import Path
from typing import Any
from flask import Flask, Response, jsonify, render_template_string
from .errors import ScanAmpError
from .ledger import Ledger

_CSS = """
:root{color-scheme:light dark;--bg:#f7f8fa;--fg:#17202a;--muted:#5b6770;--card:#fff;
--line:#d9dee3;--accent:#155e75;--crit:#9b1c1c;--warn:#8a5a00}
@media (prefers-color-scheme:dark){:root{--bg:#11161a;--fg:#e7ecef;--muted:#a3b0b8;
--card:#1a2228;--line:#303c44;--accent:#71d1e8;--crit:#ff8f8f;--warn:#ffd479}}
*{box-sizing:border-box}body{margin:0;padding:2rem;background:var(--bg);color:var(--fg);
font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1100px;margin:0 auto}h1,h2{line-height:1.2}h2{border-bottom:1px solid var(--line);
padding-bottom:.3rem}table{width:100%;border-collapse:collapse;margin:1rem 0;background:var(--card)}
th,td{text-align:left;border:1px solid var(--line);padding:.4rem .55rem;vertical-align:top}
th{background:color-mix(in srgb,var(--accent) 10%,var(--card))}
a{color:var(--accent)}code,pre{font-family:ui-monospace,Consolas,monospace}
pre{white-space:pre-wrap;background:var(--card);border:1px solid var(--line);padding:.7rem}
.muted{color:var(--muted)}.critical{color:var(--crit);font-weight:600}
.warning{color:var(--warn);font-weight:600}.nav{margin:1rem 0}.cards{display:flex;gap:.8rem;flex-wrap:wrap}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:.7rem 1rem}
""".strip()
_BASE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'">
<title>{{ title }} | scanamp-audit</title><style>{{ css }}</style></head>
<body><main><h1>{{ title }}</h1>
{% block body %}{% endblock %}
<p class="muted nav"><a href="/">Runs</a> | <a href="/healthz">Health</a></p>
<p class="muted">Local read-only ledger view. No external resources, no network access.</p>
</main></body></html>"""
_INDEX = _BASE.replace("{% block body %}{% endblock %}", """
{% if runs %}
<table><thead><tr><th>Run</th><th>Created</th><th>Table</th><th>Queries</th>
<th>Diagnostics</th><th>Scanned bytes</th><th>Amplification</th></tr></thead><tbody>
{% for run in runs %}<tr>
<td><a href="/runs/{{ run.run_id }}">{{ run.run_id }}</a></td>
<td>{{ run.created_at }}</td><td>{{ run.table_name }}</td><td>{{ run.query_count }}</td>
<td>{{ run.diagnostic_count }} (critical {{ run.critical_count }})</td>
<td>{{ run.scanned_bytes }}</td><td>{{ run.scan_amplification }}</td></tr>{% endfor %}
</tbody></table>
{% else %}<p>No runs have been recorded yet.</p>{% endif %}
""")
_RUN = _BASE.replace("{% block body %}{% endblock %}", """
{% if run %}
<div class="cards"><div class="card"><b>{{ run.query_count }}</b><br>queries</div>
<div class="card"><b>{{ run.diagnostic_count }}</b><br>diagnostics</div>
<div class="card"><b>{{ run.critical_count }}</b><br>critical</div>
<div class="card"><b>{{ run.scanned_bytes }}</b><br>scanned bytes</div></div>
<h2>Summary</h2>
<table><tbody>
<tr><th>Table</th><td>{{ run.table_name }}</td></tr>
<tr><th>Lake</th><td>{{ run.lake_name }}</td></tr>
<tr><th>Workload</th><td>{{ run.workload_file }}</td></tr>
<tr><th>Config</th><td>{{ run.config_file }}</td></tr>
<tr><th>Created</th><td>{{ run.created_at }}</td></tr>
<tr><th>Table files / bytes</th><td>{{ run.table_files }} / {{ run.table_bytes }}</td></tr>
<tr><th>Scanned files / bytes</th><td>{{ run.scanned_files }} / {{ run.scanned_bytes }}</td></tr>
<tr><th>Estimated matched bytes</th><td>{{ run.estimated_matched_bytes }}</td></tr>
<tr><th>Scan amplification</th><td>{{ run.scan_amplification }}</td></tr>
</tbody></table>
<h2>Queries</h2>
<table><thead><tr><th>Query</th><th>Scanned files</th><th>Scanned bytes</th>
<th>Amplification</th><th>Partition prune</th><th>Stats prune</th><th>Diagnostics</th></tr></thead><tbody>
{% for q in run.queries %}<tr><td>{{ q.query_id }}</td><td>{{ q.metrics.scanned_files }}</td>
<td>{{ q.metrics.scanned_bytes }}</td><td>{{ q.metrics.scan_amplification }}</td>
<td>{{ q.metrics.partition_prune_ratio }}</td><td>{{ q.metrics.stats_prune_ratio }}</td>
<td>{{ q.diagnostic_count }}</td></tr>{% endfor %}</tbody></table>
<h2>Diagnostics</h2>
{% if run.diagnostics %}<table><thead><tr><th>ID</th><th>Severity</th><th>Rule</th>
<th>Query</th><th>Message</th></tr></thead><tbody>
{% for d in run.diagnostics %}<tr><td>{{ d.id }}</td>
<td class="{{ d.severity }}">{{ d.severity }}</td><td>{{ d.rule_id }}</td>
<td>{{ d.query_id or "-" }}</td><td>{{ d.message }}</td></tr>{% endfor %}
</tbody></table>
{% for d in run.diagnostics %}<h3>{{ d.id }}: {{ d.rule_id }}</h3>
<p><strong>Suggestion:</strong> {{ d.suggestion }}</p>
<pre>{{ d.evidence | tojson(indent=2) }}</pre>{% endfor %}
{% else %}<p>No diagnostics.</p>{% endif %}
<h2>Artifacts</h2>
<table><thead><tr><th>Name</th><th>SHA-256</th><th>Bytes</th></tr></thead><tbody>
{% for a in run.artifacts %}<tr><td>{{ a.name }}</td><td>{{ a.sha256 }}</td>
<td>{{ a.byte_size }}</td></tr>{% endfor %}</tbody></table>
<h2>Input fingerprints</h2>
<table><tbody>{% for kind, digest in run.fingerprints.items() %}
<tr><th>{{ kind }}</th><td>{{ digest }}</td></tr>{% endfor %}</tbody></table>
{% else %}<p>Run not found.</p>{% endif %}
""")
def create_app(
    ledger_path: str | Path,
    *,
    allow_outside: bool = False,
    cwd: Path | None = None,
) -> Flask:
    app = Flask(__name__, static_folder=None)
    app.jinja_env.autoescape = True
    ledger = Ledger(ledger_path, allow_outside=allow_outside, cwd=cwd, read_only=True)
    app.extensions["scanamp_ledger"] = ledger
    @app.before_request
    def readonly_methods() -> Response | None:
        from flask import request
        if request.method not in ("GET", "HEAD"):
            return jsonify({"error": "read_only", "method": request.method}), 405
        return None
    @app.after_request
    def security_headers(response: Response) -> Response:
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response
    @app.get("/healthz")
    def healthz() -> Any:
        try:
            runs = ledger.recent_runs(1)
        except ScanAmpError as exc:
            return jsonify({"status": "error", "error": exc.code, "network": "disabled"}), 500
        return jsonify({"status": "ok", "service": "scanamp-audit", "runs": len(runs),
                        "network": "disabled"})
    @app.get("/")
    def index() -> str:
        return render_template_string(_INDEX, title="Scan amplification runs",
                                      css=_CSS, runs=ledger.recent_runs(20))
    @app.get("/runs/<run_id>")
    def run_detail(run_id: str) -> tuple[str, int] | str:
        try:
            run = ledger.get_run(run_id)
        except ScanAmpError:
            return render_template_string(_BASE, title="Run not found", css=_CSS), 404
        if run is None:
            return render_template_string(_BASE, title="Run not found", css=_CSS), 404
        return render_template_string(_RUN, title=f"Run {run_id}", css=_CSS, run=run)
    return app
