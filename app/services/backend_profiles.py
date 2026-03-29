from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4


PROFILE_FILE = Path("/opt/llm-gateway/.runtime/backend_profiles.json")


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


def _normalize_profile(item: dict[str, object], active_profile_id: str | None) -> dict[str, object]:
    return {
        "id": str(item.get("id") or ""),
        "name": str(item.get("name") or "backend-profile"),
        "public_model_name": str(item.get("public_model_name") or ""),
        "backend_model_name": str(item.get("backend_model_name") or ""),
        "base_url": str(item.get("base_url") or ""),
        "context_window": str(item.get("context_window") or ""),
        "response_reserve": str(item.get("response_reserve") or ""),
        "default_max_tokens": str(item.get("default_max_tokens") or ""),
        "ngl_layers": str(item.get("ngl_layers") or ""),
        "service_name": str(item.get("service_name") or ""),
        "activate_command": str(item.get("activate_command") or ""),
        "status_command": str(item.get("status_command") or ""),
        "logs_command": str(item.get("logs_command") or ""),
        "is_active": bool(item.get("id")) and item.get("id") == active_profile_id,
    }


def list_backend_profiles() -> list[dict[str, object]]:
    state = _load_state()
    active_profile_id = state.get("active_profile_id")
    items: list[dict[str, object]] = []
    for item in state.get("profiles") or []:
        if not isinstance(item, dict):
            continue
        items.append(_normalize_profile(item, active_profile_id))
    return items


def get_active_backend_profile() -> dict[str, object] | None:
    for profile in list_backend_profiles():
        if profile.get("is_active"):
            return profile
    return None


def get_backend_profile(profile_id: str) -> dict[str, object]:
    state = _load_state()
    active_profile_id = state.get("active_profile_id")
    for item in state.get("profiles") or []:
        if isinstance(item, dict) and item.get("id") == profile_id:
            return _normalize_profile(item, active_profile_id)
    raise RuntimeError("Backend-Profil wurde nicht gefunden.")


def save_backend_profile(
    profile_id: str | None,
    name: str,
    public_model_name: str,
    backend_model_name: str,
    base_url: str,
    service_name: str,
    context_window: str = "",
    response_reserve: str = "",
    default_max_tokens: str = "",
    ngl_layers: str = "",
    activate_command: str = "",
    status_command: str = "",
    logs_command: str = "",
    make_active: bool = False,
) -> dict[str, object]:
    clean_name = (name or "").strip()
    clean_public = (public_model_name or "").strip()
    clean_backend = (backend_model_name or "").strip()
    clean_base_url = (base_url or "").strip().rstrip("/")
    clean_context_window = (context_window or "").strip()
    clean_response_reserve = (response_reserve or "").strip()
    clean_default_max_tokens = (default_max_tokens or "").strip()
    clean_ngl_layers = (ngl_layers or "").strip()
    clean_service = (service_name or "").strip()
    clean_activate = (activate_command or "").strip()
    clean_status = (status_command or "").strip()
    clean_logs = (logs_command or "").strip()

    if not clean_name:
        raise RuntimeError("Profilname ist leer.")
    if not clean_public:
        raise RuntimeError("PUBLIC_MODEL_NAME fuer das Profil ist leer.")
    if not clean_backend:
        raise RuntimeError("BACKEND_MODEL_NAME fuer das Profil ist leer.")
    if not clean_base_url:
        raise RuntimeError("BASE_URL fuer das Profil ist leer.")

    state = _load_state()
    profiles = state.get("profiles") or []
    existing: dict[str, object] | None = None

    for item in profiles:
        if not isinstance(item, dict):
            continue
        if profile_id and item.get("id") == profile_id:
            existing = item
            break
        if not profile_id and item.get("name") == clean_name:
            existing = item
            break

    if existing is None:
        existing = {"id": uuid4().hex}
        profiles.append(existing)

    existing["name"] = clean_name
    existing["public_model_name"] = clean_public
    existing["backend_model_name"] = clean_backend
    existing["base_url"] = clean_base_url
    existing["context_window"] = clean_context_window
    existing["response_reserve"] = clean_response_reserve
    existing["default_max_tokens"] = clean_default_max_tokens
    existing["ngl_layers"] = clean_ngl_layers
    existing["service_name"] = clean_service
    existing["activate_command"] = clean_activate
    existing["status_command"] = clean_status
    existing["logs_command"] = clean_logs

    state["profiles"] = profiles
    if make_active:
        state["active_profile_id"] = existing["id"]
    _save_state(state)
    return _normalize_profile(existing, state.get("active_profile_id"))


