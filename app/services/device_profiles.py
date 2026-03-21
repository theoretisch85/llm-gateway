from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4


PROFILE_FILE = Path("/opt/llm-gateway/.runtime/device_profiles.json")


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


def _redact_token(token: str) -> str:
    clean_token = (token or "").strip()
    if not clean_token:
        return "-"
    if len(clean_token) <= 8:
        return "*" * len(clean_token)
    return f"{clean_token[:4]}***{clean_token[-4:]}"


def list_device_profiles() -> list[dict[str, object]]:
    # Device profiles stay outside .env because each Pi can have its own SSH
    # target and token, but only one active token is mirrored into runtime config.
    state = _load_state()
    active_profile_id = state.get("active_profile_id")
    items: list[dict[str, object]] = []

    for item in state.get("profiles") or []:
        if not isinstance(item, dict):
            continue
        items.append(
            {
                "id": str(item.get("id") or ""),
                "name": str(item.get("name") or "Pi Device"),
                "gateway_base_url": str(item.get("gateway_base_url") or ""),
                "device_token": str(item.get("device_token") or ""),
                "device_token_redacted": _redact_token(str(item.get("device_token") or "")),
                "ssh_host": str(item.get("ssh_host") or ""),
                "ssh_user": str(item.get("ssh_user") or ""),
                "ssh_port": str(item.get("ssh_port") or "22"),
                "remote_dir": str(item.get("remote_dir") or "~/kai-pi"),
                "ssh_root_prefix": str(item.get("ssh_root_prefix") or "sudo -n"),
                "notes": str(item.get("notes") or ""),
                "is_active": bool(item.get("id")) and item.get("id") == active_profile_id,
            }
        )
    return items


def get_device_profile(profile_id: str) -> dict[str, object]:
    clean_profile_id = (profile_id or "").strip()
    if not clean_profile_id:
        raise RuntimeError("Device-Profil-ID fehlt.")
    for item in list_device_profiles():
        if str(item.get("id") or "") == clean_profile_id:
            return item
    raise RuntimeError("Device-Profil wurde nicht gefunden.")


def get_active_device_profile() -> dict[str, object] | None:
    for item in list_device_profiles():
        if item.get("is_active"):
            return item
    return None


def save_device_profile(
    *,
    profile_id: str | None,
    name: str,
    gateway_base_url: str,
    device_token: str,
    ssh_host: str,
    ssh_user: str,
    ssh_port: str,
    remote_dir: str,
    ssh_root_prefix: str,
    notes: str = "",
    make_active: bool = False,
) -> dict[str, object]:
    clean_name = (name or "").strip() or "Pi Device"
    clean_gateway_base_url = (gateway_base_url or "").strip().rstrip("/")
    clean_device_token = (device_token or "").strip()
    clean_ssh_host = (ssh_host or "").strip()
    clean_ssh_user = (ssh_user or "").strip()
    clean_ssh_port = (ssh_port or "").strip() or "22"
    clean_remote_dir = (remote_dir or "").strip() or "~/kai-pi"
    clean_ssh_root_prefix = (ssh_root_prefix or "").strip() or "sudo -n"
    clean_notes = (notes or "").strip()

    if not clean_gateway_base_url:
        raise RuntimeError("Gateway-Base-URL fuer das Pi-Profil fehlt.")
    if not clean_device_token:
        raise RuntimeError("Device-Token fuer das Pi-Profil fehlt.")
    if not clean_ssh_host:
        raise RuntimeError("PI_SSH_HOST fehlt.")
    if not clean_ssh_user:
        raise RuntimeError("PI_SSH_USER fehlt.")

    state = _load_state()
    profiles = state.get("profiles") or []
    existing: dict[str, object] | None = None
    clean_profile_id = (profile_id or "").strip()

    for item in profiles:
        if not isinstance(item, dict):
            continue
        if clean_profile_id and item.get("id") == clean_profile_id:
            existing = item
            break

    if existing is None:
        existing = {"id": clean_profile_id or uuid4().hex}
        profiles.append(existing)

    existing.update(
        {
            "id": existing["id"],
            "name": clean_name,
            "gateway_base_url": clean_gateway_base_url,
            "device_token": clean_device_token,
            "ssh_host": clean_ssh_host,
            "ssh_user": clean_ssh_user,
            "ssh_port": clean_ssh_port,
            "remote_dir": clean_remote_dir,
            "ssh_root_prefix": clean_ssh_root_prefix,
            "notes": clean_notes,
        }
    )

    state["profiles"] = profiles
    if make_active:
        state["active_profile_id"] = existing["id"]
    _save_state(state)
    return get_device_profile(str(existing["id"]))


def activate_device_profile(profile_id: str) -> dict[str, object]:
    state = _load_state()
    clean_profile_id = (profile_id or "").strip()
    for item in state.get("profiles") or []:
        if isinstance(item, dict) and item.get("id") == clean_profile_id:
            state["active_profile_id"] = clean_profile_id
            _save_state(state)
            return get_device_profile(clean_profile_id)
    raise RuntimeError("Device-Profil wurde nicht gefunden.")


def delete_device_profile(profile_id: str) -> dict[str, object]:
    state = _load_state()
    clean_profile_id = (profile_id or "").strip()
    deleted = False
    deleted_was_active = state.get("active_profile_id") == clean_profile_id
    kept: list[dict[str, object]] = []

    for item in state.get("profiles") or []:
        if not isinstance(item, dict):
            continue
        if item.get("id") == clean_profile_id:
            deleted = True
            continue
        kept.append(item)

    if not deleted:
        raise RuntimeError("Device-Profil wurde nicht gefunden.")

    state["profiles"] = kept
    if deleted_was_active:
        state["active_profile_id"] = None
    _save_state(state)
    return {"deleted": True, "deleted_was_active": deleted_was_active}
