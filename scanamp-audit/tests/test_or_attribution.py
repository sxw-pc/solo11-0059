"""OR-branch cross attribution: coverage sets, delta decomposition, diagnostics."""
from helpers import by_query
from scanamp_audit.config import parse_config
from scanamp_audit.engine.auditor import run_audit
from scanamp_audit.engine.metrics import compute_or_attribution
from scanamp_audit.expr.parser import parse_predicate
from scanamp_audit.expr.validate import validate_predicate
from scanamp_audit.models import (
    ColumnDef,
    ColumnStats,
    ManifestFile,
    Partition,
    Query,
    TableManifest,
    Workload,
)

def _prune_result(decisions):
    """Fabricate a prune_files-shaped result from ((part, path), decision) pairs."""
    return {
        "decisions": [
            {"partition_id": part, "path": path, "decision": decision}
            for (part, path), decision in decisions
        ]
    }

def make_table(file_stats):
    """Single-partition synthetic table; file_stats maps columns to (min, max) or None."""
    files = []
    for index, stats in enumerate(file_stats):
        columns = {}
        for name, spec in stats.items():
            type_name = "float" if name == "amount" else "integer"
            if spec is None:
                columns[name] = ColumnStats(type=type_name, has_stats=False)
            else:
                columns[name] = ColumnStats(
                    type=type_name, min=spec[0], max=spec[1],
                    null_count=0, distinct_count=2, has_stats=True,
                )
        files.append(
            ManifestFile(
                path=f"dt=2024-01-01/part-{index:04d}.csv",
                partition_values={"dt": "2024-01-01"},
                row_count=100,
                byte_size=100,
                columns=columns,
            )
        )
    partition = Partition("dt=2024-01-01", {"dt": "2024-01-01"}, files)
    return TableManifest(
        name="synthetic",
        description="synthetic OR attribution table",
        partition_columns=[ColumnDef("dt", "date")],
        columns=[ColumnDef("amount", "float"), ColumnDef("user_id", "integer")],
        files=files,
        partitions=[partition],
        fingerprint="synthetic",
        path_mismatches=[],
        source_name="synthetic",
    )

def audit(table, queries, config=None):
    parsed = []
    for query_id, where in queries:
        predicate = parse_predicate(where, query_id) if where else None
        validate_predicate(predicate, table.column_types, query_id)
        parsed.append(Query(query_id, f"test {query_id}", where, predicate))
    workload = Workload(parsed, fingerprint="test-workload", source_name="test")
    return run_audit(table, workload, config or parse_config({}),
                     "test-run", "2024-01-01T00:00:00Z")

def rules_for(result, query_id):
    return {item["rule_id"] for item in result.diagnostics if item["query_id"] == query_id}

def test_attribution_shared_and_exclusive_counts():
    branches = [
        _prune_result([(("p", "a"), "keep"), (("p", "b"), "drop")]),
        _prune_result([(("p", "a"), "unknown"), (("p", "b"), "keep")]),
    ]
    union = _prune_result([(("p", "a"), "keep"), (("p", "b"), "keep")])
    attr = compute_or_attribution(branches, union)
    assert attr["union_files"] == 2
    assert attr["shared_files"] == 1  # a is covered by both branches
    assert attr["exclusive_files"] == 1  # b is covered by branch 1 only
    assert attr["largest_branch_index"] == 1
    assert attr["expansion_delta"] == 0
    assert attr["dominant_cause"] == "none"
    first, second = attr["branches"]
    assert (first["keep_files"], first["unknown_files"]) == (1, 0)
    assert (first["exclusive_files"], first["shared_files"]) == (0, 1)
    assert second["all_unknown"] is False
    assert attr["stats_missing_branches"] == []

def test_attribution_stats_missing_dominated_union():
    branches = [
        _prune_result([(("p", "a"), "unknown"), (("p", "b"), "unknown")]),
        _prune_result([(("p", "a"), "keep"), (("p", "b"), "drop")]),
    ]
    union = _prune_result([(("p", "a"), "unknown"), (("p", "b"), "unknown")])
    attr = compute_or_attribution(branches, union)
    assert attr["expansion_delta"] == 0
    assert attr["dominant_cause"] == "stats_missing"
    assert attr["stats_missing_branches"] == [0]

