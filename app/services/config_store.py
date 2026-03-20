from pathlib import Path

from app.config import get_settings


ENV_FILE = Path("/opt/llm-gateway/.env")
ALLOWED_KEYS = {
    "LLAMACPP_BASE_URL",
    "LLAMACPP_TIMEOUT_SECONDS",
    "PUBLIC_MODEL_NAME",
    "BACKEND_MODEL_NAME",
    "BACKEND_CONTEXT_WINDOW",
    "CONTEXT_RESPONSE_RESERVE",
    "CONTEXT_CHARS_PER_TOKEN",
    "DEFAULT_MAX_TOKENS",
    "MI50_SSH_HOST",
    "MI50_SSH_USER",
    "MI50_SSH_PORT",
    "MI50_RESTART_COMMAND",
    "MI50_STATUS_COMMAND",
    "MI50_LOGS_COMMAND",
    "MI50_ROCM_SMI_COMMAND",
    "FAST_MODEL_PUBLIC_NAME",
    "FAST_MODEL_BACKEND_NAME",
    "FAST_MODEL_BASE_URL",
    "DEEP_MODEL_PUBLIC_NAME",
    "DEEP_MODEL_BACKEND_NAME",
    "DEEP_MODEL_BASE_URL",
    "ADMIN_DEFAULT_MODE",
    "ROUTING_DEEP_KEYWORDS",
    "ROUTING_LENGTH_THRESHOLD",
    "ROUTING_HISTORY_THRESHOLD",
    "DATABASE_URL",
    "HOME_ASSISTANT_BASE_URL",
    "HOME_ASSISTANT_TOKEN",
    "HOME_ASSISTANT_TIMEOUT_SECONDS",
    "HOME_ASSISTANT_ALLOWED_SERVICES",
    "HOME_ASSISTANT_ALLOWED_ENTITY_PREFIXES",
}


def read_runtime_config() -> dict[str, str]:
    values: dict[str, str] = {}
    if not ENV_FILE.exists():
        return values

    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        if "=" not in line or not line or line.lstrip().startswith("#"):
            continue
        key, value = line.split("=", 1)
        if key in ALLOWED_KEYS:
            values[key] = value
    return values


def write_runtime_config(updates: dict[str, str]) -> dict[str, str]:
    filtered_updates = {key: value for key, value in updates.items() if key in ALLOWED_KEYS}
    existing_lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
    seen_keys: set[str] = set()
    new_lines: list[str] = []

    for line in existing_lines:
        if "=" not in line or line.lstrip().startswith("#"):
            new_lines.append(line)
            continue

        key, _ = line.split("=", 1)
        if key in filtered_updates:
            new_lines.append(f"{key}={filtered_updates[key]}")
            seen_keys.add(key)
        else:
            new_lines.append(line)

    for key, value in filtered_updates.items():
        if key not in seen_keys:
            new_lines.append(f"{key}={value}")

    ENV_FILE.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    get_settings.cache_clear()
    return read_runtime_config()
