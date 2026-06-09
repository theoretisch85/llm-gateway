from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PROFILE_FILE = Path("/opt/llm-gateway/.runtime/model_profiles.json")


@dataclass(frozen=True)
class ModelProfile:
    profile_id: str
    display_name: str
    base_url: str
    backend_model: str
    public_model: str
    enabled: bool
    capabilities: tuple[str, ...]
    gpu_slot: str
    context_size: int | None


class ModelProfileError(RuntimeError):
    pass


class ModelProfileNotFoundError(ModelProfileError):
    pass


class ModelProfileDisabledError(ModelProfileError):
    pass


class ModelProfileInvalidError(ModelProfileError):
    pass


def _ensure_parent_dir() -> None:
    PROFILE_FILE.parent.mkdir(parents=True, exist_ok=True)


def _normalize_capabilities(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ("chat",)
    capabilities = [str(item).strip() for item in value if str(item).strip()]
    return tuple(capabilities or ["chat"])


def _normalize_context_size(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _normalize_profile(item: dict[str, Any]) -> ModelProfile:
    return ModelProfile(
        profile_id=str(item.get("profile_id") or "").strip(),
        display_name=str(item.get("display_name") or item.get("profile_id") or "Model Profile").strip(),
        base_url=str(item.get("base_url") or "").strip().rstrip("/"),
        backend_model=str(item.get("backend_model") or "").strip(),
        public_model=str(item.get("public_model") or item.get("profile_id") or "").strip(),
        enabled=bool(item.get("enabled", True)),
        capabilities=_normalize_capabilities(item.get("capabilities")),
        gpu_slot=str(item.get("gpu_slot") or "").strip(),
        context_size=_normalize_context_size(item.get("context_size")),
    )


def _profile_to_dict(profile: ModelProfile) -> dict[str, Any]:
    return {
        "profile_id": profile.profile_id,
        "display_name": profile.display_name,
        "base_url": profile.base_url,
        "backend_model": profile.backend_model,
        "public_model": profile.public_model,
        "enabled": profile.enabled,
        "capabilities": list(profile.capabilities),
        "gpu_slot": profile.gpu_slot,
        "context_size": profile.context_size,
    }


def _default_profiles(settings: Any) -> list[ModelProfile]:
    profiles: list[ModelProfile] = []
    fast = settings.effective_fast_model
    profiles.append(
        ModelProfile(
            profile_id="fast",
            display_name="Fast Model",
            base_url=fast.base_url,
            backend_model=fast.backend_name,
            public_model=fast.public_name,
            enabled=True,
            capabilities=("chat",),
            gpu_slot="",
            context_size=getattr(settings, "backend_context_window", None),
        )
    )

    deep = settings.effective_deep_model
    if deep.public_name and deep.backend_name:
        profiles.append(
            ModelProfile(
                profile_id="deep",
                display_name="Deep Model",
                base_url=deep.base_url,
                backend_model=deep.backend_name,
                public_model=deep.public_name,
                enabled=True,
                capabilities=("chat", "reasoning"),
                gpu_slot="",
                context_size=getattr(settings, "backend_context_window", None),
            )
        )
    return profiles


def _load_raw_state() -> dict[str, Any] | None:
    _ensure_parent_dir()
    if not PROFILE_FILE.exists():
        return None

    try:
        data = json.loads(PROFILE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None

    if not isinstance(data, dict):
        return None
    if not isinstance(data.get("profiles"), list):
        data["profiles"] = []
    return data


def _save_profiles(profiles: list[ModelProfile]) -> None:
    _ensure_parent_dir()
    payload = {"profiles": [_profile_to_dict(profile) for profile in profiles]}
    PROFILE_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def list_model_profiles(settings: Any, *, include_disabled: bool = False) -> list[ModelProfile]:
    state = _load_raw_state()
    if state is None:
        profiles = _default_profiles(settings)
        _save_profiles(profiles)
    else:
        profiles = []
        for item in state.get("profiles") or []:
            if isinstance(item, dict):
                profiles.append(_normalize_profile(item))

    if include_disabled:
        return profiles
    return [profile for profile in profiles if profile.enabled]


def resolve_model_profile(settings: Any, model_name: str) -> ModelProfile:
    requested = (model_name or "").strip()
    profiles = list_model_profiles(settings, include_disabled=True)

    for profile in profiles:
        if profile.profile_id == requested:
            return _validate_profile(profile)

    for profile in profiles:
        if profile.public_model == requested:
            return _validate_profile(profile)

    raise ModelProfileNotFoundError(f"Model profile not found: {requested}")


def _validate_profile(profile: ModelProfile) -> ModelProfile:
    if not profile.enabled:
        raise ModelProfileDisabledError(f"Model profile disabled: {profile.profile_id}")
    if not profile.base_url:
        raise ModelProfileInvalidError(f"Model profile base_url missing: {profile.profile_id}")
    if not profile.backend_model:
        raise ModelProfileInvalidError(f"Model profile backend_model missing: {profile.profile_id}")
    if not profile.public_model:
        raise ModelProfileInvalidError(f"Model profile public_model missing: {profile.profile_id}")
    return profile
