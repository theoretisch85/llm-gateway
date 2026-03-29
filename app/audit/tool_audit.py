from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any


logger = logging.getLogger(__name__)
_AUDIT_FILE = Path("/opt/llm-gateway/.runtime/tool_audit.jsonl")
_AUDIT_LOCK = Lock()
_MAX_TEXT_LEN = 4000


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clip_text(value: str) -> str:
    text = value.strip()
    if len(text) <= _MAX_TEXT_LEN:
        return text
    return text[:_MAX_TEXT_LEN] + "..."


def _safe_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _clip_text(value)
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            result[str(key)] = _safe_value(item)
        return result
    if isinstance(value, (list, tuple, set)):
        return [_safe_value(item) for item in value]
    return _clip_text(str(value))


def audit_tool_execution(
    *,
    request_id: str,
    actor_id: str,
    actor_role: str,
    source: str,
    tool_name: str,
    arguments: dict[str, Any],
    duration_ms: float,
    ok: bool,
    result: Any = None,
    error: Exception | None = None,
) -> None:
    entry: dict[str, Any] = {
        "ts": _utc_timestamp(),
        "request_id": request_id or "-",
        "actor_id": actor_id or "unknown",
        "actor_role": actor_role or "unknown",
        "source": source or "unknown",
        "tool": tool_name,
        "arguments": _safe_value(arguments),
        "duration_ms": round(float(duration_ms), 2),
        "ok": bool(ok),
    }
    if ok:
        entry["result"] = _safe_value(result)
    elif error is not None:
        entry["error"] = {
            "type": error.__class__.__name__,
            "message": _clip_text(str(error)),
        }

    try:
        _AUDIT_FILE.parent.mkdir(parents=True, exist_ok=True)
        with _AUDIT_LOCK:
            with _AUDIT_FILE.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        logger.exception("failed to write tool audit entry tool=%s request_id=%s", tool_name, request_id)
