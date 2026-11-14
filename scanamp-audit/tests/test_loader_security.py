import shutil
import pytest
import yaml
from conftest import LAKE, WORKLOAD
from scanamp_audit.errors import ManifestError, SecurityError, WorkloadError
from scanamp_audit.loader import load_table
from scanamp_audit.security import normalize_manifest_path, safe_join
from scanamp_audit.workload import load_workload

def copy_lake(tmp_path):
    target = tmp_path / "lake"
    shutil.copytree(LAKE, target)
    return target

def rewrite(root, mutate):
    path = root / "_metadata" / "manifest.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    mutate(data)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

def test_demo_totals_and_path_mismatch(demo_table):
    assert demo_table.file_count == 6
    assert len(demo_table.partitions) == 5
    assert demo_table.row_count == 570
    assert demo_table.byte_size == 497
    assert len(demo_table.path_mismatches) == 1
    assert demo_table.path_mismatches[0]["declared"] == "delta"
    assert demo_table.fingerprint == load_table(LAKE).fingerprint

def test_path_traversal_and_absolute_rejected(tmp_path):
    for raw, reason in [("../escape.csv", "PATH_TRAVERSAL"),
                        ("C:/secret.csv", "ABSOLUTE_PATH"),
                        ("/tmp/x.csv", "ABSOLUTE_PATH")]:
        with pytest.raises(SecurityError) as error:
            normalize_manifest_path(raw)
        assert error.value.reason == reason
    with pytest.raises(SecurityError):
        safe_join(LAKE, "dt=2024-01-01/../../x.csv")
    assert safe_join(LAKE, "dt=2024-01-01/region=alpha/part-0001.csv").is_file()

def test_manifest_path_failures(tmp_path):
    lake = copy_lake(tmp_path)
    rewrite(lake, lambda data: data["files"][0].__setitem__("path", "../escape.csv"))
    with pytest.raises(ManifestError) as error:
        load_table(lake)
    assert error.value.reason == "PATH_TRAVERSAL"
    lake = copy_lake(tmp_path / "missing")
    rewrite(lake, lambda data: data["files"][0].__setitem__(
        "path", "dt=2024-01-01/region=alpha/nope.csv"))
    with pytest.raises(ManifestError) as error:
        load_table(lake)
    assert error.value.reason == "FILE_NOT_FOUND"

def test_size_schema_and_stats_failures(tmp_path):
    changes = [
        (lambda data: data["files"][0].__setitem__("byte_size", 1), "BYTE_SIZE_MISMATCH"),
        (lambda data: data["files"][0]["partition_values"].__setitem__("extra", "x"),
         "PARTITION_SCHEMA_MISMATCH"),
        (lambda data: data["files"][0]["columns"].__setitem__(
            "unknown", {"type": "string", "min": "a", "max": "b"}), "UNKNOWN_COLUMN"),
        (lambda data: data["files"][0]["columns"]["amount"].__setitem__("type", "string"),
         "TYPE_MISMATCH"),
    ]
    for mutate, reason in changes:
        lake = copy_lake(tmp_path / reason)
        rewrite(lake, mutate)
        with pytest.raises(ManifestError) as error:
            load_table(lake)
        assert error.value.reason == reason

def test_workload_parsing_and_errors(demo_table, tmp_path):
    workload = load_workload(WORKLOAD, demo_table)
    assert len(workload.queries) == 8
    assert workload.queries[0].id.startswith("q01")
    assert workload.queries[0].predicate["node"] == "and"
    def write(name, text):
        path = tmp_path / name
        path.write_text(text, encoding="utf-8")
        return path
    with pytest.raises(WorkloadError) as error:
        load_workload(write("unknown.yaml", "queries:\n  - id: q1\n    where: nope = 1\n"),
                      demo_table)
    assert error.value.reason == "UNKNOWN_COLUMN"
    with pytest.raises(WorkloadError) as error:
        load_workload(write("dup.yaml", "queries:\n  - id: q1\n  - id: q1\n"), demo_table)
    assert error.value.reason == "DUPLICATE_QUERY_ID"
    with pytest.raises(WorkloadError) as error:
        load_workload(write("conflict.yaml",
                            "queries:\n  - id: q1\n    where: amount = 1\n"
                            "    predicate: amount = 2\n"), demo_table)
    assert error.value.reason == "CONFLICTING_PREDICATE"
    with pytest.raises(WorkloadError):
        load_workload(write("bad.yaml", "queries:\n  - id: q1\n    where: 'amount ='\n"),
                      demo_table)
