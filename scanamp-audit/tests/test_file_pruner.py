from helpers import plan
from scanamp_audit.engine.metrics import compute_or_branches

def decisions(result):
    return {item["path"]: item for item in result["files"]["decisions"]}

def test_equality_hits_and_misses(demo_table):
    result = plan(demo_table, "event_type = 'click'")
    assert result["files"]["scanned_files"] == 4
    assert result["files"]["dropped_files"] == 2
    assert decisions(result)["dt=2024-01-02/region=alpha/part-0001.csv"]["decision"] == "drop"

def test_amount_range_and_partition_intersection(demo_table):
    result = plan(demo_table, "dt BETWEEN '2024-01-01' AND '2024-01-02' "
                              "AND region = 'alpha' AND amount > 1000")
    assert result["files"]["scanned_files"] == 1
    assert result["files"]["scanned_bytes"] == 94

def test_missing_stats_are_unknown_not_dropped(demo_table):
    result = plan(demo_table, "region = 'alpha' AND user_id = 42")
    assert result["files"]["scanned_files"] == 3
    assert result["files"]["unknown_files"] == 3
    assert result["files"]["stats_missing_columns"] == ["user_id"]
    assert result["files"]["stats_missing_file_count"] == 3

def test_null_semantics(demo_table):
    result = plan(demo_table, "status IS NULL")
    assert result["files"]["scanned_files"] == 3
    not_null = plan(demo_table, "status IS NOT NULL")
    assert not_null["files"]["scanned_files"] == 6

def test_not_is_negation_normalized(demo_table):
    result = plan(demo_table, "NOT (amount > 1000)")
    assert result["files"]["scanned_files"] == 6
    assert result["files"]["dropped_files"] == 0

def test_in_between_and_neq(demo_table):
    result = plan(demo_table, "region IN ('alpha', 'beta') AND event_type != 'view' "
                              "AND user_id BETWEEN 10 AND 500")
    assert result["files"]["scanned_files"] == 3
    assert "dt=2024-01-02/region=alpha/part-0001.csv" in {
        path for path, item in decisions(result).items() if item["decision"] == "drop"}

def test_or_branch_expansion(demo_table):
    where = ("(dt = '2024-01-01' AND region = 'alpha' AND amount > 4000) "
             "OR (dt = '2024-01-03' AND amount < 1000)")
    result = plan(demo_table, where)
    info = compute_or_branches(result["predicate"], demo_table, 0.5, result["analysis"]["columns"])
    assert info["branch_count"] == 2
    assert info["union_scanned_files"] == 2
    assert info["expansion_factor"] == 2.0

def test_no_predicate_scans_everything(demo_table):
    result = plan(demo_table, None)
    assert result["files"]["scanned_files"] == 6
    assert result["files"]["estimated_matched_bytes"] == result["files"]["scanned_bytes"]
