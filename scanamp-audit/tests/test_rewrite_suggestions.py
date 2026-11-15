"""Degraded-path rewrite suggestion block (engine.rewrite) tests."""
import json
from scanamp_audit.engine.partition_pruner import prune_partitions
from scanamp_audit.engine.rewrite import build_degraded_path_rewrite, render_predicate
from scanamp_audit.expr.parser import parse_predicate
from scanamp_audit.expr.validate import analyze_predicate, validate_predicate
from scanamp_audit.models import ColumnDef, ManifestFile, Partition, TableManifest
from scanamp_audit.report.json_report import build_report


def _suggest(table, where, scanned_bytes=10**9):
    predicate = parse_predicate(where)
    validate_predicate(predicate, table.column_types)
    analysis = analyze_predicate(predicate, table.column_types, table.partition_column_names)
    partition = prune_partitions(table, predicate, analysis)
    block = build_degraded_path_rewrite(table, predicate, analysis, partition, scanned_bytes)
    return block, partition


def _dt_table(values):
    files = [
        ManifestFile(f"dt={value}/part-{index:04d}.csv", {"dt": value}, 10, 100 + index, {})
        for index, value in enumerate(values)
    ]
    partitions = [
        Partition(f"dt={item.partition_values['dt']}", dict(item.partition_values), [item])
        for item in files
    ]
    return TableManifest("t", "", [ColumnDef("dt", "date")], [], files, partitions,
                         "fp", [], "t")


def _only(block):
    assert block is not None, "expected a degraded-path rewrite block"
    assert len(block["suggestions"]) == 1
    return block["suggestions"][0]


def test_q03_block_attached_to_query_summary_and_evidence(demo_result):
    query = next(item for item in demo_result.queries if item["id"] == "q03_function_dt")
    block = query["degraded_path_rewrite"]
    assert block is not None
    assert block["trigger_rules"] == ["FUNCTION_WRAPPED_PARTITION",
                                      "UNKNOWN_PREDICATE_CONSERVATIVE"]
    assert block["table_bytes"] == 497
    assert block["current_scanned_bytes"] == query["metrics"]["scanned_bytes"] == 497
    suggestion = _only(block)
    assert suggestion["column"] == "dt"
    assert suggestion["function"] == "substr"
    assert suggestion["fragment"] == "substr(dt, 1, 7) = '2024-01'"
    assert suggestion["matched_partition_values"] == ["2024-01-01", "2024-01-02", "2024-01-03"]
    assert suggestion["match"] == "all"
    assert suggestion["rewrite"] == "1 = 1"
    assert suggestion["rewritten_where"] == "1 = 1"
    # The rewrite cannot prune anything on this domain, so the conservative
    # upper bound stays at the full table bytes and is not smaller than the
    # bytes the original query scanned.
    assert suggestion["selected_partitions"] == suggestion["total_partitions"] == 5
    assert suggestion["scanned_bytes_upper_bound"] == 497
    assert suggestion["scanned_bytes_upper_bound"] >= block["current_scanned_bytes"]
    assert suggestion["scan_byte_ratio"] == 1.0
    summary_entry = next(item for item in demo_result.summary["queries"]
                         if item["id"] == "q03_function_dt")
    assert summary_entry["degraded_path_rewrite"] == block
    diagnostic = next(item for item in demo_result.diagnostics
                      if item["rule_id"] == "UNKNOWN_PREDICATE_CONSERVATIVE")
    assert diagnostic["evidence"]["degraded_path_rewrite"] == block
    wrapped = next(item for item in demo_result.diagnostics
                   if item["rule_id"] == "FUNCTION_WRAPPED_PARTITION")
    assert "degraded_path_rewrite" not in wrapped["evidence"]
    report = build_report(demo_result)
    report_entry = next(item for item in report["queries"] if item["id"] == "q03_function_dt")
    assert report_entry["degraded_path_rewrite"] == block


def test_block_absent_when_rules_do_not_both_fire(demo_result):
    others = [item for item in demo_result.queries if item["id"] != "q03_function_dt"]
    assert others and all(item["degraded_path_rewrite"] is None for item in others)
    summary_nulls = [item for item in demo_result.summary["queries"]
                     if item["id"] != "q03_function_dt"]
    assert all(item["degraded_path_rewrite"] is None for item in summary_nulls)


def test_not_fragment_uses_domain_semantics_not_mechanical_strip():
    table = _dt_table(["2024-01-01", "2024-02-01", "2024-03-01"])
    block, partition = _suggest(table, "NOT substr(dt, 1, 4) = '2024'")
    assert partition["all_selected"]
    suggestion = _only(block)
    assert suggestion["fragment"] == "NOT substr(dt, 1, 4) = '2024'"
    # Every dt value starts with '2024', so the negated predicate matches
    # nothing; a mechanical strip to dt != '2024' would be wrong here.
    assert suggestion["matched_partition_values"] == []
    assert suggestion["rewrite"] == "1 = 0"
    assert suggestion["match"] == "none"
    assert suggestion["scanned_bytes_upper_bound"] == 0


def test_not_fragment_with_partial_domain_complements_matched_set():
    table = _dt_table(["2024-01-01", "2024-02-01", "2024-03-01"])
    block, _ = _suggest(table, "NOT substr(dt, 1, 7) = '2024-01'")
    suggestion = _only(block)
    assert suggestion["matched_partition_values"] == ["2024-02-01", "2024-03-01"]
    assert suggestion["rewrite"] == "dt BETWEEN '2024-02-01' AND '2024-03-01'"
    assert suggestion["match"] == "range"
    assert suggestion["scanned_bytes_upper_bound"] == 101 + 102


