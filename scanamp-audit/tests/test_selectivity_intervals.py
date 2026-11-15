"""Tests for selective-trust selectivity intervals and their reporting.

Covers:

* known-statistics leaves collapse to degenerate intervals while missing-stats
  leaves stay conservative at [0, 1];
* AND/OR/NOT interval composition (products, complements, clamping);
* per-file matched-byte lower bounds and the query-level amplification upper
  bound;
* wide-interval diagnostic evidence, which must never alter keep/drop.
"""
import pytest
from scanamp_audit.config import parse_config
from scanamp_audit.engine.file_pruner import prune_files
from scanamp_audit.engine.metrics import compute_metrics
from scanamp_audit.errors import ConfigError
from scanamp_audit.expr.estimate import (
    WIDEST_INTERVAL,
    _and_intervals,
    _not_interval,
    _or_intervals,
    estimate_selectivity,
    estimate_selectivity_interval,
)
from scanamp_audit.expr.parser import parse_predicate
from scanamp_audit.models import (
    ColumnDef,
    ColumnStats,
    ManifestFile,
    TableManifest,
)
from scanamp_audit.schema import round_float

DEFAULT_SELECTIVITY = 0.5
FILE_BYTES = 1000
FILE_ROWS = 100


def build_table(*, amount=(0.1, 0.3), user_has_stats=False, score=None):
    columns = [
        ColumnDef("user_id", "integer"),
        ColumnDef("amount", "float"),
    ]
    file_columns = {
        "user_id": ColumnStats("integer", has_stats=user_has_stats),
        "amount": ColumnStats(
            "float", min=amount[0], max=amount[1], null_count=0,
            distinct_count=3, has_stats=True,
        ),
    }
    if score is not None:
        columns.append(ColumnDef("score", "integer"))
        file_columns["score"] = ColumnStats(
            "integer", min=score[0], max=score[1], null_count=0,
            distinct_count=4, has_stats=True,
        )
    file = ManifestFile(
        path="dt=2024-01-01/part-0000.parquet",
        partition_values={"dt": "2024-01-01"},
        row_count=FILE_ROWS,
        byte_size=FILE_BYTES,
        columns=file_columns,
    )
    return TableManifest(
        name="t",
        description="interval test table",
        partition_columns=[ColumnDef("dt", "date")],
        columns=columns,
        files=[file],
        partitions=[],
        fingerprint="test",
        path_mismatches=[],
        source_name="manifest.yaml",
    )


def run(table, where, *, threshold=0.8):
    predicate = parse_predicate(where)
    result = prune_files(
        table, table.files, predicate, DEFAULT_SELECTIVITY,
        interval_width_threshold=threshold,
    )
    return predicate, result


def only_decision(result):
    assert len(result["decisions"]) == 1
    return result["decisions"][0]


# ---------------------------------------------------------------------------
# Leaf intervals
# ---------------------------------------------------------------------------

def test_known_stats_leaf_is_degenerate_interval():
    table = build_table()
    predicate, result = run(table, "amount > 0.2")
    decision = only_decision(result)
    # (0.3 - 0.2) / (0.3 - 0.1) == 0.5 for both point and interval estimates.
    assert decision["estimated_selectivity"] == 0.5
    low, high = decision["selectivity_interval"]
    assert low == high == 0.5
    assert decision["wide_selectivity_interval"] is False
    assert (decision["estimated_matched_bytes_low"]
            <= decision["estimated_matched_bytes"]
            <= decision["estimated_matched_bytes_high"])
    # A degenerate selectivity interval collapses the byte range to one row
    # either side of the point estimate.
    assert (decision["estimated_matched_bytes_high"]
            - decision["estimated_matched_bytes_low"]) <= 1


def test_missing_stats_leaf_is_conservative_widest_interval():
    table = build_table()
    predicate, result = run(table, "user_id > 100")
    decision = only_decision(result)
    assert decision["selectivity_interval"] == [0.0, 1.0]
    assert decision["selectivity_interval_width"] == 1.0
    assert decision["wide_selectivity_interval"] is True
    assert result["wide_interval_file_count"] == 1
    # Conservative retention: no statistics means the file cannot be dropped.
    assert decision["decision"] == "unknown"
    assert result["dropped_files"] == 0
    assert result["scanned_files"] == 1


