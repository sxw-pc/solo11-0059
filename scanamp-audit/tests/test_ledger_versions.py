import json
from dataclasses import replace

import pytest
import yaml

from conftest import CONFIG, LAKE, WORKLOAD
from scanamp_audit.cli import main
from scanamp_audit.engine.auditor import run_audit
from scanamp_audit.errors import LedgerError
from scanamp_audit.ledger import Ledger
from scanamp_audit.workload import load_workload

QUERY_IDS = [
    "q01_partition_plus_amount",
    "q02_data_only_event",
    "q03_function_dt",
    "q04_or_branches",
    "q05_missing_user_stats",
    "q06_wide_date_range",
    "q07_null_status",
    "q08_in_and_between",
]

def record(path, result):
    ledger = Ledger(path, allow_outside=True)
    ledger.record_run(result, [])
    ledger.close()

def query_by_id(result, query_id):
    return next(item for item in result.queries if item["id"] == query_id)

def test_snapshots_retained_with_incrementing_versions(
    demo_result, demo_table, demo_workload, demo_config, tmp_path
):
    path = tmp_path / "ledger.sqlite"
    record(path, demo_result)
    ledger = Ledger(path, allow_outside=True)
    assert ledger.list_versions("demo-run") == [
        {"version": 1, "kind": "current", "created_at": "2024-01-01T00:00:00Z",
         "recorded_at": "2024-01-01T00:00:00Z"}
    ]
    for day in ("2024-01-02T00:00:00Z", "2024-01-03T00:00:00Z"):
        ledger.record_run(
            run_audit(demo_table, demo_workload, demo_config, "demo-run", day), []
        )
    versions = ledger.list_versions("demo-run")
    assert [item["version"] for item in versions] == [1, 2, 3]
    assert [item["kind"] for item in versions] == ["snapshot", "snapshot", "current"]
    assert [item["created_at"] for item in versions] == [
        "2024-01-01T00:00:00Z", "2024-01-02T00:00:00Z", "2024-01-03T00:00:00Z",
    ]
    assert all(item["recorded_at"] >= item["created_at"] for item in versions)
    rows = ledger.connection.execute(
        "SELECT COUNT(*) AS count FROM run_versions WHERE run_id = 'demo-run'"
    ).fetchone()
    assert rows["count"] == 2 and len(ledger.recent_runs()) == 1
    ledger.close()

def test_snapshot_is_immutable_and_preserves_pruning_detail(
    demo_result, demo_table, demo_workload, demo_config, tmp_path
):
    path = tmp_path / "ledger.sqlite"
    record(path, demo_result)
    record(path, demo_result)
    ledger = Ledger(path, allow_outside=True, read_only=True)
    snapshot_v1 = ledger.get_version("demo-run", 1)
    assert [query["query_id"] for query in snapshot_v1["queries"]] == QUERY_IDS
    snapshot = snapshot_v1["queries"][0]
    source = query_by_id(demo_result, "q01_partition_plus_amount")
    assert snapshot["metrics"] == source["metrics"]
    assert snapshot["partition_decisions"] == sorted(
        source["partition"]["decisions"], key=lambda item: item["partition_id"]
    )
    assert snapshot["file_decisions"] == sorted(
        source["files"]["decisions"], key=lambda item: item["path"]
    )
    assert {item["decision"] for item in snapshot["partition_decisions"]} == {
        "drop", "unknown"
    }
    assert {item["decision"] for item in snapshot["file_decisions"]} == {"keep", "drop"}
    # Aliases and numeric addressing agree.
    assert ledger.get_version("demo-run", "current") == ledger.get_version("demo-run", 2)
    assert ledger.get_version("demo-run", "latest") == ledger.get_version("demo-run", 2)
    assert ledger.get_version("demo-run", "previous") == snapshot_v1
    # A later overwrite neither mutates nor cascades away the archived version.
    ledger.close()
    third = run_audit(demo_table, demo_workload, demo_config, "demo-run",
                      "2024-01-03T00:00:00Z")
    record(path, third)
    ledger = Ledger(path, allow_outside=True, read_only=True)
    assert ledger.get_version("demo-run", 1) == snapshot_v1
    ledger.close()

def modified_workload(tmp_path):
    data = yaml.safe_load(WORKLOAD.read_text(encoding="utf-8"))
    for query in data["queries"]:
        if query["id"] == "q01_partition_plus_amount":
            query["where"] = (
                "dt >= '2024-01-02' AND region IN ('alpha', 'gamma') AND amount > 1000"
            )
    data["queries"] = [
        query for query in data["queries"] if query["id"] != "q08_in_and_between"
    ]
    path = tmp_path / "workload_v2.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path

