from __future__ import annotations

import re
from typing import Any

from app.config import Settings
from app.services.backend_control import ops_command_catalog


_SENSITIVE_ARG_KEYS = {
    "token",
    "password",
    "secret",
    "authorization",
    "api_key",
}


def validate_tool_arguments(*, tool_name: str, args: dict[str, Any], settings: Settings) -> dict[str, Any]:
    if not isinstance(args, dict):
        raise ValueError("Tool-Argumente muessen ein Objekt sein.")

    if tool_name == "ha.call":
        return _validate_ha_call(args, settings)
    if tool_name == "gateway.ops":
        return _validate_gateway_ops(args)
    if tool_name == "math.calculate":
        return _validate_math_calculate(args)

    return args


def sanitize_tool_arguments(args: dict[str, Any]) -> dict[str, Any]:
    return _sanitize_value(args)


def _validate_ha_call(args: dict[str, Any], settings: Settings) -> dict[str, Any]:
    domain = str(args.get("domain") or "").strip().lower()
    service = str(args.get("service") or "").strip().lower()
    if not domain or not service:
        raise ValueError("domain und service sind erforderlich.")

    service_data_raw = args.get("service_data")
    if service_data_raw is None:
        service_data: dict[str, Any] = {}
    elif isinstance(service_data_raw, dict):
        service_data = dict(service_data_raw)
    else:
        raise ValueError("service_data muss ein Objekt sein.")

    entity_id = args.get("entity_id")
    if entity_id is not None:
        entity_id = str(entity_id).strip()
        if not entity_id:
            entity_id = None

    full_service = f"{domain}.{service}"
    allowed_services = settings.parsed_home_assistant_allowed_services
    if allowed_services and full_service not in allowed_services:
        raise ValueError(f"Home Assistant service {full_service} ist nicht freigegeben.")

    entity_ids = _extract_entity_ids(entity_id=entity_id, service_data=service_data)
    allowed_prefixes = settings.parsed_home_assistant_allowed_entity_prefixes
    if allowed_prefixes:
        for item in entity_ids:
            lowered = item.lower()
            if not any(lowered.startswith(prefix) for prefix in allowed_prefixes):
                raise ValueError(f"Entity {item} ist nicht freigegeben.")

    return {
        "domain": domain,
        "service": service,
        "entity_id": entity_id,
        "service_data": service_data,
    }


def _validate_gateway_ops(args: dict[str, Any]) -> dict[str, Any]:
    command = str(args.get("command") or "").strip().lower()
    target = str(args.get("target") or "").strip().lower() or "gateway"

    if "." in command and "target" not in args:
        split_target, split_command = command.split(".", 1)
        if split_target and split_command:
            target = split_target
            command = split_command

    if not command:
        raise ValueError("command ist erforderlich.")
    if not re.fullmatch(r"[a-z0-9_]+", command):
        raise ValueError("Ops-Command ist ungueltig.")
    if not re.fullmatch(r"[a-z0-9_]+", target):
        raise ValueError("Ops-Target ist ungueltig.")

    catalog = ops_command_catalog()
    if target not in catalog:
        raise ValueError("Unbekanntes Ops-Ziel.")
    if command not in catalog[target]:
        raise ValueError(f"Unbekannter Ops-Befehl fuer {target}: {command}")

    return {
        "target": target,
        "command": command,
    }


def _validate_math_calculate(args: dict[str, Any]) -> dict[str, Any]:
    expression = str(args.get("expression") or args.get("input") or "").strip()
    if not expression:
        raise ValueError("expression ist erforderlich.")
    if len(expression) > 240:
        raise ValueError("expression ist zu lang.")
    return {"expression": expression}


def _extract_entity_ids(*, entity_id: str | None, service_data: dict[str, Any]) -> list[str]:
    result: list[str] = []
    if entity_id:
        result.append(entity_id)

    raw = service_data.get("entity_id")
    if isinstance(raw, str):
        result.extend(item.strip() for item in raw.split(",") if item.strip())
    elif isinstance(raw, list):
        result.extend(str(item).strip() for item in raw if str(item).strip())

    deduped: list[str] = []
    seen: set[str] = set()
    for item in result:
        if item in seen:
            continue
        seen.add(item)
        deduped.append(item)
    return deduped


def _sanitize_value(value: Any) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if key_text.lower() in _SENSITIVE_ARG_KEYS:
                result[key_text] = "***"
            else:
                result[key_text] = _sanitize_value(item)
        return result
    if isinstance(value, list):
        return [_sanitize_value(item) for item in value]
    return value
