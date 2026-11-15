"""Tests for partition layout statistics and counterfactual layout evaluation."""
from pathlib import Path
from scanamp_audit.engine.layout_evaluator import (
    _build_view,
    column_statistics,
    evaluate_layouts,
)
from scanamp_audit.expr.parser import parse_predicate
from scanamp_audit.expr.validate import validate_predicate
from scanamp_audit.models import ColumnDef, ManifestFile, Partition, Query, TableManifest
from scanamp_audit.schema import partition_id


def layout(evaluation, layout_id):
    return next(item for item in evaluation["layouts"] if item["layout_id"] == layout_id)


def query(layout_entry, query_id):
    return next(item for item in layout_entry["queries"] if item["query_id"] == query_id)


def make_table(partition_columns, rows):
    """Build a synthetic table from (path, partition values, bytes, rows) tuples."""
    names = [column.name for column in partition_columns]
    files = [
        ManifestFile(path, dict(values), row_count, byte_size)
        for path, values, byte_size, row_count in rows
    ]
    files.sort(key=lambda item: item.path)
    groups: dict[tuple, list[ManifestFile]] = {}
    for item in files:
        groups.setdefault(tuple(item.partition_values[name] for name in names), []).append(item)
    partitions = [
        Partition(partition_id(dict(zip(names, key)), names), dict(zip(names, key)), value)
        for key, value in sorted(groups.items())
    ]
    table = TableManifest("synthetic", "", partition_columns, [], files, partitions,
                          "fp", [], "synthetic", Path("."))
    return table


def make_queries(table, wheres):
    queries = []
    for index, where in enumerate(wheres, 1):
        predicate = parse_predicate(where, f"q{index}")
        validate_predicate(predicate, table.column_types, f"q{index}")
        queries.append(Query(f"q{index}", "", where, predicate))
    return queries


def test_multi_column_removal_uses_true_merged_byte_totals():
    columns = [ColumnDef("a", "string"), ColumnDef("b", "string"), ColumnDef("c", "string")]
    rows = [
        ("a=x/b=1/c=p/f1.csv", {"a": "x", "b": "1", "c": "p"}, 10, 10),
        ("a=x/b=2/c=p/f2.csv", {"a": "x", "b": "2", "c": "p"}, 20, 20),
        ("a=y/b=1/c=p/f3.csv", {"a": "y", "b": "1", "c": "p"}, 30, 30),
        ("a=y/b=1/c=q/f4.csv", {"a": "y", "b": "1", "c": "q"}, 40, 40),
    ]
    table = make_table(columns, rows)
    queries = make_queries(table, ["a = 'x' AND b = '1'"])
    evaluation = evaluate_layouts(table, queries)
    assert [item["layout_id"] for item in evaluation["layouts"]] == [
        "current", "without:a", "without:b", "without:c",
        "without:a,b", "without:a,c", "without:b,c", "without:a,b,c",
    ]
    without_bc = layout(evaluation, "without:b,c")
    assert without_bc["remaining_columns"] == ["a"]
    assert without_bc["partition_count"] == 2
    assert without_bc["merged_group_count"] == 2
    entry = query(without_bc, "q1")
    assert entry["candidate_files"] == 2
    # a=x merged f1 + f2: a byte sum from real files, not a guess.
    assert entry["candidate_bytes"] == 30
    assert entry["candidate_rows"] == 30
    baseline = query(evaluation["layouts"][0], "q1")
    assert baseline["candidate_bytes"] == 10
    assert entry["candidate_bytes_delta"] == 20
    assert entry["partition_prune_ratio"] == 0.5
    # a alone has two values, each holding two files, so no single-file value.
    stats = {item["column"]: item for item in evaluation["columns"]}
    assert stats["a"]["cardinality"] == 2
    assert stats["a"]["single_file_partition_ratio"] == 0.0
    assert stats["b"]["single_file_partition_count"] == 1
    assert stats["c"]["single_file_partition_count"] == 1
    # Removing all columns leaves one root partition holding every file.
    every = layout(evaluation, "without:a,b,c")
    assert every["partition_count"] == 1
    assert every["remaining_columns"] == []
    assert every["file_count"] == 4 and every["byte_size"] == 100
    assert query(every, "q1")["conservative_all_partitions"] is True


