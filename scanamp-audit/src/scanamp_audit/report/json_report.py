"""Machine-readable report assembly and deterministic JSON writing."""
from __future__ import annotations
import json
from pathlib import Path
from typing import Any
from ..models import AuditResult

def build_report(result: AuditResult) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "run_id": result.run_id,
        "generated_at": result.generated_at,
        "inputs": result.inputs,
        "config": result.config.to_dict(),
        "table": result.table.to_dict(),
        "queries": result.queries,
        "diagnostics": result.diagnostics,
        "summary": result.summary,
    }
def write_json(path: str | Path, data: Any) -> None:
    text = json.dumps(data, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    Path(path).write_text(text, encoding="utf-8")
def write_report_json(result: AuditResult, path: str | Path) -> None:
    write_json(path, build_report(result))
