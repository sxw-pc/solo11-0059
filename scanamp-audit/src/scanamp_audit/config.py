"""Audit configuration: defaults, strict merge and deterministic fingerprint."""
from __future__ import annotations
from pathlib import Path
from typing import Any
import yaml
from .errors import ConfigError
from .models import SEVERITIES, AuditConfig, RuleConfig
from .schema import fingerprint

RULE_DEFAULTS: dict[str, tuple[str, dict[str, float | int]]] = {
    "PARTITION_KEY_UNFILTERED": ("warning", {}),
    "LOW_SELECTIVITY_PARTITION": (
        "warning",
        {
            "min_partition_prune_ratio": 0.5,
            "min_scanned_file_ratio": 0.5,
            "min_scanned_files": 2,
        },
    ),
    "HIGH_CARDINALITY_OVERPARTITION": (
        "info",
        {
            "min_partition_count": 4,
            "min_selected_partition_ratio": 0.8,
            "min_selected_partitions": 2,
        },
    ),
    "FUNCTION_WRAPPED_PARTITION": ("critical", {}),
    "STATS_MISSING": ("warning", {"min_missing_filter_columns": 1}),
    "PATH_MANIFEST_MISMATCH": ("critical", {}),
    "OR_BRANCH_BLOWUP": (
        "warning",
        {"min_branches": 2, "min_expansion_factor": 1.5},
    ),
    "UNKNOWN_PREDICATE_CONSERVATIVE": ("warning", {}),
}
SEVERITY_WEIGHT = {"none": 0, "info": 1, "warning": 2, "critical": 3}
_TOP_LEVEL_KEYS = {"schema_version", "diagnostics", "estimation", "report"}
def _read_yaml(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(
            "cannot read audit config",
            location=str(path),
            reason="READ_ERROR",
            details={"error": str(exc)},
        ) from exc
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(
            "audit config is not valid YAML",
            location=str(path),
            reason="YAML_ERROR",
            details={"error": str(exc)},
        ) from exc
def _expect_mapping(value: Any, location: str, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(
            "expected a mapping",
            location=location,
            field=field,
            reason="TYPE_ERROR",
            details={"actual": type(value).__name__},
        )
    return value
def _parse_rule(
    rule_id: str, raw: Any, location: str, defaults: tuple[str, dict[str, float | int]]
) -> RuleConfig:
    default_severity, default_thresholds = defaults
    if raw is None:
        raw = {}
    mapping = _expect_mapping(raw, location, f"diagnostics.{rule_id}")
    unknown = set(mapping) - {"enabled", "severity", "thresholds"}
    if unknown:
        raise ConfigError(
            "unknown rule configuration key",
            location=location,
            field=f"diagnostics.{rule_id}",
            reason="UNKNOWN_KEY",
            details={"keys": sorted(unknown)},
        )
    enabled = mapping.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ConfigError(
            "enabled must be a boolean",
            location=location,
            field=f"diagnostics.{rule_id}.enabled",
            reason="TYPE_ERROR",
        )
    severity = mapping.get("severity", default_severity)
    if severity not in SEVERITIES:
        raise ConfigError(
            "severity must be info, warning or critical",
            location=location,
            field=f"diagnostics.{rule_id}.severity",
            reason="INVALID_SEVERITY",
            details={"allowed": list(SEVERITIES)},
        )
    thresholds = dict(default_thresholds)
    raw_thresholds = mapping.get("thresholds", {})
    threshold_map = _expect_mapping(raw_thresholds, location, f"diagnostics.{rule_id}.thresholds")
    unknown_thresholds = set(threshold_map) - set(default_thresholds)
    if unknown_thresholds:
        raise ConfigError(
            "unknown threshold key for diagnostic rule",
            location=location,
            field=f"diagnostics.{rule_id}.thresholds",
            reason="UNKNOWN_THRESHOLD",
            details={"keys": sorted(unknown_thresholds), "allowed": sorted(default_thresholds)},
        )
    for key, value in threshold_map.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise ConfigError(
                "threshold must be a non-negative number",
                location=location,
                field=f"diagnostics.{rule_id}.thresholds.{key}",
                reason="INVALID_THRESHOLD",
            )
        if "ratio" in key and value > 1:
            raise ConfigError(
                "ratio threshold must be between 0 and 1",
                location=location,
                field=f"diagnostics.{rule_id}.thresholds.{key}",
                reason="INVALID_THRESHOLD",
            )
        thresholds[key] = value
    return RuleConfig(enabled=enabled, severity=severity, thresholds=thresholds)
def parse_config(data: Any, *, source_name: str = "audit.yaml", location: str = "config") -> AuditConfig:
    mapping = _expect_mapping(data, location, "$")
    unknown_top = set(mapping) - _TOP_LEVEL_KEYS
    if unknown_top:
        raise ConfigError(
            "unknown top-level config key",
            location=location,
            field="$",
            reason="UNKNOWN_KEY",
            details={"keys": sorted(unknown_top)},
        )
    schema_version = mapping.get("schema_version", "1.0")
    if schema_version != "1.0":
        raise ConfigError(
            "unsupported config schema_version",
            location=location,
            field="schema_version",
            reason="UNSUPPORTED_SCHEMA",
            details={"supported": ["1.0"]},
        )
    estimation = _expect_mapping(mapping.get("estimation", {}), location, "estimation")
    unknown_est = set(estimation) - {"default_selectivity"}
    if unknown_est:
        raise ConfigError(
            "unknown estimation key",
            location=location,
            field="estimation",
            reason="UNKNOWN_KEY",
            details={"keys": sorted(unknown_est)},
        )
    default_selectivity = estimation.get("default_selectivity", 0.5)
    if (
        isinstance(default_selectivity, bool)
        or not isinstance(default_selectivity, (int, float))
        or not 0 <= float(default_selectivity) <= 1
    ):
        raise ConfigError(
            "default_selectivity must be between 0 and 1",
            location=location,
            field="estimation.default_selectivity",
            reason="INVALID_SELECTIVITY",
        )
    report = _expect_mapping(mapping.get("report", {}), location, "report")
    unknown_report = set(report) - {"top_n"}
    if unknown_report:
        raise ConfigError(
            "unknown report key",
            location=location,
            field="report",
            reason="UNKNOWN_KEY",
            details={"keys": sorted(unknown_report)},
        )
    top_n = report.get("top_n", 10)
    if isinstance(top_n, bool) or not isinstance(top_n, int) or top_n < 1:
        raise ConfigError(
            "report.top_n must be a positive integer",
            location=location,
            field="report.top_n",
            reason="INVALID_TOP_N",
        )
    raw_rules = _expect_mapping(mapping.get("diagnostics", {}), location, "diagnostics")
    unknown_rules = set(raw_rules) - set(RULE_DEFAULTS)
    if unknown_rules:
        raise ConfigError(
            "unknown diagnostic rule id",
            location=location,
            field="diagnostics",
            reason="UNKNOWN_RULE",
            details={"rules": sorted(unknown_rules)},
        )
    rules = {
        rule_id: _parse_rule(rule_id, raw_rules.get(rule_id), location, defaults)
        for rule_id, defaults in RULE_DEFAULTS.items()
    }
    config = AuditConfig(
        schema_version="1.0",
        rules=rules,
        default_selectivity=float(default_selectivity),
        top_n=top_n,
        fingerprint="",
        source_name=source_name,
    )
    config.fingerprint = fingerprint(config.to_dict())
    return config
def load_config(path: str | Path) -> AuditConfig:
    config_path = Path(path)
    if not config_path.exists():
        raise ConfigError(
            "audit config file does not exist",
            location=str(path),
            reason="NOT_FOUND",
        )
    data = _read_yaml(config_path)
    return parse_config(data, source_name=config_path.name, location=str(path))
def fail_on_triggered(result: Any, fail_on: str) -> bool:
    """Return True when the result should fail a CI gate."""
    if fail_on not in SEVERITY_WEIGHT:
        raise ConfigError(
            "fail-on must be critical, warning, info or none",
            field="fail_on",
            reason="INVALID_FAIL_ON",
        )
    threshold = SEVERITY_WEIGHT[fail_on]
    if threshold == 0:
        return False
    for diagnostic in result.diagnostics:
        if SEVERITY_WEIGHT.get(diagnostic["severity"], 0) >= threshold:
            return True
    return False
