"""SQLite run ledger with idempotent, per-run replacement semantics.

Overwriting an existing ``run_id`` first archives the replaced version's
per-query pruning detail (per-partition and per-file decisions plus
metrics) as an immutable, append-only row in ``run_versions``; see
``versions.py`` for the snapshot and diff read model.
"""
from __future__ import annotations
import json
import sqlite3
from pathlib import Path
from typing import Any
from . import versions
from .errors import LedgerError
from .models import AuditResult
from .security import validate_ledger_path, validate_run_id

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  table_name TEXT NOT NULL,
  lake_name TEXT NOT NULL,
  workload_file TEXT NOT NULL,
  config_file TEXT NOT NULL,
  query_count INTEGER NOT NULL,
  diagnostic_count INTEGER NOT NULL,
  critical_count INTEGER NOT NULL,
  warning_count INTEGER NOT NULL,
  info_count INTEGER NOT NULL,
  table_files INTEGER NOT NULL,
  table_bytes INTEGER NOT NULL,
  scanned_files INTEGER NOT NULL,
  scanned_bytes INTEGER NOT NULL,
  estimated_matched_bytes INTEGER NOT NULL,
  scan_amplification REAL NOT NULL,
  summary_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS queries (
  run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
  query_id TEXT NOT NULL,
  scanned_files INTEGER NOT NULL,
  scanned_bytes INTEGER NOT NULL,
  scanned_rows INTEGER NOT NULL,
  estimated_matched_bytes INTEGER NOT NULL,
  scan_amplification REAL NOT NULL,
  partition_prune_ratio REAL NOT NULL,
  stats_prune_ratio REAL NOT NULL,
  diagnostic_count INTEGER NOT NULL,
  details_json TEXT NOT NULL,
  PRIMARY KEY (run_id, query_id)
);
CREATE TABLE IF NOT EXISTS diagnostics (
  run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
  seq INTEGER NOT NULL,
  diagnostic_id TEXT NOT NULL,
  rule_id TEXT NOT NULL,
  severity TEXT NOT NULL,
  query_id TEXT,
  message TEXT NOT NULL,
  evidence_json TEXT NOT NULL,
  suggestion TEXT NOT NULL,
  PRIMARY KEY (run_id, seq)
);
CREATE TABLE IF NOT EXISTS artifacts (
  run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  byte_size INTEGER NOT NULL,
  PRIMARY KEY (run_id, name)
);
CREATE TABLE IF NOT EXISTS input_fingerprints (
  run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
  kind TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  PRIMARY KEY (run_id, kind)
);
-- Archived pre-overwrite versions.  Intentionally no foreign key to runs:
-- snapshots must survive replacement of the live run row and are never
-- updated or deleted (append-only, immutable evidence).
CREATE TABLE IF NOT EXISTS run_versions (
  run_id TEXT NOT NULL,
  version INTEGER NOT NULL,
  recorded_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  summary_json TEXT NOT NULL,
  queries_json TEXT NOT NULL,
  PRIMARY KEY (run_id, version)
);
"""

class Ledger:
    def __init__(
        self,
        path: str | Path,
        *,
        allow_outside: bool = False,
        cwd: Path | None = None,
        read_only: bool = False,
    ) -> None:
        self.path = validate_ledger_path(path, cwd=cwd, allow_outside=allow_outside)
        self.read_only = read_only
        if read_only and not self.path.is_file():
            raise LedgerError("ledger does not exist", location=str(path), field="ledger",
                              reason="NOT_FOUND")
        if not read_only:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if read_only:
                self.connection = sqlite3.connect(
                    self.path.as_uri() + "?mode=ro", uri=True, timeout=5,
                    check_same_thread=False,
                )
            else:
                self.connection = sqlite3.connect(str(self.path), timeout=5)
        except sqlite3.Error as exc:
            raise LedgerError("cannot open SQLite ledger", location=str(path),
                              field="ledger", reason="OPEN_ERROR",
                              details={"error": str(exc)}) from exc
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        if read_only:
            self.connection.execute("PRAGMA query_only = ON")
        else:
            self.connection.executescript(_SCHEMA)
    def close(self) -> None:
        self.connection.close()
    def record_run(self, result: AuditResult, artifacts: list[dict[str, Any]]) -> None:
        validate_run_id(result.run_id)
        summary = result.summary
        run = (
            result.run_id,
            result.generated_at,
            summary["table"]["name"],
            result.inputs["lake_name"],
            result.inputs["workload_file"],
            result.inputs["config_file"],
            summary["counts"]["queries"],
            summary["counts"]["diagnostics"],
            summary["counts"]["critical"],
            summary["counts"]["warning"],
            summary["counts"]["info"],
            summary["table"]["file_count"],
            summary["table"]["byte_size"],
            summary["scan"]["scanned_files"],
            summary["scan"]["scanned_bytes"],
            summary["scan"]["estimated_matched_bytes"],
            summary["scan"]["scan_amplification"],
            json.dumps(summary, sort_keys=True, ensure_ascii=False),
        )
        try:
            with self.connection:
                versions.archive_current(self.connection, result.run_id)
                for table in ("diagnostics", "queries", "artifacts", "input_fingerprints", "runs"):
                    self.connection.execute(f"DELETE FROM {table} WHERE run_id = ?", (result.run_id,))
                self.connection.execute(
                    "INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", run
                )
                for query in result.queries:
                    details = {
                        "id": query["id"],
                        "where": query["where"],
                        "metrics": query["metrics"],
                        "selected_partitions": query["partition"]["selected_count"],
                        "partition_count": query["partition"]["partition_count"],
                        "diagnostic_ids": query["diagnostic_ids"],
                        "partition_decisions": query["partition"]["decisions"],
                        "file_decisions": query["files"]["decisions"],
                    }
                    self.connection.execute(
                        "INSERT INTO queries VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            result.run_id, query["id"], query["metrics"]["scanned_files"],
                            query["metrics"]["scanned_bytes"], query["metrics"]["scanned_rows"],
                            query["metrics"]["estimated_matched_bytes"],
                            query["metrics"]["scan_amplification"],
                            query["metrics"]["partition_prune_ratio"],
                            query["metrics"]["stats_prune_ratio"],
                            query["diagnostic_count"],
                            json.dumps(details, sort_keys=True, ensure_ascii=False),
                        ),
                    )
                for index, item in enumerate(result.diagnostics, 1):
                    self.connection.execute(
                        "INSERT INTO diagnostics VALUES (?,?,?,?,?,?,?,?,?)",
                        (
                            result.run_id, index, item["id"], item["rule_id"],
                            item["severity"], item["query_id"], item["message"],
                            json.dumps(item["evidence"], sort_keys=True, ensure_ascii=False),
                            item["suggestion"],
                        ),
                    )
                for item in sorted(artifacts, key=lambda artifact: artifact["name"]):
                    self.connection.execute(
                        "INSERT INTO artifacts VALUES (?,?,?,?)",
                        (result.run_id, item["name"], item["sha256"], item["byte_size"]),
                    )
                for kind, digest in sorted(summary["fingerprints"].items()):
                    self.connection.execute(
                        "INSERT INTO input_fingerprints VALUES (?,?,?)",
                        (result.run_id, kind, digest),
                    )
        except sqlite3.Error as exc:
            raise LedgerError("failed to write ledger rows", location=str(self.path),
                              field="ledger", reason="WRITE_ERROR",
                              details={"error": str(exc)}) from exc
    def list_versions(self, run_id: str) -> list[dict[str, Any]]:
        """Version history of a run: archived snapshots plus the live version."""
        validate_run_id(run_id)
        return versions.list_versions(self.connection, run_id)
    def get_version(self, run_id: str, version: int | str = "current") -> dict[str, Any] | None:
        """One version of a run's pruning detail; ``None`` if the run is unknown."""
        validate_run_id(run_id)
        return versions.get_version(self.connection, run_id, version)
    def diff_versions(
        self, run_id: str, old: int | str = "previous", new: int | str = "current"
    ) -> dict[str, Any] | None:
        """Pruning-decision diff between two versions, rebuilt from the ledger only."""
        validate_run_id(run_id)
        return versions.diff_versions(self.connection, run_id, old, new)
    def recent_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        try:
            rows = self.connection.execute(
                "SELECT * FROM runs ORDER BY created_at DESC, run_id DESC LIMIT ?", (limit,)
            ).fetchall()
        except sqlite3.Error as exc:
            raise LedgerError("failed to read ledger", location=str(self.path),
                              field="ledger", reason="READ_ERROR",
                              details={"error": str(exc)}) from exc
        return [self._run_dict(row) for row in rows]
    def get_run(self, run_id: str) -> dict[str, Any] | None:
        validate_run_id(run_id)
        row = self.connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        run = self._run_dict(row)
        run["queries"] = [
            {**json.loads(item["details_json"]), "query_id": item["query_id"]}
            for item in self.connection.execute(
                "SELECT * FROM queries WHERE run_id = ? ORDER BY query_id", (run_id,)
            ).fetchall()
        ]
        run["diagnostics"] = [
            {
                "seq": item["seq"], "id": item["diagnostic_id"], "rule_id": item["rule_id"],
                "severity": item["severity"], "query_id": item["query_id"],
                "message": item["message"], "suggestion": item["suggestion"],
                "evidence": json.loads(item["evidence_json"]),
            }
            for item in self.connection.execute(
                "SELECT * FROM diagnostics WHERE run_id = ? ORDER BY seq", (run_id,)
            ).fetchall()
        ]
        run["artifacts"] = [
            dict(item) for item in self.connection.execute(
                "SELECT name, sha256, byte_size FROM artifacts WHERE run_id = ? ORDER BY name",
                (run_id,),
            ).fetchall()
        ]
        run["fingerprints"] = {
            item["kind"]: item["sha256"] for item in self.connection.execute(
                "SELECT kind, sha256 FROM input_fingerprints WHERE run_id = ? ORDER BY kind",
                (run_id,),
            ).fetchall()
        }
        return run
    @staticmethod
    def _run_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "run_id": row["run_id"],
            "created_at": row["created_at"],
            "table_name": row["table_name"],
            "lake_name": row["lake_name"],
            "workload_file": row["workload_file"],
            "config_file": row["config_file"],
            "query_count": row["query_count"],
            "diagnostic_count": row["diagnostic_count"],
            "critical_count": row["critical_count"],
            "warning_count": row["warning_count"],
            "info_count": row["info_count"],
            "table_files": row["table_files"],
            "table_bytes": row["table_bytes"],
            "scanned_files": row["scanned_files"],
            "scanned_bytes": row["scanned_bytes"],
            "estimated_matched_bytes": row["estimated_matched_bytes"],
            "scan_amplification": row["scan_amplification"],
            "summary": json.loads(row["summary_json"]),
        }