def test_missing_stats_point_estimate_is_replaced_but_interval_is_not():
    table = build_table()
    predicate = parse_predicate("user_id > 100")
    resolver = {
        "user_id": {"kind": "unknown", "type": "integer"},
        "amount": {"kind": "stats", "type": "float", "min": 0.1, "max": 0.3,
                   "null_count": 0, "distinct_count": 3, "row_count": FILE_ROWS},
    }
    point = estimate_selectivity(predicate, resolver, table.column_types,
                                 DEFAULT_SELECTIVITY)
    interval = estimate_selectivity_interval(predicate, resolver, table.column_types,
                                             DEFAULT_SELECTIVITY)
    assert point == DEFAULT_SELECTIVITY
    assert interval == WIDEST_INTERVAL


# ---------------------------------------------------------------------------
# Acceptance scenario: precise amount stats AND a missing-stats column
# ---------------------------------------------------------------------------

def test_mixed_stats_and_missing_columns_bracket_the_point_estimate():
    table = build_table()
    # amount is fully inside [0.1, 0.3] (selectivity 1.0); user_id has no stats.
    predicate, result = run(table, "amount >= 0.1 AND amount <= 0.3 AND user_id > 100")
    decision = only_decision(result)

    # Legacy point path hides the uncertainty behind default_selectivity (0.5).
    assert decision["estimated_selectivity"] == 0.5
    point_bytes = decision["estimated_matched_bytes"]
    assert point_bytes == FILE_BYTES // 2

    # The interval keeps the known amount bound while treating user_id as open.
    assert decision["selectivity_interval"] == [0.0, 1.0]
    assert decision["estimated_matched_bytes_low"] < point_bytes
    assert decision["estimated_matched_bytes_low"] == 0
    assert decision["estimated_matched_bytes_high"] == FILE_BYTES
    assert decision["wide_selectivity_interval"] is True

    # The interval is evidence only: keep/drop still follows three-valued eval.
    assert decision["decision"] == "unknown"
    assert result["scanned_files"] == 1 and result["dropped_files"] == 0


def test_amplification_upper_bound_grows_when_lower_bound_shrinks():
    table = build_table()
    predicate, result = run(table, "amount >= 0.1 AND amount <= 0.3 AND user_id > 100")
    partition = {
        "selected_partitions": [
            {"file_count": 1, "byte_size": FILE_BYTES, "row_count": FILE_ROWS},
        ],
    }
    metrics = compute_metrics(table, partition, result)
    # Point amplification: 1000 scanned / 500 estimated matched == 2x.
    assert metrics["scan_amplification"] == 2.0
    # Worst case: matched bytes lower bound is 0 -> scanned / max(0, 1).
    assert metrics["scan_amplification_upper_bound"] == float(FILE_BYTES)
    assert (metrics["scan_amplification_upper_bound"]
            >= metrics["scan_amplification"])
    assert metrics["estimated_matched_bytes_low"] == 0
    assert metrics["estimated_matched_bytes_high"] == FILE_BYTES
    assert metrics["wide_interval_file_count"] == 1


def test_and_upper_bound_covers_child_lower_bounds_with_missing_stats():
    table = build_table()
    predicate = parse_predicate(
        "amount >= 0.1 AND amount <= 0.3 AND user_id > 100"
    )
    resolver = {
        "dt": {"kind": "exact", "type": "date", "value": "2024-01-01"},
        "amount": {"kind": "stats", "type": "float", "min": 0.1, "max": 0.3,
                   "null_count": 0, "distinct_count": 3, "row_count": FILE_ROWS},
        "user_id": {"kind": "unknown", "type": "integer"},
    }
    types = table.column_types
    child_intervals = [
        estimate_selectivity_interval(child, resolver, types, DEFAULT_SELECTIVITY)
        for child in predicate["children"]
    ]
    combined = estimate_selectivity_interval(predicate, resolver, types,
                                             DEFAULT_SELECTIVITY)
    # Requested invariant: the AND upper bound is no lower than any child's
    # lower bound. The unknown child contributes a zero lower bound; the known
    # amount children are fully inside their stats range.
    assert combined[1] >= max(child[0] for child in child_intervals)
    assert combined == (0.0, 1.0)