def test_single_partition_column_removal_is_root_layout():
    column = ColumnDef("p", "string")
    table = make_table([column], [
        ("p=one/f1.csv", {"p": "one"}, 10, 5),
        ("p=two/f1.csv", {"p": "two"}, 20, 5),
    ])
    queries = make_queries(table, ["p = 'one'"])
    evaluation = evaluate_layouts(table, queries)
    assert [item["layout_id"] for item in evaluation["layouts"]] == ["current", "without:p"]
    without = layout(evaluation, "without:p")
    assert without["partition_count"] == 1 and without["merged_group_count"] == 1
    entry = query(without, "q1")
    assert entry["conservative_all_partitions"] is True
    assert entry["candidate_files"] == 2 and entry["candidate_bytes"] == 30
    assert entry["candidate_bytes_delta"] == 20


def test_column_statistics_cardinality_and_single_file_share(demo_table):
    stats = {item["column"]: item for item in column_statistics(demo_table)}
    dt, region = stats["dt"], stats["region"]
    assert dt["cardinality"] == 3 and region["cardinality"] == 4
    assert [item["value"] for item in dt["values"]] == [
        "2024-01-01", "2024-01-02", "2024-01-03"]
    assert [item["file_count"] for item in dt["values"]] == [3, 2, 1]
    assert [item["byte_size"] for item in dt["values"]] == [255, 164, 78]
    assert dt["single_file_partition_count"] == 1
    assert dt["single_file_partition_ratio"] == round(1 / 3, 6)
    assert region["single_file_partition_count"] == 3
    assert region["single_file_partition_ratio"] == 0.75
    assert dt["file_count_distribution"] == {
        "count": 3, "min": 1, "max": 3, "total": 6, "mean": 2.0, "median": 2.0}


def test_removing_column_merges_partitions_with_real_file_sets(demo_table, demo_workload):
    evaluation = evaluate_layouts(demo_table, demo_workload.queries)
    without_dt = layout(evaluation, "without:dt")
    assert without_dt["remaining_columns"] == ["region"]
    assert without_dt["partition_count"] == 4
    assert without_dt["merged_group_count"] == 1
    assert without_dt["file_count"] == demo_table.file_count
    merged = {item["query_id"]: item for item in without_dt["queries"]}
    # region=alpha now owns all three alpha files (3 total, 254 bytes) because
    # two old dt partitions merged; it is not estimated from a file count.
    alpha = query(without_dt, "q05_missing_user_stats")
    assert alpha["selected_partition_count"] == 1
    assert alpha["candidate_files"] == 3
    assert alpha["candidate_bytes"] == 94 + 83 + 77
    assert alpha["candidate_bytes_delta"] == 254 - 254
    # A query whose predicate does not mention the removed column keeps the
    # same candidate sets even though the partition count dropped.
    q02 = query(evaluation["layouts"][0], "q02_data_only_event")
    q02_merged = merged["q02_data_only_event"]
    assert q02_merged["candidate_files"] == q02["candidate_files"] == 6
    assert q02_merged["candidate_bytes"] == q02["candidate_bytes"] == 497


def test_removed_column_is_only_partition_filter(demo_table, demo_workload):
    evaluation = evaluate_layouts(demo_table, demo_workload.queries)
    without_region = layout(evaluation, "without:region")
    q05 = query(without_region, "q05_missing_user_stats")
    assert q05["referenced_partition_columns"] == ["region"]
    assert q05["removed_partition_filter_columns"] == ["region"]
    assert q05["retained_partition_filter_columns"] == []
    assert q05["conservative_all_partitions"] is True
    # Conservative retention must recompute from the real merged partitions,
    # not zero out the ratio or reuse an old byte count.
    assert q05["selected_partition_count"] == without_region["partition_count"] == 3
    assert q05["partition_prune_ratio"] == 0.0
    assert q05["candidate_file_prune_ratio"] == 0.0
    assert q05["candidate_files"] == 6
    assert q05["candidate_bytes"] == 497
    assert q05["candidate_bytes_delta"] == 497 - 254
    assert "QUERY_LOST_ONLY_PARTITION_FILTER:region" in q05["reasons"]
    assert "CONSERVATIVE_ALL_PARTITIONS_RETAINED:region" in q05["reasons"]
    assert "MERGED_PARTITION_PREDICATE_UNDECIDABLE:region" in q05["reasons"]


