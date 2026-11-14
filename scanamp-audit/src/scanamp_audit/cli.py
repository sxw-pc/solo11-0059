"""Command line interface for scanamp-audit."""
from __future__ import annotations
import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any
from .config import fail_on_triggered, load_config
from .engine.auditor import run_audit
from .errors import ScanAmpError
from .ledger import Ledger
from .loader import load_table
from .report.renderer import write_reports
from .security import validate_local_host, validate_port, validate_run_id
from .server import create_app
from .workload import load_workload

def _fixtures_root() -> Path:
    candidates: list[Path] = []
    if os.environ.get("SCANAMP_FIXTURES"):
        candidates.append(Path(os.environ["SCANAMP_FIXTURES"]))
    cwd = Path.cwd()
    candidates.append(cwd / "fixtures")
    candidates.extend(parent / "fixtures" for parent in cwd.parents)
    candidates.append(Path(__file__).resolve().parents[2] / "fixtures")
    for candidate in candidates:
        if (candidate / "demo_lake" / "_metadata" / "manifest.yaml").is_file():
            return candidate
    raise ScanAmpError(
        "cannot locate fixtures/demo_lake; run from the project root or set SCANAMP_FIXTURES",
        field="fixtures",
        reason="FIXTURES_NOT_FOUND",
    )
def _execute(
    lake: str | Path,
    workload_path: str | Path,
    config_path: str | Path,
    ledger_path: str | Path,
    out: str | Path,
    *,
    run_id: str | None,
    direct_out: bool,
    allow_ledger_outside: bool,
) -> tuple[Any, Path]:
    table = load_table(lake)
    workload = load_workload(workload_path, table)
    config = load_config(config_path)
    if run_id is None:
        run_id = f"run-{table.fingerprint[:8]}-{workload.fingerprint[:8]}-{config.fingerprint[:8]}"
    validate_run_id(run_id)
    result = run_audit(table, workload, config, run_id)
    report_dir = Path(out) if direct_out else Path(out) / "runs" / run_id
    artifacts = write_reports(result, report_dir)
    ledger = Ledger(ledger_path, allow_outside=allow_ledger_outside)
    try:
        ledger.record_run(result, artifacts)
    finally:
        ledger.close()
    return result, report_dir
def _report(result: Any, report_dir: Path) -> None:
    print(
        f"run_id={result.run_id} report_dir={report_dir} queries={len(result.queries)} "
        f"diagnostics={len(result.diagnostics)} critical={result.critical_count()} "
        f"warning={result.warning_count()}"
    )
def _gate(result: Any, fail_on: str) -> int:
    if fail_on_triggered(result, fail_on):
        print(f"fail-on={fail_on}: diagnostic gate triggered", file=sys.stderr)
        return 1
    return 0
def cmd_demo(args: argparse.Namespace) -> int:
    root = _fixtures_root()
    result, report_dir = _execute(
        root / "demo_lake", root / "workloads" / "demo_workload.yaml",
        root / "configs" / "demo_audit.yaml", args.ledger, args.out,
        run_id=args.run_id, direct_out=True,
        allow_ledger_outside=args.allow_ledger_outside_workdir,
    )
    _report(result, report_dir)
    return _gate(result, args.fail_on)
def cmd_audit(args: argparse.Namespace) -> int:
    result, report_dir = _execute(
        args.lake, args.workload, args.config, args.ledger, args.out,
        run_id=args.run_id, direct_out=False,
        allow_ledger_outside=args.allow_ledger_outside_workdir,
    )
    _report(result, report_dir)
    return _gate(result, args.fail_on)
def cmd_serve(args: argparse.Namespace) -> int:
    host = validate_local_host(args.host)
    port = validate_port(args.port)
    app = create_app(args.ledger, allow_outside=args.allow_ledger_outside_workdir)
    print(f"serving scanamp-audit on http://{host}:{port}/ (read-only, local only)")
    app.run(host=host, port=port, debug=False, use_reloader=False)
    return 0
def _diff(actual: Any, expected: Any, path: str = "$", limit: int = 10) -> list[str]:
    if len(path) > limit:
        return []
    differences: list[str] = []
    if type(actual) is not type(expected):
        return [f"{path}: type {type(actual).__name__} != {type(expected).__name__}"]
    if isinstance(actual, dict):
        for key in sorted(set(actual) | set(expected)):
            if key not in actual:
                differences.append(f"{path}.{key}: missing in actual")
            elif key not in expected:
                differences.append(f"{path}.{key}: unexpected in actual")
            else:
                differences.extend(_diff(actual[key], expected[key], f"{path}.{key}", limit))
            if len(differences) >= limit:
                return differences[:limit]
        return differences
    if isinstance(actual, list):
        if len(actual) != len(expected):
            differences.append(f"{path}: length {len(actual)} != {len(expected)}")
        for index, (left, right) in enumerate(zip(actual, expected)):
            differences.extend(_diff(left, right, f"{path}[{index}]", limit))
            if len(differences) >= limit:
                return differences[:limit]
        return differences
    if actual != expected:
        differences.append(f"{path}: {actual!r} != {expected!r}")
    return differences
def cmd_golden(args: argparse.Namespace) -> int:
    actual_path, expected_path = Path(args.actual), Path(args.expected)
    if not actual_path.is_file() or not expected_path.is_file():
        print("golden: actual or expected file is missing", file=sys.stderr)
        return 2
    try:
        actual = json.loads(actual_path.read_text(encoding="utf-8"))
        expected = json.loads(expected_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"golden: cannot read JSON: {exc}", file=sys.stderr)
        return 2
    differences = _diff(actual, expected)
    if differences:
        print("golden mismatch:", file=sys.stderr)
        for item in differences:
            print(f"  {item}", file=sys.stderr)
        return 1
    print(f"golden OK: {actual_path}")
    return 0
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="scanamp-audit", description="Offline scan amplification auditor")
    sub = parser.add_subparsers(dest="command", required=True)
    demo = sub.add_parser("demo", help="run the bundled demo end to end")
    demo.add_argument("--out", default="var/demo-run")
    demo.add_argument("--ledger", default="var/ledger.sqlite")
    demo.add_argument("--run-id", default="demo-run")
    demo.add_argument("--fail-on", choices=["critical", "warning", "info", "none"], default="none")
    demo.add_argument("--allow-ledger-outside-workdir", action="store_true")
    demo.set_defaults(func=cmd_demo)
    audit = sub.add_parser("audit", help="audit a lake, workload and config")
    audit.add_argument("--lake", required=True)
    audit.add_argument("--workload", required=True)
    audit.add_argument("--config", required=True)
    audit.add_argument("--ledger", default="var/ledger.sqlite")
    audit.add_argument("--out", default="var")
    audit.add_argument("--run-id")
    audit.add_argument("--fail-on", choices=["critical", "warning", "info", "none"], default="none")
    audit.add_argument("--allow-ledger-outside-workdir", action="store_true")
    audit.set_defaults(func=cmd_audit)
    serve = sub.add_parser("serve", help="serve the read-only ledger viewer")
    serve.add_argument("--ledger", default="var/ledger.sqlite")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--allow-ledger-outside-workdir", action="store_true")
    serve.set_defaults(func=cmd_serve)
    golden = sub.add_parser("golden", help="compare a summary with a golden file")
    golden.add_argument("--actual", required=True)
    golden.add_argument("--expected", required=True)
    golden.set_defaults(func=cmd_golden)
    return parser
def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except ScanAmpError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