def test_value_range_and_set_rewrites():
    table = _dt_table(["2024-01-01", "2024-02-01", "2024-03-01"])
    suggestion = _only(_suggest(table, "substr(dt, 6, 2) = '01'")[0])
    assert suggestion["rewrite"] == "dt = '2024-01-01'"
    assert suggestion["match"] == "value"
    assert suggestion["scanned_bytes_upper_bound"] == 100
    suggestion = _only(_suggest(table, "substr(dt, 6, 2) <= '02'")[0])
    assert suggestion["rewrite"] == "dt BETWEEN '2024-01-01' AND '2024-02-01'"
    assert suggestion["match"] == "range"
    assert suggestion["scanned_bytes_upper_bound"] == 100 + 101
    suggestion = _only(_suggest(table, "substr(dt, 6, 2) != '02'")[0])
    assert suggestion["rewrite"] == "dt IN ('2024-01-01', '2024-03-01')"
    assert suggestion["match"] == "set"
    assert suggestion["scanned_bytes_upper_bound"] == 100 + 102


def test_or_structure_is_respected(demo_table):
    block, partition = _suggest(demo_table, "substr(dt, 1, 4) = '1999' OR region = 'gamma'")
    assert partition["all_selected"]
    suggestion = _only(block)
    assert suggestion["rewrite"] == "1 = 0"
    assert suggestion["rewritten_where"] == "1 = 0 OR region = 'gamma'"
    # Only the gamma partition survives the rewritten OR.
    assert suggestion["selected_partitions"] == 1
    assert suggestion["scanned_bytes_upper_bound"] == 78


def test_and_structure_is_preserved(demo_table):
    block, partition = _suggest(demo_table, "substr(dt, 1, 4) = '2024' AND amount > 100")
    assert partition["all_selected"]
    suggestion = _only(block)
    assert suggestion["rewrite"] == "1 = 1"
    assert suggestion["rewritten_where"] == "1 = 1 AND amount > 100"
    # The data-column conjunct is undecidable at partition level, so the
    # conservative bound stays at the full table bytes.
    assert suggestion["scanned_bytes_upper_bound"] == 497


def test_multiple_wrapped_columns_have_stable_sorted_order(demo_table):
    where = "lower(region) = 'alpha' AND substr(dt, 1, 7) = '2024-01'"
    block, partition = _suggest(demo_table, where)
    assert partition["all_selected"]
    assert [item["column"] for item in block["suggestions"]] == ["dt", "region"]
    first, second = block["suggestions"]
    assert first["rewrite"] == "1 = 1"
    assert first["scanned_bytes_upper_bound"] == 497
    assert second["rewrite"] == "region = 'alpha'"
    assert second["matched_partition_values"] == ["alpha"]
    assert second["scanned_bytes_upper_bound"] == 94 + 83 + 77
    again, _ = _suggest(demo_table, where)
    assert json.dumps(again, sort_keys=True) == json.dumps(block, sort_keys=True)


def test_trigger_requires_both_rules(demo_table):
    # Function-wrapped but prunable via a bare conjunct: not all selected.
    block, partition = _suggest(demo_table, "substr(dt, 1, 7) = '2024-01' AND dt = '2024-01-01'")
    assert not partition["all_selected"]
    assert block is None
    # Bare partition predicate only: nothing function-wrapped.
    assert _suggest(demo_table, "dt = '2024-01-01'")[0] is None
    # Function over a data column is not a wrapped partition column.
    assert _suggest(demo_table, "substr(event_type, 1, 4) = 'clic'")[0] is None


def test_unsupported_function_yields_no_suggestion(demo_table):
    block, partition = _suggest(demo_table, "date_trunc(dt, 1) = '2024'")
    assert partition["all_selected"]
    assert block is not None
    assert block["suggestions"] == []


def test_null_comparison_is_not_derivable(demo_table):
    # `!= NULL` is three-valued UNKNOWN, never a derivable "always true".
    block, partition = _suggest(demo_table, "substr(dt, 1, 4) != NULL")
    assert partition["all_selected"]
    assert block is not None
    assert block["suggestions"] == []


def test_not_above_group_rewrites_leaf_by_domain_equivalence(demo_table):
    block, partition = _suggest(demo_table, "NOT (substr(dt, 1, 4) = '2024' AND region = 'alpha')")
    assert partition["all_selected"]
    suggestion = _only(block)
    # The NOT belongs to the parenthesized group, not to the leaf fragment.
    assert suggestion["fragment"] == "substr(dt, 1, 4) = '2024'"
    assert suggestion["rewrite"] == "1 = 1"
    assert suggestion["rewritten_where"] == "NOT (1 = 1 AND region = 'alpha')"
    # Alpha partitions drop out; the rest stay.
    assert suggestion["scanned_bytes_upper_bound"] == 497 - (94 + 83 + 77)


def test_render_predicate_round_trip():
    for text in ("substr(dt, 1, 7) = '2024-01'",
                 "NOT (dt = '2024-01-01' AND region = 'alpha')",
                 "user_id = 1 OR user_id = 2 AND user_id = 3",
                 "dt NOT IN ('2024-01-01', '2024-02-01')",
                 "dt BETWEEN '2024-01-01' AND '2024-01-03'",
                 "status IS NOT NULL"):
        rendered = render_predicate(parse_predicate(text))
        assert render_predicate(parse_predicate(rendered)) == rendered