def test_attribution_shared_contribution():
    branches = [
        _prune_result([(("p", "a"), "keep"), (("p", "b"), "keep"), (("p", "c"), "drop")]),
        _prune_result([(("p", "a"), "drop"), (("p", "b"), "keep"), (("p", "c"), "keep")]),
        _prune_result([(("p", "a"), "drop"), (("p", "b"), "keep"), (("p", "c"), "keep")]),
    ]
    union = _prune_result([(("p", "a"), "keep"), (("p", "b"), "keep"), (("p", "c"), "keep")])
    attr = compute_or_attribution(branches, union)
    assert attr["expansion_delta"] == 1
    # c is the file the largest branch misses, and two branches share it
    assert attr["shared_contribution"] == 1
    assert attr["exclusive_contribution"] == 0
    assert attr["dominant_cause"] == "shared"
    assert attr["shared_files"] == 2  # b and c
    assert attr["exclusive_files"] == 1  # a

def test_attribution_exclusive_contribution():
    branches = [
        _prune_result([(("p", "a"), "keep"), (("p", "b"), "drop")]),
        _prune_result([(("p", "a"), "drop"), (("p", "b"), "keep")]),
    ]
    union = _prune_result([(("p", "a"), "keep"), (("p", "b"), "keep")])
    attr = compute_or_attribution(branches, union)
    assert attr["expansion_delta"] == 1
    assert attr["exclusive_contribution"] == 1
    assert attr["shared_contribution"] == 0
    assert attr["dominant_cause"] == "exclusive"

def test_attribution_all_branches_empty():
    branches = [
        _prune_result([(("p", "a"), "drop")]),
        _prune_result([(("p", "a"), "drop")]),
    ]
    union = _prune_result([(("p", "a"), "drop")])
    attr = compute_or_attribution(branches, union)
    assert attr["union_files"] == 0
    assert attr["expansion_delta"] == 0
    assert attr["dominant_cause"] == "none"
    assert attr["stats_missing_branches"] == []

def test_equivalent_unknown_coverage_is_not_blowup(demo_table):
    # Both branches are UNKNOWN on exactly the same files (user_id has no stats
    # there); union == single branch, so expansion is 1.0 and nothing was blown up.
    where = "user_id = 1000 OR user_id = 2000"
    result = audit(demo_table, [("q_or_equiv", where)])
    info = by_query(result, "q_or_equiv")["or_branches"]
    attr = info["attribution"]
    assert info["union_scanned_files"] == 3
    assert info["expansion_factor"] == 1.0
    assert attr["expansion_delta"] == 0
    assert attr["shared_files"] == 3
    assert attr["exclusive_files"] == 0
    assert attr["dominant_cause"] == "stats_missing"
    assert attr["stats_missing_branches"] == [0, 1]
    assert "OR_BRANCH_BLOWUP" not in rules_for(result, "q_or_equiv")
    # Equivalent coverage must not fire even when the expansion threshold is 1.0.
    config = parse_config(
        {"diagnostics": {"OR_BRANCH_BLOWUP": {"thresholds": {"min_expansion_factor": 1.0}}}}
    )
    lowered = audit(demo_table, [("q_or_equiv", where)], config)
    assert "OR_BRANCH_BLOWUP" not in rules_for(lowered, "q_or_equiv")

def test_stats_missing_branch_dominates_union():
    # user_id has no stats anywhere, so its branch keeps every file; the other
    # branch only hits a subset. The union is fully explained by missing stats.
    table = make_table([
        {"amount": (0.0, 100.0), "user_id": None},
        {"amount": (0.0, 1000.0), "user_id": None},
        {"amount": (500.0, 1000.0), "user_id": None},
    ])
    where = "user_id = 7 OR amount >= 900"
    result = audit(table, [("q_or_stats", where)])
    info = by_query(result, "q_or_stats")["or_branches"]
    attr = info["attribution"]
    assert info["union_scanned_files"] == 3
    assert info["expansion_factor"] == 1.0
    assert attr["largest_branch_index"] == 0
    assert attr["expansion_delta"] == 0
    assert attr["shared_files"] == 2
    assert attr["exclusive_files"] == 1
    assert attr["dominant_cause"] == "stats_missing"
    assert attr["stats_missing_branches"] == [0]
    assert "OR_BRANCH_BLOWUP" not in rules_for(result, "q_or_stats")
    assert "STATS_MISSING" in rules_for(result, "q_or_stats")
    config = parse_config(
        {"diagnostics": {"OR_BRANCH_BLOWUP": {"thresholds": {"min_expansion_factor": 1.0}}}}
    )
    lowered = audit(table, [("q_or_stats", where)], config)
    assert "OR_BRANCH_BLOWUP" not in rules_for(lowered, "q_or_stats")