def test_and_interval_is_contained_in_each_known_child_interval():
    # Under product independence the universal guarantee is containment:
    # multiplying factors in [0, 1] can only shrink the interval.
    table = build_table(score=(0, 100))
    predicate = parse_predicate("amount > 0.2 AND score < 50")
    resolver = {
        "amount": {"kind": "stats", "type": "float", "min": 0.1, "max": 0.3,
                   "null_count": 0, "distinct_count": 3, "row_count": FILE_ROWS},
        "score": {"kind": "stats", "type": "integer", "min": 0, "max": 100,
                  "null_count": 0, "distinct_count": 4, "row_count": FILE_ROWS},
        "user_id": {"kind": "unknown", "type": "integer"},
    }
    types = table.column_types
    children = [
        estimate_selectivity_interval(child, resolver, types, DEFAULT_SELECTIVITY)
        for child in predicate["children"]
    ]
    low, high = estimate_selectivity_interval(predicate, resolver, types,
                                              DEFAULT_SELECTIVITY)
    assert ([(round_float(a), round_float(b)) for a, b in children]
            == [(0.5, 0.5), (0.5, 0.5)])
    assert (round_float(low), round_float(high)) == (0.25, 0.25)
    # Product independence: the AND interval is contained in every child
    # interval, i.e. no wider than the tightest child.
    for child_low, child_high in children:
        assert low <= child_low and high <= child_high
    assert low <= high


# ---------------------------------------------------------------------------
# OR / NOT composition
# ---------------------------------------------------------------------------

def test_or_merges_complement_products():
    table = build_table()
    predicate = parse_predicate("amount > 0.2 OR user_id > 100")
    resolver = {
        "amount": {"kind": "stats", "type": "float", "min": 0.1, "max": 0.3,
                   "null_count": 0, "distinct_count": 3, "row_count": FILE_ROWS},
        "user_id": {"kind": "unknown", "type": "integer"},
    }
    low, high = estimate_selectivity_interval(predicate, resolver,
                                              table.column_types,
                                              DEFAULT_SELECTIVITY)
    # 1 - (1 - 0.5) * (1 - 1.0) == 1.0 upper; 1 - (1 - 0.5) * (1 - 0) == 0.5 lower
    assert (round_float(low), round_float(high)) == (0.5, 1.0)


def test_or_interval_helpers():
    low, high = _or_intervals([(0.2, 0.4), (0.1, 0.3)])
    assert low == pytest.approx(1 - 0.8 * 0.9)
    assert high == pytest.approx(1 - 0.6 * 0.7)
    # Identity and degenerate cases stay inside [0, 1].
    assert _or_intervals([(0.0, 0.0), (1.0, 1.0)]) == (1.0, 1.0)


def test_and_interval_helpers():
    low, high = _and_intervals([(0.2, 0.4), (0.5, 0.8)])
    assert low == pytest.approx(0.1)
    assert high == pytest.approx(0.32)
    assert _and_intervals([(0.0, 1.0), (0.7, 0.7)]) == (0.0, 0.7)


def test_not_takes_complement_and_clamps():
    low, high = _not_interval((0.2, 0.7))
    assert low == pytest.approx(0.3) and high == pytest.approx(0.8)
    assert _not_interval((0.0, 1.0)) == (0.0, 1.0)
    # Float drift outside the unit box is clamped back in.
    low, high = _not_interval((0.0, 1.0 + 1e-12))
    assert (low, high) == (0.0, 1.0)
    low, high = _not_interval((-1e-12, 0.4))
    assert low == pytest.approx(0.6) and high == 1.0
    for interval in ((0.0, 0.0), (1.0, 1.0), (0.33, 0.91)):
        complement = _not_interval(interval)
        assert 0.0 <= complement[0] <= complement[1] <= 1.0


def test_not_with_missing_stats_stays_conservative_end_to_end():
    table = build_table()
    # NNF rewrites NOT(user_id > 100) into user_id <= 100, still unknown.
    predicate, result = run(table, "NOT (user_id > 100)")
    decision = only_decision(result)
    assert decision["selectivity_interval"] == [0.0, 1.0]
    assert decision["decision"] == "unknown"
    assert result["scanned_files"] == 1 and result["dropped_files"] == 0


def test_not_node_directly_uses_complement():
    table = build_table(score=(0, 100))
    predicate = {
        "node": "not",
        "child": parse_predicate("score < 50"),
    }
    resolver = {
        "score": {"kind": "stats", "type": "integer", "min": 0, "max": 100,
                  "null_count": 0, "distinct_count": 4, "row_count": FILE_ROWS},
        "amount": {"kind": "stats", "type": "float", "min": 0.1, "max": 0.3,
                   "null_count": 0, "distinct_count": 3, "row_count": FILE_ROWS},
        "user_id": {"kind": "unknown", "type": "integer"},
    }
    assert estimate_selectivity_interval(predicate, resolver, table.column_types,
                                         DEFAULT_SELECTIVITY) == (0.5, 0.5)


# ---------------------------------------------------------------------------
# Bounds, thresholds, decisions and edge cases
# ---------------------------------------------------------------------------

