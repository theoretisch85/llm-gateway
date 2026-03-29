from __future__ import annotations

import json
import re
from pathlib import Path

from app.services.backend_control import ops_command_catalog


CUSTOM_TOOLS_FILE = Path("/opt/llm-gateway/.runtime/mcp_custom_tools.json")
TOOL_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_.-]{2,63}$")


def _default_state() -> dict[str, object]:
    return {"tools": []}


def _ensure_parent_dir() -> None:
    CUSTOM_TOOLS_FILE.parent.mkdir(parents=True, exist_ok=True)


def _load_state() -> dict[str, object]:
    _ensure_parent_dir()
    if not CUSTOM_TOOLS_FILE.exists():
        return _default_state()

    try:
        data = json.loads(CUSTOM_TOOLS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return _default_state()

    if not isinstance(data, dict):
        return _default_state()
    if not isinstance(data.get("tools"), list):
        data["tools"] = []
    return data


def _save_state(state: dict[str, object]) -> None:
    _ensure_parent_dir()
    CUSTOM_TOOLS_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _normalize_tool(item: dict[str, object]) -> dict[str, str]:
    return {
        "name": str(item.get("name") or "").strip().lower(),
        "description": str(item.get("description") or "").strip(),
        "target": str(item.get("target") or "").strip().lower(),
        "command": str(item.get("command") or "").strip().lower(),
    }


def list_custom_mcp_tools() -> list[dict[str, str]]:
    state = _load_state()
    items: list[dict[str, str]] = []
    for item in state.get("tools") or []:
        if not isinstance(item, dict):
            continue
        normalized = _normalize_tool(item)
        if normalized["name"] and normalized["target"] and normalized["command"]:
            items.append(normalized)
    items.sort(key=lambda tool: tool["name"])
    return items


def _validate_tool_name(name: str) -> str:
    clean_name = (name or "").strip().lower()
    if not TOOL_NAME_PATTERN.fullmatch(clean_name):
        raise RuntimeError(
            "Tool-Name ungueltig. Erlaubt sind 3-64 Zeichen: a-z, 0-9, '.', '-' oder '_' "
            "(z. B. gateway.install_htop)."
        )
    return clean_name


def _validate_target_and_command(target: str, command: str) -> tuple[str, str]:
    clean_target = (target or "").strip().lower()
    clean_command = (command or "").strip().lower()
    catalog = ops_command_catalog()

    if clean_target not in catalog:
        raise RuntimeError(f"Unbekanntes Ops-Ziel: {clean_target or '-'}")
    if clean_command not in catalog[clean_target]:
        raise RuntimeError(f"Unbekannter Ops-Befehl fuer {clean_target}: {clean_command or '-'}")
    return clean_target, clean_command


def save_custom_mcp_tool(*, name: str, description: str, target: str, command: str) -> dict[str, str]:
    clean_name = _validate_tool_name(name)
    clean_target, clean_command = _validate_target_and_command(target, command)
    clean_description = (description or "").strip() or f"Custom Ops Tool: {clean_target}.{clean_command}"

    state = _load_state()
    tools = state.get("tools") or []
    existing: dict[str, object] | None = None
    for item in tools:
        if isinstance(item, dict) and str(item.get("name") or "").strip().lower() == clean_name:
            existing = item
            break

    if existing is None:
        existing = {}
        tools.append(existing)

    existing["name"] = clean_name
    existing["description"] = clean_description
    existing["target"] = clean_target
    existing["command"] = clean_command
    state["tools"] = tools
    _save_state(state)
    return _normalize_tool(existing)


def delete_custom_mcp_tool(name: str) -> dict[str, object]:
    clean_name = _validate_tool_name(name)
    state = _load_state()
    kept: list[dict[str, object]] = []
    deleted = False

    for item in state.get("tools") or []:
        if not isinstance(item, dict):
            continue
        item_name = str(item.get("name") or "").strip().lower()
        if item_name == clean_name:
            deleted = True
            continue
        kept.append(item)

    if not deleted:
        raise RuntimeError("Custom MCP-Tool wurde nicht gefunden.")

    state["tools"] = kept
    _save_state(state)
    return {"deleted": True, "name": clean_name}
