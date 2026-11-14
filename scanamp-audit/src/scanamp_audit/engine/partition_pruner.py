"""Partition-level pruning with conservative three-valued decisions."""
from __future__ import annotations
from typing import Any
from ..expr.evaluator import FALSE, TRUE, UNKNOWN, evaluate
from ..models import Partition, TableManifest
from ..schema import round_float

def _resolver(values: dict[str, Any], table: TableManifest) -> dict[str, dict[str, Any]]:
    return {
        name: {"kind": "exact", "type": table.column_types[name], "value": value}
        for name, value in values.items()
    }
def prune_partitions(
    table: TableManifest, predicate: dict[str, Any] | None, analysis: dict[str, Any]
) -> dict[str, Any]:
    decisions: list[dict[str, Any]] = []
    selected: list[Partition] = []
    pruned: list[Partition] = []
    reasons = analysis.get("undecidable_reasons", [])
    partition_refs = analysis.get("partition_columns", [])
    for partition in table.partitions:
        if predicate is None:
            result, reason = TRUE, "NO_PREDICATE"
        else:
            result = evaluate(predicate, _resolver(partition.values, table), table.column_types)
            if result == TRUE:
                reason = "PARTITION_PREDICATE_TRUE"
            elif result == FALSE:
                reason = "PARTITION_PREDICATE_FALSE"
            elif reasons:
                reason = "UNDECIDABLE_PARTITION_PREDICATE"
            elif not partition_refs:
                reason = "NO_PARTITION_PREDICATE"
            else:
                reason = "PARTITION_PREDICATE_UNKNOWN_WITH_DATA"
        decision = "keep" if result == TRUE else "drop" if result == FALSE else "unknown"
        decisions.append(
            {
                "partition_id": partition.partition_id,
                "values": dict(partition.values),
                "decision": decision,
                "reason": reason,
            }
        )
        (pruned if result == FALSE else selected).append(partition)
    total = len(table.partitions)
    unknown_count = sum(1 for item in decisions if item["decision"] == "unknown")
    return {
        "partition_count": total,
        "selected_count": len(selected),
        "pruned_count": len(pruned),
        "unknown_count": unknown_count,
        "all_selected": len(selected) == total,
        "prune_ratio": round_float(len(pruned) / total) if total else 0.0,
        "selected_ids": [partition.partition_id for partition in selected],
        "selected_partitions": [partition.to_dict() for partition in selected],
        "pruned_partitions": [partition.to_dict() for partition in pruned],
        "decisions": decisions,
        "undecidable_reasons": list(reasons),
    }
