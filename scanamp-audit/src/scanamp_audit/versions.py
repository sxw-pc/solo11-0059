"""Immutable per-run version snapshots and pruning-decision diffs.

Every overwrite of a ``run_id`` archives the replaced version's per-query
pruning detail (per-partition and per-file decisions plus metrics) as an
append-only row in ``run_versions`` with a monotonically increasing
per-run version number and the write time.  The live tables keep their
replace-in-transaction semantics; the current state is addressable as the
version after the last snapshot, so any two versions of a run can be
diffed entirely from the ledger -- report files are never consulted.
"""
from __future__ import annotations
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any
from .errors import LedgerError

_SCANNED_KEYS = ("scanned_files", "scanned_bytes", "scanned_rows")
_ESTIMATE_KEYS = ("estimated_matched_rows", "estimated_matched_bytes")

def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
def _snapshot_query(query_id: str, details: dict[str, Any]) -> dict[str, Any]:
    """Canonical, deterministically ordered per-query pruning snapshot."""
    return {
        "query_id": query_id,
        "metrics": details.get("metrics", {}),
        "partition_decisions": sorted(
            details.get("partition_decisions", []), key=lambda item: item["partition_id"]
        ),
        "file_decisions": sorted(
            details.get("file_decisions", []), key=lambda item: item["path"]
        ),
    }
def _live_queries(conn: sqlite3.Connection, run_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT query_id, details_json FROM queries WHERE run_id = ? ORDER BY query_id",
        (run_id,),
    ).fetchall()
    return [_snapshot_query(row["query_id"], json.loads(row["details_json"])) for row in rows]
def archive_current(conn: sqlite3.Connection, run_id: str) -> None:
    """Snapshot the currently stored version of ``run_id`` before replacement."""
    row = conn.execute(
        "SELECT created_at, summary_json FROM runs WHERE run_id = ?", (run_id,)
    ).fetchone()
    if row is None:
        return
    version = conn.execute(
        "SELECT COALESCE(MAX(version), 0) + 1 AS next FROM run_versions WHERE run_id = ?",
        (run_id,),
    ).fetchone()["next"]
    payload = {
        "run_id": run_id,
        "created_at": row["created_at"],
        "queries": _live_queries(conn, run_id),
    }
    conn.execute(
        "INSERT INTO run_versions VALUES (?,?,?,?,?,?)",
        (run_id, version, _utc_now(), row["created_at"], row["summary_json"],
         json.dumps(payload, sort_keys=True, ensure_ascii=False)),
    )
def _snapshot_rows(conn: sqlite3.Connection, run_id: str) -> list[sqlite3.Row]:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'run_versions'"
    ).fetchone()
    if exists is None:  # ledger predates version snapshots
        return []
    return conn.execute(
        "SELECT version, created_at, recorded_at FROM run_versions "
        "WHERE run_id = ? ORDER BY version",
        (run_id,),
    ).fetchall()
