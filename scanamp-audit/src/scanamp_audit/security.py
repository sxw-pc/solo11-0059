"""Path, host and run-id safety checks.

The tool is local-only. Manifest paths may never escape the lake root, and the
read-only server may only bind to loopback hosts supplied explicitly.
"""
from __future__ import annotations
import re
from pathlib import Path, PurePosixPath
from .errors import SecurityError

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_DRIVE_RE = re.compile(r"^[A-Za-z]:")
def validate_local_host(host: str) -> str:
    if not isinstance(host, str) or not host.strip():
        raise SecurityError(
            "server host must be a non-empty loopback address",
            field="host",
            reason="EMPTY_HOST",
            details={"allowed_hosts": sorted(LOCAL_HOSTS)},
        )
    normalized = host.strip().lower()
    if normalized not in LOCAL_HOSTS:
        raise SecurityError(
            "refusing to bind a non-loopback host",
            field="host",
            reason="NON_LOCAL_BIND",
            details={"requested": host, "allowed_hosts": sorted(LOCAL_HOSTS)},
        )
    return normalized
def validate_port(port: int) -> int:
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise SecurityError(
            "port must be an integer between 1 and 65535",
            field="port",
            reason="INVALID_PORT",
            details={"requested": port},
        )
    return port
def validate_run_id(run_id: str) -> str:
    if not isinstance(run_id, str) or not _RUN_ID_RE.match(run_id):
        raise SecurityError(
            "run id must match [A-Za-z0-9][A-Za-z0-9._-]{0,63}",
            field="run_id",
            reason="INVALID_RUN_ID",
            details={"requested": run_id},
        )
    return run_id
def normalize_manifest_path(raw: str, *, location: str = "manifest") -> str:
    """Validate a manifest-relative POSIX path and return a normalized form."""
    if not isinstance(raw, str) or not raw.strip():
        raise SecurityError(
            "file path must be a non-empty relative string",
            location=location,
            field="path",
            reason="EMPTY_PATH",
        )
    if "\x00" in raw:
        raise SecurityError(
            "file path contains a NUL byte",
            location=location,
            field="path",
            reason="NUL_BYTE",
        )
    normalized_sep = raw.replace("\\", "/")
    if normalized_sep.startswith("/") or _DRIVE_RE.match(normalized_sep):
        raise SecurityError(
            "absolute file paths are not allowed",
            location=location,
            field="path",
            reason="ABSOLUTE_PATH",
            details={"path": raw},
        )
    parts = PurePosixPath(normalized_sep).parts
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise SecurityError(
            "file path must not contain '.', '..' or empty components",
            location=location,
            field="path",
            reason="PATH_TRAVERSAL",
            details={"path": raw},
        )
    return "/".join(parts)
def safe_join(root: Path, relative: str, *, location: str = "manifest") -> Path:
    """Join a validated relative path to ``root`` and enforce containment."""
    normalized = normalize_manifest_path(relative, location=location)
    root_resolved = root.resolve(strict=False)
    candidate = (root_resolved / PurePosixPath(normalized)).resolve(strict=False)
    if candidate != root_resolved and not candidate.is_relative_to(root_resolved):
        raise SecurityError(
            "resolved file path escapes the lake root",
            location=location,
            field="path",
            reason="PATH_ESCAPE",
            details={"path": relative},
        )
    return candidate
def ensure_within_workdir(
    path: str | Path,
    *,
    cwd: Path | None = None,
    allow_outside: bool = False,
    kind: str = "path",
) -> Path:
    """Require a runtime path to stay inside the working directory."""
    candidate = Path(path).expanduser().resolve(strict=False)
    base = (cwd or Path.cwd()).resolve(strict=False)
    if not allow_outside and candidate != base and not candidate.is_relative_to(base):
        raise SecurityError(
            f"{kind} must be inside the working directory or explicitly allowed",
            location=str(path),
            field=kind,
            reason="OUTSIDE_WORKDIR",
            details={"workdir": str(base)},
        )
    return candidate
def validate_ledger_path(
    path: str | Path,
    *,
    cwd: Path | None = None,
    allow_outside: bool = False,
) -> Path:
    resolved = ensure_within_workdir(
        path, cwd=cwd, allow_outside=allow_outside, kind="ledger"
    )
    if resolved.exists() and resolved.is_dir():
        raise SecurityError(
            "ledger path points at a directory",
            location=str(path),
            field="ledger",
            reason="LEDGER_IS_DIRECTORY",
        )
    return resolved
