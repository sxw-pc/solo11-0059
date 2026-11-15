"""Shared deterministic formatting helpers for report writers."""
from __future__ import annotations
import html
import json
from typing import Any

def fmt(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:.6f}"
    if value is None:
        return "null"
    return str(value)
def md_escape(value: Any) -> str:
    text = fmt(value)
    text = text.replace("\\", "\\\\").replace("&", "&amp;").replace("<", "&lt;")
    text = text.replace(">", "&gt;").replace("|", "\\|").replace("`", "\\`")
    return " ".join(text.replace("\r", "\n").splitlines())
def html_escape(value: Any) -> str:
    return html.escape(fmt(value), quote=True)
def json_pretty(value: Any) -> str:
    return json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False)
def md_code(value: Any) -> str:
    return "`" + md_escape(value) + "`"
