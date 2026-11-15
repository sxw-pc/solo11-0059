from typing import Any
from scanamp_audit.engine.file_pruner import prune_files
from scanamp_audit.engine.partition_pruner import prune_partitions
from scanamp_audit.expr.parser import parse_predicate
from scanamp_audit.expr.validate import analyze_predicate, validate_predicate

def plan(table, where: str | None, default: float = 0.5, files=None) -> dict[str, Any]:
    predicate = parse_predicate(where) if where else None
    validate_predicate(predicate, table.column_types)
    analysis = analyze_predicate(predicate, table.column_types, table.partition_column_names)
    partition = prune_partitions(table, predicate, analysis)
    if files is None:
        selected = set(partition["selected_ids"])
        files = [item for part in table.partitions if part.partition_id in selected
                 for item in part.files]
    file_result = prune_files(table, files, predicate, default, analysis["columns"])
    return {"predicate": predicate, "analysis": analysis, "partition": partition,
            "files": file_result}

def by_query(result, query_id: str):
    return next(item for item in result.queries if item["id"] == query_id)
