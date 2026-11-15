from dataclasses import replace
from scanamp_audit.config import parse_config
from scanamp_audit.engine.diagnostics import evaluate_diagnostics

CASES = {
    "PARTITION_KEY_UNFILTERED": ("q02_data_only_event", "q01_partition_plus_amount"),
    "LOW_SELECTIVITY_PARTITION": ("q05_missing_user_stats", "q01_partition_plus_amount"),
    "HIGH_CARDINALITY_OVERPARTITION": ("q06_wide_date_range", "q01_partition_plus_amount"),
    "FUNCTION_WRAPPED_PARTITION": ("q03_function_dt", "q01_partition_plus_amount"),
    "STATS_MISSING": ("q05_missing_user_stats", "q02_data_only_event"),
    "OR_BRANCH_BLOWUP": ("q04_or_branches", "q01_partition_plus_amount"),
    "UNKNOWN_PREDICATE_CONSERVATIVE": ("q03_function_dt", "q02_data_only_event"),
}

def rules_for(result, query_id):
    return {item["rule_id"] for item in result.diagnostics if item["query_id"] == query_id}

def test_each_query_rule_has_hit_and_miss(demo_result):
    for rule_id, (hit, miss) in CASES.items():
        assert rule_id in rules_for(demo_result, hit), rule_id
        assert rule_id not in rules_for(demo_result, miss), rule_id

def test_path_mismatch_hit_and_miss(demo_table, demo_config):
    hits = [item for item in evaluate_diagnostics(demo_table, [], demo_config)
            if item.rule_id == "PATH_MANIFEST_MISMATCH"]
    assert hits and hits[0].evidence["column"] == "region"
    clean = replace(demo_table, path_mismatches=[])
    assert evaluate_diagnostics(clean, [], demo_config) == []

def test_rule_can_be_disabled(demo_table, demo_config, demo_result):
    config = parse_config({"diagnostics": {"FUNCTION_WRAPPED_PARTITION": {"enabled": False}},
                           "estimation": {"default_selectivity": 0.5},
                           "report": {"top_n": 10}})
    diagnostics = evaluate_diagnostics(demo_table, demo_result.queries, config)
    assert not any(item.rule_id == "FUNCTION_WRAPPED_PARTITION" for item in diagnostics)

def test_severities_and_sorted_order(demo_result):
    assert demo_result.summary["counts"]["critical"] == 2
    assert all(item["severity"] in {"info", "warning", "critical"}
               for item in demo_result.diagnostics)
    assert [item["id"] for item in demo_result.diagnostics] == [
        f"diag-{index:04d}" for index in range(1, len(demo_result.diagnostics) + 1)]