def list_versions(conn: sqlite3.Connection, run_id: str) -> list[dict[str, Any]]:
    """Version history of a run: archived snapshots plus the live version."""
    row = conn.execute("SELECT created_at FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    if row is None:
        return []
    versions = [
        {"version": item["version"], "kind": "snapshot",
         "created_at": item["created_at"], "recorded_at": item["recorded_at"]}
        for item in _snapshot_rows(conn, run_id)
    ]
    versions.append({
        "version": versions[-1]["version"] + 1 if versions else 1,
        "kind": "current",
        "created_at": row["created_at"],
        "recorded_at": row["created_at"],
    })
    return versions
def _resolve(conn: sqlite3.Connection, run_id: str, version: int | str) -> tuple[int, str]:
    """Map a version specifier to a ``(number, kind)`` pair."""
    snapshots = {item["version"] for item in _snapshot_rows(conn, run_id)}
    current = max(snapshots, default=0) + 1
    if isinstance(version, str):
        if version in ("current", "latest"):
            return current, "current"
        if version == "previous" and snapshots:
            return max(snapshots), "snapshot"
        reason = "VERSION_NOT_FOUND" if version == "previous" else "INVALID_VERSION"
        message = (
            "run has no archived version to compare against"
            if version == "previous" else f"unknown version alias {version!r}"
        )
        raise LedgerError(message, field="version", reason=reason,
                          details={"run_id": run_id, "requested": version})
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise LedgerError(
            "version must be a positive integer, 'current'/'latest' or 'previous'",
            field="version", reason="INVALID_VERSION",
            details={"run_id": run_id, "requested": version},
        )
    if version == current:
        return current, "current"
    if version in snapshots:
        return version, "snapshot"
    raise LedgerError("version not found for run", field="version",
                      reason="VERSION_NOT_FOUND",
                      details={"run_id": run_id, "requested": version, "current": current})
def get_version(
    conn: sqlite3.Connection, run_id: str, version: int | str = "current"
) -> dict[str, Any] | None:
    """Return one version of a run's pruning detail, or ``None`` if unknown."""
    row = conn.execute(
        "SELECT created_at, summary_json FROM runs WHERE run_id = ?", (run_id,)
    ).fetchone()
    if row is None:
        return None
    number, kind = _resolve(conn, run_id, version)
    if kind == "snapshot":
        row = conn.execute(
            "SELECT created_at, recorded_at, summary_json, queries_json "
            "FROM run_versions WHERE run_id = ? AND version = ?",
            (run_id, number),
        ).fetchone()
        queries = json.loads(row["queries_json"])["queries"]
        recorded_at = row["recorded_at"]
    else:
        queries = _live_queries(conn, run_id)
        recorded_at = row["created_at"]
    return {
        "run_id": run_id,
        "version": number,
        "kind": kind,
        "created_at": row["created_at"],
        "recorded_at": recorded_at,
        "summary": json.loads(row["summary_json"]),
        "queries": queries,
    }
def _decisions(items: list[dict[str, Any]], key: str) -> dict[str, str]:
    return {item[key]: item["decision"] for item in items}
def _estimates(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        item["path"]: (
            item.get("estimated_selectivity"),
            item.get("estimated_matched_rows"),
            item.get("estimated_matched_bytes"),
        )
        for item in items
    }
def _delta(old: dict[str, Any] | None, new: dict[str, Any] | None, key: str) -> dict[str, Any]:
    left = old.get(key) if old is not None else None
    right = new.get(key) if new is not None else None
    delta = right - left if left is not None and right is not None else None
    return {"old": left, "new": right, "delta": delta}
def _query_diff(
    query_id: str, old: dict[str, Any] | None, new: dict[str, Any] | None
) -> dict[str, Any]:
    old_parts = _decisions(old["partition_decisions"], "partition_id") if old else {}
    new_parts = _decisions(new["partition_decisions"], "partition_id") if new else {}
    old_files = _decisions(old["file_decisions"], "path") if old else {}
    new_files = _decisions(new["file_decisions"], "path") if new else {}
    old_metrics = old["metrics"] if old else None
    new_metrics = new["metrics"] if new else None
    if old is None:
        change = "added"
    elif new is None:
        change = "removed"
    elif old_parts != new_parts or old_files != new_files:
        change = "decisions_changed"
    elif any(old_metrics.get(key) != new_metrics.get(key) for key in _SCANNED_KEYS):
        change = "metrics_changed"
    elif (
        any(old_metrics.get(key) != new_metrics.get(key) for key in _ESTIMATE_KEYS)
        or _estimates(old["file_decisions"]) != _estimates(new["file_decisions"])
    ):
        change = "estimate_only"
    else:
        change = "unchanged"
    old_selected = {pid for pid, decision in old_parts.items() if decision != "drop"}
    new_selected = {pid for pid, decision in new_parts.items() if decision != "drop"}
    return {
        "query_id": query_id,
        "change": change,
        "added_partitions": sorted(new_selected - old_selected),
        "removed_partitions": sorted(old_selected - new_selected),
        "partition_decisions": [
            {"partition_id": pid, "old": old_parts[pid], "new": new_parts[pid]}
            for pid in sorted(old_parts.keys() & new_parts.keys())
            if old_parts[pid] != new_parts[pid]
        ],
        "added_files": sorted(new_files.keys() - old_files.keys()),
        "removed_files": sorted(old_files.keys() - new_files.keys()),
        "file_decisions": [
            {"path": path, "old": old_files[path], "new": new_files[path]}
            for path in sorted(old_files.keys() & new_files.keys())
            if old_files[path] != new_files[path]
        ],
        "scanned_files": _delta(old_metrics, new_metrics, "scanned_files"),
        "scanned_bytes": _delta(old_metrics, new_metrics, "scanned_bytes"),
        "estimated_matched_bytes": _delta(old_metrics, new_metrics, "estimated_matched_bytes"),
    }
def _meta(version: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": version["version"],
        "kind": version["kind"],
        "created_at": version["created_at"],
        "recorded_at": version["recorded_at"],
    }
def diff_versions(
    conn: sqlite3.Connection, run_id: str, old: int | str = "previous",
    new: int | str = "current",
) -> dict[str, Any] | None:
    """Rebuild the deterministically ordered pruning diff between two versions.

    Per query it lists the selected-partition set changes (added/removed
    ``partition_id``), scanned-files/scanned-bytes changes and per-file
    decision changes; queries whose estimates changed while every pruning
    decision stayed the same are reported under ``estimate_only_queries``.
    Returns ``None`` when the run is unknown.
    """
    old_version = get_version(conn, run_id, old)
    if old_version is None:
        return None
    new_version = get_version(conn, run_id, new)
    if new_version is None:
        return None
    old_queries = {query["query_id"]: query for query in old_version["queries"]}
    new_queries = {query["query_id"]: query for query in new_version["queries"]}
    entries = [
        _query_diff(query_id, old_queries.get(query_id), new_queries.get(query_id))
        for query_id in sorted(old_queries.keys() | new_queries.keys())
    ]
    changed = [entry for entry in entries if entry["change"] != "unchanged"]
    totals = {}
    for key in ("scanned_files", "scanned_bytes", "estimated_matched_bytes"):
        old_total = sum(query["metrics"].get(key, 0) for query in old_version["queries"])
        new_total = sum(query["metrics"].get(key, 0) for query in new_version["queries"])
        totals[key] = {"old": old_total, "new": new_total, "delta": new_total - old_total}
    return {
        "run_id": run_id,
        "old": _meta(old_version),
        "new": _meta(new_version),
        "queries": changed,
        "estimate_only_queries": [
            entry["query_id"] for entry in changed if entry["change"] == "estimate_only"
        ],
        "added_queries": [entry["query_id"] for entry in changed if entry["change"] == "added"],
        "removed_queries": [
            entry["query_id"] for entry in changed if entry["change"] == "removed"
        ],
        "unchanged_queries": [
            entry["query_id"] for entry in entries if entry["change"] == "unchanged"
        ],
        "totals": totals,
    }