def test_counterfactual_prune_ratio_can_increase(demo_table, demo_workload):
    evaluation = evaluate_layouts(demo_table, demo_workload.queries)
    without_dt = layout(evaluation, "without:dt")
    baseline = evaluation["layouts"][0]
    q08 = query(without_dt, "q08_in_and_between")
    q08_baseline = query(baseline, "q08_in_and_between")
    # region IN ('alpha','beta') keeps 3 of 5 old partitions; after dropping dt
    # the same predicate keeps 2 of 4 merged partitions, a higher ratio even
    # though the candidate file set grows.
    assert q08_baseline["partition_prune_ratio"] == round(2 / 5, 6)
    assert q08["partition_prune_ratio"] == 0.5
    assert q08["partition_prune_ratio_delta"] > 0
    assert "PARTITION_PRUNE_RATIO_INCREASED" in q08["reasons"]
    assert q08["candidate_files"] == 4
    assert q08["candidate_files_delta"] == 4 - 4
    # The file-based ratio stays identical because the candidate file set is
    # unchanged; the partition-count perspective is the one that rises.
    assert q08["candidate_file_prune_ratio"] == q08_baseline["candidate_file_prune_ratio"]
    # The dt-only query q06 keeps the same candidate bytes but the merged
    # layout has fewer partitions, so its prune ratio rises from 0 to 0.75.
    q06 = query(without_dt, "q06_wide_date_range")
    assert q06["conservative_all_partitions"] is True
    assert q06["candidate_files"] == 6
    assert q06["reasons"]
    q01 = query(without_dt, "q01_partition_plus_amount")
    assert q01["partition_prune_ratio"] == 0.75
    assert q01["partition_prune_ratio_delta"] > 0


def test_multi_column_removal_is_deterministic(demo_table, demo_workload):
    first = evaluate_layouts(demo_table, demo_workload.queries)
    second = evaluate_layouts(demo_table, demo_workload.queries)
    assert first == second
    ids = [item["layout_id"] for item in first["layouts"]]
    assert ids == ["current", "without:dt", "without:region", "without:dt,region"]
    both = layout(first, "without:dt,region")
    assert both["remaining_columns"] == []
    assert both["partition_count"] == 1
    assert both["merged_group_count"] == 1
    assert both["file_count"] == 6 and both["byte_size"] == 497
    assert all(item["reasons"] for item in both["queries"])


def test_view_merges_files_by_real_partition_values(demo_table):
    view, merged = _build_view(demo_table, [demo_table.partition_columns[1]], ["dt"])
    assert view.partition_column_names == ["region"]
    assert merged == 1
    alpha = next(part for part in view.partitions if part.values == {"region": "alpha"})
    assert alpha.file_count == 3
    assert alpha.byte_size == 94 + 83 + 77
    assert {item.path for item in alpha.files} == {
        "dt=2024-01-01/region=alpha/part-0001.csv",
        "dt=2024-01-01/region=alpha/part-0002.csv",
        "dt=2024-01-02/region=alpha/part-0001.csv",
    }


def test_baseline_layout_matches_audit_query_metrics(demo_result):
    baseline = demo_result.layout_evaluation["layouts"][0]
    assert baseline["layout_id"] == "current"
    assert baseline["removed_columns"] == []
    for entry in baseline["queries"]:
        audit_query = next(item for item in demo_result.queries if item["id"] == entry["query_id"])
        metrics = audit_query["metrics"]
        assert entry["candidate_files"] == metrics["after_partition_files"]
        assert entry["candidate_bytes"] == metrics["after_partition_bytes"]
        assert entry["selected_partition_count"] == audit_query["partition"]["selected_count"]
        # The audit's query-level partition_prune_ratio is file based.
        assert entry["candidate_file_prune_ratio"] == metrics["partition_prune_ratio"]
        assert entry["partition_prune_ratio"] == audit_query["partition"]["prune_ratio"]
        assert entry["reasons"] == []


def test_layout_evaluation_reaches_summary_and_ledger(demo_result, demo_ledger):
    from scanamp_audit.ledger import Ledger
    assert demo_result.summary["layout_evaluation"] == demo_result.layout_evaluation
    ledger = Ledger(demo_ledger, allow_outside=True, read_only=True)
    stored = ledger.get_run("demo-run")
    ledger.close()
    assert stored["layout_evaluation"] == demo_result.layout_evaluation
    assert [item["layout_id"] for item in stored["layout_evaluation"]["layouts"]][0] == "current"
