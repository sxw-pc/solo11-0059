import json
from conftest import GOLDEN
from scanamp_audit.engine.auditor import run_audit

def load(name):
    return json.loads((GOLDEN / name).read_text(encoding="utf-8"))

def test_summary_matches_golden(demo_result):
    assert demo_result.summary == load("expected_summary.json")

def test_diagnostics_match_golden(demo_result):
    assert demo_result.summary["diagnostics"] == load("expected_diagnostics.json")

def test_summary_is_deterministic(demo_table, demo_workload, demo_config, demo_result):
    again = run_audit(demo_table, demo_workload, demo_config, "demo-run", "2030-12-31T23:59:59Z")
    assert again.summary == demo_result.summary
    assert again.generated_at != demo_result.generated_at