def test_byte_and_row_bounds_bracket_the_point_estimate():
    table = build_table()
    _, result = run(table, "amount >= 0.1 AND user_id > 100")
    decision = only_decision(result)
    assert (decision["estimated_matched_rows_low"]
            <= decision["estimated_matched_rows"]
            <= decision["estimated_matched_rows_high"])
    assert (decision["estimated_matched_bytes_low"]
            <= decision["estimated_matched_bytes"]
            <= decision["estimated_matched_bytes_high"])
    assert decision["estimated_matched_rows_low"] == 0
    assert decision["estimated_matched_rows_high"] == FILE_ROWS


def test_wide_interval_threshold_is_configurable_and_uses_strict_inequality():
    table = build_table()
    # Width is exactly 1.0 for the missing-stats leaf.
    _, flagged = run(table, "user_id > 100", threshold=0.99)
    assert only_decision(flagged)["wide_selectivity_interval"] is True
    assert flagged["wide_interval_file_count"] == 1
    _, quiet = run(table, "user_id > 100", threshold=1.0)
    assert only_decision(quiet)["wide_selectivity_interval"] is False
    assert quiet["wide_interval_file_count"] == 0
    assert quiet["selectivity_interval_width_threshold"] == 1.0
    # Threshold changes evidence only; the decision is identical.
    assert (only_decision(flagged)["decision"]
            == only_decision(quiet)["decision"] == "unknown")


def test_dropped_file_has_zeroed_interval_and_is_not_flagged():
    table = build_table()
    _, result = run(table, "amount > 9999 AND user_id > 100")
    decision = only_decision(result)
    assert decision["decision"] == "drop"
    assert decision["selectivity_interval"] == [0.0, 0.0]
    assert decision["estimated_matched_bytes_low"] == 0
    assert decision["estimated_matched_bytes_high"] == 0
    assert decision["wide_selectivity_interval"] is False
    assert result["scanned_files"] == 0 and result["dropped_files"] == 1
    assert result["estimated_matched_bytes_low"] == 0
    assert result["estimated_matched_bytes_high"] == 0


def test_no_predicate_is_full_degenerate_interval():
    table = build_table()
    result = prune_files(table, table.files, None, DEFAULT_SELECTIVITY)
    decision = only_decision(result)
    assert decision["selectivity_interval"] == [1.0, 1.0]
    assert decision["wide_selectivity_interval"] is False
    assert decision["estimated_matched_bytes_low"] == FILE_BYTES
    assert decision["estimated_matched_bytes_high"] == FILE_BYTES
    assert result["estimated_matched_bytes_low"] == FILE_BYTES


def test_config_threshold_parsing():
    config = parse_config(
        {"estimation": {"default_selectivity": 0.5,
                        "selectivity_interval_width_threshold": 0.25}}
    )
    assert config.selectivity_interval_width_threshold == 0.25
    # The threshold is diagnostic-only and excluded from the config fingerprint.
    assert "selectivity_interval_width_threshold" not in config.to_dict()["estimation"]
    default = parse_config({"estimation": {"default_selectivity": 0.5}})
    assert default.selectivity_interval_width_threshold == 0.8
    with pytest.raises(ConfigError):
        parse_config(
            {"estimation": {"selectivity_interval_width_threshold": 1.5}}
        )
    with pytest.raises(ConfigError):
        parse_config(
            {"estimation": {"selectivity_interval_width_threshold": "wide"}}
        )


def test_demo_workload_upper_bound_brackets_point_amplification(demo_result):
    query = next(item for item in demo_result.queries
                 if item["id"] == "q05_missing_user_stats")
    metrics = query["metrics"]
    assert metrics["scan_amplification"] == 2.0
    assert (metrics["scan_amplification_upper_bound"]
            > metrics["scan_amplification"])
    assert metrics["wide_interval_file_count"] == 3
    # Every per-file decision carries the interval evidence, while legacy
    # decisions and reasons remain unchanged.
    for decision in query["files"]["decisions"]:
        assert len(decision["selectivity_interval"]) == 2
        assert decision["selectivity_interval"][0] <= decision["selectivity_interval"][1]
        assert isinstance(decision["wide_selectivity_interval"], bool)
        assert decision["decision"] in {"keep", "drop", "unknown"}
    # Fully known queries keep degenerate intervals, so the bound equals point.
    known = next(item for item in demo_result.queries
                 if item["id"] == "q01_partition_plus_amount")
    assert (known["metrics"]["scan_amplification_upper_bound"]
            == known["metrics"]["scan_amplification"])
