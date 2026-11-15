from helpers import plan

def ids(partition):
    return [item["partition_id"] for item in partition["selected_partitions"]]

def test_and_intersection(demo_table):
    result = plan(demo_table, "dt BETWEEN '2024-01-01' AND '2024-01-02' AND region = 'alpha'")
    assert result["partition"]["selected_count"] == 2
    assert result["partition"]["pruned_count"] == 3
    assert ids(result["partition"]) == [
        "dt=2024-01-01/region=alpha", "dt=2024-01-02/region=alpha"]

def test_data_only_predicate_keeps_all(demo_table):
    result = plan(demo_table, "event_type = 'click'")
    assert result["partition"]["all_selected"]
    assert {item["reason"] for item in result["partition"]["decisions"]} == {
        "NO_PARTITION_PREDICATE"}

def test_function_wrapped_is_undecidable(demo_table):
    result = plan(demo_table, "substr(dt, 1, 7) = '2024-01'")
    assert result["partition"]["all_selected"]
    assert result["partition"]["unknown_count"] == 5
    assert "FUNCTION_WRAPPED_PARTITION:substr(dt)" in result["partition"]["undecidable_reasons"]

def test_not_complement_and_or_union(demo_table):
    complement = plan(demo_table, "NOT (region = 'alpha')")
    assert ids(complement["partition"]) == [
        "dt=2024-01-01/region=delta", "dt=2024-01-02/region=beta",
        "dt=2024-01-03/region=gamma"]
    union = plan(demo_table, "region = 'alpha' OR dt = '2024-01-03'")
    assert union["partition"]["selected_count"] == 3

def test_in_on_partition_and_sorted_decisions(demo_table):
    result = plan(demo_table, "region IN ('alpha', 'beta')")
    assert result["partition"]["selected_count"] == 3
    decisions = [item["partition_id"] for item in result["partition"]["decisions"]]
    assert decisions == sorted(decisions)

def test_no_predicate_keeps_everything(demo_table):
    result = plan(demo_table, None)
    assert result["partition"]["selected_count"] == 5
    assert all(item["reason"] == "NO_PREDICATE" for item in result["partition"]["decisions"])
