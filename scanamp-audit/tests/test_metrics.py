from helpers import by_query, plan
from scanamp_audit.engine.metrics import compute_metrics, ratio

def test_ratio_zero_division():
    assert ratio(1, 0) == 0.0
    assert ratio(1, 4) == 0.25

def test_query_metrics_are_consistent(demo_table):
    result = plan(demo_table, "dt BETWEEN '2024-01-01' AND '2024-01-02' "
                              "AND region = 'alpha' AND amount > 1000")
    metrics = compute_metrics(demo_table, result["partition"], result["files"])
    assert metrics["partition_prune_ratio"] == 0.5
    assert metrics["stats_prune_ratio"] == round(2 / 3, 6)
    assert metrics["total_prune_ratio"] == round(5 / 6, 6)
    assert metrics["scan_amplification"] >= 1

def test_summary_ranking_and_counts(demo_result):
    summary = demo_result.summary
    assert summary["counts"]["queries"] == 8
    assert summary["counts"]["diagnostics"] == len(demo_result.diagnostics)
    amps = [item["scan_amplification"] for item in summary["ranking"]]
    assert amps == sorted(amps, reverse=True)
    assert summary["ranking"][0]["id"] == "q07_null_status"
    assert summary["scan"]["scanned_files"] == sum(
        item["metrics"]["scanned_files"] for item in demo_result.queries)

def test_query_metric_fields(demo_result):
    query = by_query(demo_result, "q05_missing_user_stats")
    metrics = query["metrics"]
    assert metrics["scanned_files"] == 3
    assert metrics["partition_prune_ratio"] == 0.5
    assert metrics["stats_prune_ratio"] == 0.0
    assert metrics["scan_amplification"] == 2.0