def test_diff_reconstructs_partition_and_scan_changes(
    demo_result, demo_table, demo_config, tmp_path
):
    path = tmp_path / "ledger.sqlite"
    record(path, demo_result)
    workload_v2 = load_workload(modified_workload(tmp_path), demo_table)
    second = run_audit(demo_table, workload_v2, demo_config, "demo-run",
                       "2024-01-02T00:00:00Z")
    record(path, second)
    # Read-only connection, no report files anywhere: the diff is ledger-only.
    ledger = Ledger(path, allow_outside=True, read_only=True)
    diff = ledger.diff_versions("demo-run")
    assert diff["run_id"] == "demo-run"
    assert diff["old"]["version"] == 1 and diff["old"]["kind"] == "snapshot"
    assert diff["new"]["version"] == 2 and diff["new"]["kind"] == "current"
    assert [entry["query_id"] for entry in diff["queries"]] == [
        "q01_partition_plus_amount", "q08_in_and_between",
    ]
    entry = diff["queries"][0]
    assert entry["change"] == "decisions_changed"
    assert entry["added_partitions"] == ["dt=2024-01-03/region=gamma"]
    assert entry["removed_partitions"] == ["dt=2024-01-01/region=alpha"]
    old_metrics = query_by_id(demo_result, "q01_partition_plus_amount")["metrics"]
    new_metrics = query_by_id(second, "q01_partition_plus_amount")["metrics"]
    assert entry["scanned_files"] == {
        "old": old_metrics["scanned_files"],
        "new": new_metrics["scanned_files"],
        "delta": new_metrics["scanned_files"] - old_metrics["scanned_files"],
    }
    assert entry["scanned_bytes"]["delta"] == (
        new_metrics["scanned_bytes"] - old_metrics["scanned_bytes"]
    )
    removed = diff["queries"][1]
    assert removed["change"] == "removed" and removed["scanned_bytes"]["new"] is None
    assert diff["removed_queries"] == ["q08_in_and_between"]
    assert diff["added_queries"] == [] and diff["estimate_only_queries"] == []
    assert diff["unchanged_queries"] == QUERY_IDS[1:7]
    assert diff["totals"]["scanned_bytes"]["delta"] == (
        diff["totals"]["scanned_bytes"]["new"] - diff["totals"]["scanned_bytes"]["old"]
    )
    # Deterministic: a fresh connection rebuilds the identical diff.
    again = Ledger(path, allow_outside=True, read_only=True)
    assert again.diff_versions("demo-run", 1, 2) == diff
    assert again.diff_versions("demo-run", "previous", "latest") == diff
    assert json.dumps(diff, sort_keys=True) == json.dumps(
        again.diff_versions("demo-run"), sort_keys=True
    )
    again.close()
    ledger.close()

def test_estimate_only_change_detected(demo_result, demo_table, demo_workload,
                                       demo_config, tmp_path):
    path = tmp_path / "ledger.sqlite"
    record(path, demo_result)
    tuned = replace(demo_config, default_selectivity=0.9)
    record(path, run_audit(demo_table, demo_workload, tuned, "demo-run",
                           "2024-01-02T00:00:00Z"))
    ledger = Ledger(path, allow_outside=True, read_only=True)
    diff = ledger.diff_versions("demo-run")
    assert diff["queries"]  # the estimate change is visible
    assert {entry["change"] for entry in diff["queries"]} == {"estimate_only"}
    assert "q05_missing_user_stats" in diff["estimate_only_queries"]
    entry = next(
        item for item in diff["queries"]
        if item["query_id"] == "q05_missing_user_stats"
    )
    assert entry["added_partitions"] == [] and entry["removed_partitions"] == []
    assert entry["partition_decisions"] == [] and entry["file_decisions"] == []
    assert entry["scanned_files"]["delta"] == 0
    assert entry["scanned_bytes"]["delta"] == 0
    assert entry["estimated_matched_bytes"]["delta"] > 0
    assert diff["totals"]["scanned_bytes"]["delta"] == 0
    assert diff["totals"]["estimated_matched_bytes"]["delta"] > 0
    ledger.close()

def test_version_lookup_errors(demo_result, tmp_path):
    path = tmp_path / "ledger.sqlite"
    record(path, demo_result)
    ledger = Ledger(path, allow_outside=True)
    assert ledger.list_versions("unknown-run") == []
    assert ledger.get_version("unknown-run", 1) is None
    assert ledger.diff_versions("unknown-run") is None
    with pytest.raises(LedgerError) as error:
        ledger.diff_versions("demo-run")
    assert error.value.reason == "VERSION_NOT_FOUND"
    for bad in ("previous", 2, 0, True, "sideways"):
        with pytest.raises(LedgerError):
            ledger.get_version("demo-run", bad)
    ledger.close()

def test_audit_twice_then_versions_and_diff_from_ledger(tmp_path):
    ledger_path = tmp_path / "ledger.sqlite"
    command = [
        "audit", "--lake", str(LAKE), "--workload", str(WORKLOAD),
        "--config", str(CONFIG), "--ledger", str(ledger_path),
        "--out", str(tmp_path / "out"), "--run-id", "drift-demo",
        "--allow-ledger-outside-workdir",
    ]
    assert main(command) == 0
    assert main(command) == 0
    ledger = Ledger(ledger_path, allow_outside=True, read_only=True)
    versions = ledger.list_versions("drift-demo")
    assert [item["version"] for item in versions] == [1, 2]
    assert [item["kind"] for item in versions] == ["snapshot", "current"]
    diff = ledger.diff_versions("drift-demo", 1, 2)
    assert diff["queries"] == []
    assert diff["unchanged_queries"] == QUERY_IDS
    assert diff["totals"]["scanned_bytes"]["delta"] == 0
    ledger.close()