def test_shared_growth_fires_with_shared_attribution():
    # Every branch keeps two of three files; the union's extra file is shared by
    # two branches, so splitting cannot save the rescan.
    table = make_table([
        {"amount": (0.0, 100.0), "user_id": (0, 10)},
        {"amount": (0.0, 1000.0), "user_id": (0, 10)},
        {"amount": (500.0, 1000.0), "user_id": (0, 10)},
    ])
    result = audit(table, [("q_or_shared", "amount < 500 OR amount > 100 OR amount >= 500")])
    info = by_query(result, "q_or_shared")["or_branches"]
    attr = info["attribution"]
    assert info["union_scanned_files"] == 3
    assert info["expansion_factor"] == 1.5
    assert attr["expansion_delta"] == 1
    assert attr["shared_contribution"] == 1
    assert attr["exclusive_contribution"] == 0
    assert attr["dominant_cause"] == "shared"
    diagnostic = next(item for item in result.diagnostics
                      if item["rule_id"] == "OR_BRANCH_BLOWUP")
    assert diagnostic["query_id"] == "q_or_shared"
    assert "shared" in diagnostic["message"]
    evidence = diagnostic["evidence"]
    assert evidence["dominant_cause"] == "shared"
    assert evidence["shared_contribution"] == 1
    assert evidence["exclusive_contribution"] == 0
    assert evidence["expansion_delta"] == 1

def test_demo_q04_exclusive_attribution(demo_result):
    info = by_query(demo_result, "q04_or_branches")["or_branches"]
    attr = info["attribution"]
    assert attr["union_files"] == info["union_scanned_files"] == 2
    assert attr["expansion_delta"] == 1
    assert attr["shared_files"] == 0
    assert attr["exclusive_files"] == 2
    assert attr["exclusive_contribution"] == 1
    assert attr["shared_contribution"] == 0
    assert attr["dominant_cause"] == "exclusive"
    assert attr["stats_missing_branches"] == []
    for branch in info["branches"]:
        assert branch["keep_files"] + branch["unknown_files"] == branch["scanned_files"]
    diagnostic = next(item for item in demo_result.diagnostics
                      if item["rule_id"] == "OR_BRANCH_BLOWUP")
    evidence = diagnostic["evidence"]
    assert evidence["dominant_cause"] == "exclusive"
    assert evidence["exclusive_contribution"] == 1
    assert evidence["shared_contribution"] == 0
    assert "per-branch" in diagnostic["suggestion"]

def test_summary_or_attribution_for_demo(demo_result):
    entries = demo_result.summary["or_attribution"]
    assert [entry["id"] for entry in entries] == ["q04_or_branches"]
    entry = entries[0]
    assert entry["dominant_cause"] == "exclusive"
    assert entry["expansion_delta"] == 1
    assert entry["expansion_factor"] == 2.0

def test_summary_or_attribution_is_stably_sorted():
    table = make_table([
        {"amount": (0.0, 100.0), "user_id": (0, 10)},
        {"amount": (500.0, 1000.0), "user_id": (0, 10)},
        {"amount": (1500.0, 2000.0), "user_id": (0, 10)},
        {"amount": (2500.0, 3000.0), "user_id": (0, 10)},
    ])
    queries = [
        ("q_tie_b", "amount = 1600 OR amount = 2600"),
        ("q_big", "amount = 50 OR amount = 750 OR amount = 1600 OR amount = 2600"),
        ("q_tie_a", "amount = 50 OR amount = 750"),
        ("q_mid", "amount = 50 OR amount = 750 OR amount >= 1500"),
    ]
    result = audit(table, queries)
    entries = result.summary["or_attribution"]
    assert [entry["id"] for entry in entries] == ["q_big", "q_mid", "q_tie_a", "q_tie_b"]
    deltas = [entry["expansion_delta"] for entry in entries]
    assert deltas == [3, 2, 1, 1]
    assert all(entry["dominant_cause"] == "exclusive" for entry in entries)
    again = audit(table, queries)
    assert again.summary["or_attribution"] == entries

def test_disabled_rule_still_computes_attribution(demo_table):
    config = parse_config({"diagnostics": {"OR_BRANCH_BLOWUP": {"enabled": False}}})
    where = "(dt = '2024-01-01' AND region = 'alpha' AND amount > 4000) " \
            "OR (dt = '2024-01-03' AND amount < 1000)"
    result = audit(demo_table, [("q_or_off", where)], config)
    assert "OR_BRANCH_BLOWUP" not in rules_for(result, "q_or_off")
    info = by_query(result, "q_or_off")["or_branches"]
    assert info["attribution"]["dominant_cause"] == "exclusive"
    assert result.summary["or_attribution"][0]["id"] == "q_or_off"
