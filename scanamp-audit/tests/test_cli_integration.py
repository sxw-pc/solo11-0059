import json
from conftest import CONFIG, GOLDEN, LAKE, WORKLOAD
from scanamp_audit.cli import main

def test_demo_command_generates_reports_and_ledger(tmp_path):
    out, ledger = tmp_path / "demo", tmp_path / "ledger.sqlite"
    code = main(["demo", "--out", str(out), "--ledger", str(ledger),
                 "--allow-ledger-outside-workdir"])
    assert code == 0
    assert (out / "report.json").is_file()
    assert (out / "report.md").is_file()
    assert (out / "report.html").is_file()
    assert (out / "summary.json").is_file()
    assert ledger.is_file()

def audit_cmd(tmp_path, fail_on):
    return main(["audit", "--lake", str(LAKE), "--workload", str(WORKLOAD),
                 "--config", str(CONFIG), "--ledger", str(tmp_path / "ledger.sqlite"),
                 "--out", str(tmp_path / "out"), "--run-id", "ci-run",
                 "--fail-on", fail_on, "--allow-ledger-outside-workdir"])

def test_audit_gate_and_default_run_id(tmp_path):
    assert audit_cmd(tmp_path, "warning") == 1
    assert audit_cmd(tmp_path, "none") == 0
    assert (tmp_path / "out" / "runs" / "ci-run" / "report.json").is_file()
    code = main(["audit", "--lake", str(LAKE), "--workload", str(WORKLOAD),
                 "--config", str(CONFIG), "--ledger", str(tmp_path / "ledger.sqlite"),
                 "--out", str(tmp_path / "out2"), "--fail-on", "none",
                 "--allow-ledger-outside-workdir"])
    assert code == 0
    run_dirs = list((tmp_path / "out2" / "runs").iterdir())
    assert len(run_dirs) == 1 and run_dirs[0].name.startswith("run-")

def test_golden_command(tmp_path):
    actual = tmp_path / "summary.json"
    actual.write_text((GOLDEN / "expected_summary.json").read_text(encoding="utf-8"),
                      encoding="utf-8")
    assert main(["golden", "--actual", str(actual),
                 "--expected", str(GOLDEN / "expected_summary.json")]) == 0
    data = json.loads(actual.read_text(encoding="utf-8"))
    data["run_id"] = "tampered"
    actual.write_text(json.dumps(data), encoding="utf-8")
    assert main(["golden", "--actual", str(actual),
                 "--expected", str(GOLDEN / "expected_summary.json")]) == 1

def test_invalid_inputs_return_nonzero(tmp_path):
    assert main(["audit", "--lake", str(tmp_path / "missing"), "--workload", str(WORKLOAD),
                 "--config", str(CONFIG), "--ledger", str(tmp_path / "l.sqlite"),
                 "--allow-ledger-outside-workdir"]) == 2
