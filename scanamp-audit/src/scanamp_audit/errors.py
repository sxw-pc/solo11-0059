"""Typed, location-aware errors for scanamp-audit.

Every failure carries an optional location, field and reason so callers can
point at the exact manifest/workload/config entry that caused the problem.
"""
from __future__ import annotations
from typing import Any

class ScanAmpError(Exception):
    """Base error with a stable, serializable structure."""
    code = "SCANAMP_ERROR"
    def __init__(
        self,
        message: str,
        *,
        location: str | None = None,
        field: str | None = None,
        reason: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.location = location
        self.field = field
        self.reason = reason
        self.details = details or {}
    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.location is not None:
            data["location"] = self.location
        if self.field is not None:
            data["field"] = self.field
        if self.reason is not None:
            data["reason"] = self.reason
        if self.details:
            data["details"] = self.details
        return data
    def __str__(self) -> str:
        parts = [self.message]
        if self.location:
            parts.append(f"location={self.location}")
        if self.field:
            parts.append(f"field={self.field}")
        if self.reason:
            parts.append(f"reason={self.reason}")
        return " | ".join(parts)

class ConfigError(ScanAmpError):
    code = "CONFIG_ERROR"

class ManifestError(ScanAmpError):
    code = "MANIFEST_ERROR"

class WorkloadError(ScanAmpError):
    code = "WORKLOAD_ERROR"

class PredicateError(ScanAmpError):
    code = "PREDICATE_ERROR"

class SecurityError(ScanAmpError):
    code = "SECURITY_ERROR"

class LedgerError(ScanAmpError):
    code = "LEDGER_ERROR"

class ReportError(ScanAmpError):
    code = "REPORT_ERROR"
def require(condition: bool, message: str, **kwargs: Any) -> None:
    """Raise a ScanAmpError when a validation condition is not met."""
    if not condition:
        raise ScanAmpError(message, **kwargs)
