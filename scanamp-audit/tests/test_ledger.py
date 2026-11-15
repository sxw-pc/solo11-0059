import sqlite3
import pytest
from conftest import ROOT
from scanamp_audit.errors import SecurityError
from scanamp_audit.ledger import Ledger
from scanamp_audit.report.renderer import write_reports

def open_ledger(path):
    return Ledger(path, allow_outside=True)

def test_record_query_and_idempotent_rerun(demo_result, tmp_path):
    path = tmp_path / "ledger.sqlite"
    artifacts = write_reports(demo_result, tmp_path / "run")
    ledger = open_ledger(path)
    ledger.record_run(demo_result, artifacts)
    ledger.record_run(demo_result, artifacts)
    runs = ledger.recent_runs()
    assert len(runs) == 1 and runs[0]["run_id"] == "demo-run"
    assert runs[0]["query_count"] == 8
    run = ledger.get_run("demo-run")
    assert len(run["queries"]) == 8
    assert len(run["diagnostics"]) == len(demo_result.diagnostics)
    assert {item["name"] for item in run["artifacts"]} == {
        "report.json", "report.md", "report.html", "summary.json"}
    assert run["fingerprints"]["manifest"] == demo_result.summary["fingerprints"]["manifest"]
    assert ledger.get_run("does-not-exist") is None
    ledger.close()

def test_read_only_ledger_rejects_writes(demo_ledger):
    ledger = Ledger(demo_ledger, allow_outside=True, read_only=True)
    with pytest.raises(sqlite3.OperationalError):
        ledger.connection.execute("CREATE TABLE forbidden (id INTEGER)")
    ledger.close()

def test_ledger_path_must_be_inside_workdir(tmp_path):
    with pytest.raises(SecurityError) as error:
        Ledger(tmp_path / "ledger.sqlite", cwd=ROOT)
    assert error.value.reason == "OUTSIDE_WORKDIR"

def test_invalid_run_id_rejected(demo_result, tmp_path):
    ledger = open_ledger(tmp_path / "ledger.sqlite")
    with pytest.raises(SecurityError):
        ledger.get_run("../escape")
    ledger.close()
