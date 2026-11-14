"""Write the four archival artifacts for one audit run."""
from __future__ import annotations
import hashlib
from pathlib import Path
from typing import Any
from ..models import AuditResult
from ..schema import round_float
from .html_report import write_html
from .json_report import write_json, write_report_json
from .markdown_report import write_markdown

_ARTIFACTS = ("report.json", "report.md", "report.html", "summary.json")
def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()
def write_reports(result: AuditResult, run_dir: str | Path) -> list[dict[str, Any]]:
    target = Path(run_dir)
    target.mkdir(parents=True, exist_ok=True)
    write_report_json(result, target / "report.json")
    write_markdown(result, target / "report.md")
    write_html(result, target / "report.html")
    write_json(target / "summary.json", result.summary)
    artifacts = []
    for name in _ARTIFACTS:
        path = target / name
        artifacts.append(
            {
                "name": name,
                "sha256": _sha256(path),
                "byte_size": path.stat().st_size,
                "path": str(path),
            }
        )
    return artifacts
def amplification_delta(left: float, right: float) -> float:
    return round_float(left - right)
