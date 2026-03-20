from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from app.services.database_admin import _redact_database_url


PROFILE_FILE = Path("/opt/llm-gateway/.runtime/database_profiles.json")


def _default_state() -> dict[str, object]:
    return {
        "active_profile_id": None,
        "profiles": [],
    }


def _ensure_parent_dir() -> None:
    PROFILE_FILE.parent.mkdir(parents=True, exist_ok=True)


def _load_state() -> dict[str, object]:
    _ensure_parent_dir()
    if not PROFILE_FILE.exists():
        return _default_state()

    try:
        data = json.loads(PROFILE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return _default_state()

    if not isinstance(data, dict):
        return _default_state()
    if not isinstance(data.get("profiles"), list):
        data["profiles"] = []
    return data


def _save_state(state: dict[str, object]) -> None:
    _ensure_parent_dir()
    PROFILE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _derive_profile_name(database_url: str) -> str:
    parts = urlsplit(database_url)
    db_name = (parts.path or "/").strip("/") or "database"
    host = parts.hostname or "postgres"
    return f"{host}/{db_name}"


def list_database_profiles(active_database_url: str | None = None) -> list[dict[str, object]]:
    state = _load_state()
    active_profile_id = state.get("active_profile_id")
    profiles = state.get("profiles") or []
    items: list[dict[str, object]] = []
    active_seen = False

    for item in profiles:
        if not isinstance(item, dict):
            continue
        database_url = str(item.get("database_url") or "")
        is_active = bool(item.get("id")) and item.get("id") == active_profile_id
        if active_database_url and database_url == active_database_url:
            is_active = True
            active_seen = True
        items.append(
            {
                "id": str(item.get("id") or ""),
                "name": str(item.get("name") or _derive_profile_name(database_url)),
                "database_url_redacted": _redact_database_url(database_url),
                "is_active": is_active,
                "is_ephemeral": False,
            }
        )

    if active_database_url and not active_seen:
        items.insert(
            0,
            {
                "id": "",
                "name": "aktive Verbindung (noch nicht als Profil gespeichert)",
                "database_url_redacted": _redact_database_url(active_database_url),
                "is_active": True,
                "is_ephemeral": True,
            },
        )

    return items


def save_database_profile(name: str, database_url: str, make_active: bool = True) -> dict[str, object]:
    state = _load_state()
    profiles = state.get("profiles") or []
    clean_url = database_url.strip()
    clean_name = (name or "").strip() or _derive_profile_name(clean_url)
    existing: dict[str, object] | None = None

    for item in profiles:
        if not isinstance(item, dict):
            continue
        if item.get("name") == clean_name:
            existing = item
            break

    if existing is None:
        existing = {
            "id": uuid4().hex,
            "name": clean_name,
            "database_url": clean_url,
        }
        profiles.append(existing)
    else:
        existing["name"] = clean_name
        existing["database_url"] = clean_url

    state["profiles"] = profiles
    if make_active:
        state["active_profile_id"] = existing["id"]
    _save_state(state)
    return {
        "id": existing["id"],
        "name": clean_name,
        "database_url": clean_url,
        "database_url_redacted": _redact_database_url(clean_url),
    }


def activate_database_profile(profile_id: str) -> str:
    state = _load_state()
    for item in state.get("profiles") or []:
        if isinstance(item, dict) and item.get("id") == profile_id:
            state["active_profile_id"] = profile_id
            _save_state(state)
            return str(item.get("database_url") or "")
    raise RuntimeError("Datenbank-Profil wurde nicht gefunden.")


def delete_database_profile(profile_id: str) -> dict[str, object]:
    state = _load_state()
    profiles = state.get("profiles") or []
    kept: list[dict[str, object]] = []
    deleted = False
    deleted_was_active = state.get("active_profile_id") == profile_id

    for item in profiles:
        if not isinstance(item, dict):
            continue
        if item.get("id") == profile_id:
            deleted = True
            continue
        kept.append(item)

    if not deleted:
        raise RuntimeError("Datenbank-Profil wurde nicht gefunden.")

    state["profiles"] = kept
    if deleted_was_active:
        state["active_profile_id"] = None
    _save_state(state)
    return {"deleted": True, "deleted_was_active": deleted_was_active}