def activate_backend_profile(profile_id: str) -> dict[str, object]:
    state = _load_state()
    for item in state.get("profiles") or []:
        if isinstance(item, dict) and item.get("id") == profile_id:
            state["active_profile_id"] = profile_id
            _save_state(state)
            return _normalize_profile(item, profile_id)
    raise RuntimeError("Backend-Profil wurde nicht gefunden.")


def clear_active_backend_profile(profile_id: str | None = None) -> dict[str, object]:
    state = _load_state()
    active_profile_id = state.get("active_profile_id")
    if profile_id and active_profile_id != profile_id:
        raise RuntimeError("Dieses Backend-Profil ist aktuell nicht aktiv.")

    active_profile: dict[str, object] | None = None
    for item in state.get("profiles") or []:
        if isinstance(item, dict) and item.get("id") == active_profile_id:
            active_profile = item
            break

    state["active_profile_id"] = None
    _save_state(state)
    return {
        "cleared": True,
        "previous_active_profile_id": str(active_profile_id or ""),
        "previous_active_profile_name": str((active_profile or {}).get("name") or ""),
    }


def delete_backend_profile(profile_id: str) -> dict[str, object]:
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
        raise RuntimeError("Backend-Profil wurde nicht gefunden.")

    state["profiles"] = kept
    if deleted_was_active:
        state["active_profile_id"] = None
    _save_state(state)
    return {"deleted": True, "deleted_was_active": deleted_was_active}


def build_runtime_updates_for_backend_profile(profile: dict[str, object]) -> dict[str, str]:
    # Keep the live gateway mapping in sync with the profile that was just
    # activated on the MI50 side.
    service_name = str(profile.get("service_name") or "").strip()
    activate_command = str(profile.get("activate_command") or "").strip()
    status_command = str(profile.get("status_command") or "").strip()
    logs_command = str(profile.get("logs_command") or "").strip()
    context_window = str(profile.get("context_window") or "").strip()
    response_reserve = str(profile.get("response_reserve") or "").strip()
    default_max_tokens = str(profile.get("default_max_tokens") or "").strip()
    updates = {
        "LLAMACPP_BASE_URL": str(profile.get("base_url") or "").strip(),
        "PUBLIC_MODEL_NAME": str(profile.get("public_model_name") or "").strip(),
        "BACKEND_MODEL_NAME": str(profile.get("backend_model_name") or "").strip(),
        "FAST_MODEL_PUBLIC_NAME": str(profile.get("public_model_name") or "").strip(),
        "FAST_MODEL_BACKEND_NAME": str(profile.get("backend_model_name") or "").strip(),
        "FAST_MODEL_BASE_URL": str(profile.get("base_url") or "").strip(),
        "DEEP_MODEL_PUBLIC_NAME": "",
        "DEEP_MODEL_BACKEND_NAME": "",
        "DEEP_MODEL_BASE_URL": "",
    }
    if context_window:
        updates["BACKEND_CONTEXT_WINDOW"] = context_window
    if response_reserve:
        updates["CONTEXT_RESPONSE_RESERVE"] = response_reserve
    if default_max_tokens:
        updates["DEFAULT_MAX_TOKENS"] = default_max_tokens
    if activate_command:
        updates["MI50_RESTART_COMMAND"] = activate_command
    elif service_name:
        updates["MI50_RESTART_COMMAND"] = f"sudo systemctl restart {service_name}"
    if status_command:
        updates["MI50_STATUS_COMMAND"] = status_command
    elif service_name:
        updates["MI50_STATUS_COMMAND"] = f"sudo systemctl status {service_name} --no-pager"
    if logs_command:
        updates["MI50_LOGS_COMMAND"] = logs_command
    elif service_name:
        updates["MI50_LOGS_COMMAND"] = f"journalctl -u {service_name} -n 80 --no-pager"
    return updates


def known_backend_service_names() -> list[str]:
    names: list[str] = []
    for profile in list_backend_profiles():
        service_name = str(profile.get("service_name") or "").strip()
        if service_name and service_name not in names:
            names.append(service_name)
    return names
