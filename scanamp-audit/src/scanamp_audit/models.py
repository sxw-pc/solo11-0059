"""Serializable domain models shared by loader, engine, reports and ledger."""
from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SEVERITIES = ("info", "warning", "critical")
SEVERITY_RANK = {"critical": 0, "warning": 1, "info": 2}

@dataclass(frozen=True)
class ColumnDef:
    name: str
    type: str
    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "type": self.type}

@dataclass
class ColumnStats:
    type: str
    min: Any = None
    max: Any = None
    null_count: int | None = None
    distinct_count: int | None = None
    has_stats: bool = True
    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "min": self.min,
            "max": self.max,
            "null_count": self.null_count,
            "distinct_count": self.distinct_count,
            "has_stats": self.has_stats,
        }

@dataclass
class ManifestFile:
    path: str
    partition_values: dict[str, Any]
    row_count: int
    byte_size: int
    columns: dict[str, ColumnStats] = field(default_factory=dict)
    inferred_partition_values: dict[str, Any] = field(default_factory=dict)
    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "partition_values": dict(self.partition_values),
            "row_count": self.row_count,
            "byte_size": self.byte_size,
            "columns": {name: stats.to_dict() for name, stats in self.columns.items()},
        }

@dataclass
class Partition:
    partition_id: str
    values: dict[str, Any]
    files: list[ManifestFile]
    @property
    def file_count(self) -> int:
        return len(self.files)
    @property
    def row_count(self) -> int:
        return sum(item.row_count for item in self.files)
    @property
    def byte_size(self) -> int:
        return sum(item.byte_size for item in self.files)
    def to_dict(self) -> dict[str, Any]:
        return {
            "partition_id": self.partition_id,
            "values": dict(self.values),
            "file_count": self.file_count,
            "row_count": self.row_count,
            "byte_size": self.byte_size,
        }

@dataclass
class TableManifest:
    name: str
    description: str
    partition_columns: list[ColumnDef]
    columns: list[ColumnDef]
    files: list[ManifestFile]
    partitions: list[Partition]
    fingerprint: str
    path_mismatches: list[dict[str, Any]]
    source_name: str
    lake_root: Path | None = field(default=None, repr=False)
    @property
    def column_types(self) -> dict[str, str]:
        result = {col.name: col.type for col in self.columns}
        result.update({col.name: col.type for col in self.partition_columns})
        return result
    @property
    def partition_column_names(self) -> list[str]:
        return [col.name for col in self.partition_columns]
    @property
    def file_count(self) -> int:
        return len(self.files)
    @property
    def row_count(self) -> int:
        return sum(item.row_count for item in self.files)
    @property
    def byte_size(self) -> int:
        return sum(item.byte_size for item in self.files)
    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "partition_columns": [col.to_dict() for col in self.partition_columns],
            "columns": [col.to_dict() for col in self.columns],
            "files": [item.to_dict() for item in self.files],
            "partitions": [part.to_dict() for part in self.partitions],
            "totals": {
                "file_count": self.file_count,
                "row_count": self.row_count,
                "byte_size": self.byte_size,
                "partition_count": len(self.partitions),
            },
            "path_mismatches": list(self.path_mismatches),
        }

@dataclass
class Query:
    id: str
    description: str
    where: str | None
    predicate: dict[str, Any] | None
    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "description": self.description,
            "where": self.where,
            "predicate": self.predicate,
        }

@dataclass
class Workload:
    queries: list[Query]
    fingerprint: str
    source_name: str

@dataclass
class RuleConfig:
    enabled: bool
    severity: str
    thresholds: dict[str, float | int]
    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "severity": self.severity,
            "thresholds": dict(sorted(self.thresholds.items())),
        }

@dataclass
class AuditConfig:
    schema_version: str
    rules: dict[str, RuleConfig]
    default_selectivity: float
    top_n: int
    fingerprint: str
    source_name: str
    def rule(self, rule_id: str) -> RuleConfig:
        return self.rules[rule_id]
    def enabled(self, rule_id: str) -> bool:
        return self.rules[rule_id].enabled
    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "diagnostics": {
                rule_id: cfg.to_dict() for rule_id, cfg in sorted(self.rules.items())
            },
            "estimation": {"default_selectivity": self.default_selectivity},
            "report": {"top_n": self.top_n},
        }

@dataclass
class Diagnostic:
    rule_id: str
    severity: str
    message: str
    suggestion: str
    query_id: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    def sort_key(self) -> tuple[Any, ...]:
        import json
        return (
            SEVERITY_RANK.get(self.severity, 9),
            self.rule_id,
            self.query_id or "",
            json.dumps(self.evidence, sort_keys=True, ensure_ascii=False),
        )
    def to_dict(self, diagnostic_id: str | None = None) -> dict[str, Any]:
        data: dict[str, Any] = {
            "rule_id": self.rule_id,
            "severity": self.severity,
            "message": self.message,
            "query_id": self.query_id,
            "evidence": self.evidence,
            "suggestion": self.suggestion,
        }
        if diagnostic_id is not None:
            data = {"id": diagnostic_id, **data}
        return data

@dataclass
class AuditResult:
    run_id: str
    generated_at: str
    table: TableManifest
    workload: Workload
    config: AuditConfig
    queries: list[dict[str, Any]]
    diagnostics: list[dict[str, Any]]
    summary: dict[str, Any]
    inputs: dict[str, Any]
    layout_evaluation: dict[str, Any] = field(default_factory=dict)
    def critical_count(self) -> int:
        return sum(1 for item in self.diagnostics if item["severity"] == "critical")
    def warning_count(self) -> int:
        return sum(1 for item in self.diagnostics if item["severity"] == "warning")
    def info_count(self) -> int:
        return sum(1 for item in self.diagnostics if item["severity"] == "info")
    def max_severity(self) -> str | None:
        if self.critical_count():
            return "critical"
        if self.warning_count():
            return "warning"
        if self.info_count():
            return "info"
        return None
