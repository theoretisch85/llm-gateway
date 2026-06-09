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
    if tool_name in {"news.get_latest", "news.get_calm", "news.get_positive", "news.get_relevant"}:
        return _validate_news_query_args(args)
    if tool_name == "news.get_article":
        return _validate_news_article_args(args)
    if tool_name in {"news.get_sources", "news.trigger_ingest", "news.get_last_ingest"}:
        return _validate_news_source_query_args(args)
    if tool_name == "news.set_source_status":
        return _validate_news_set_source_status(args)
    if tool_name in {"mail.list_recent", "mail.classify_recent"}:
        return _validate_mail_list_args(args)
    if tool_name == "mail.get_message":
        return _validate_mail_get_message_args(args)

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


def _validate_news_query_args(args: dict[str, Any]) -> dict[str, Any]:
    validated = _validate_news_common_args(args)
    return validated


def _validate_news_article_args(args: dict[str, Any]) -> dict[str, Any]:
    validated = _validate_news_common_args(args)
    article_id = str(args.get("id") or args.get("article_id") or "").strip()
    if not article_id:
        raise ValueError("id ist erforderlich.")
    validated["id"] = article_id
    return validated


def _validate_news_source_query_args(args: dict[str, Any]) -> dict[str, Any]:
    validated: dict[str, Any] = {}
    source = args.get("source")
    if source is not None:
        source_value = str(source).strip()
        if source_value:
            validated["source"] = source_value
    return validated


def _validate_news_set_source_status(args: dict[str, Any]) -> dict[str, Any]:
    source_id = str(args.get("id") or args.get("source_id") or "").strip()
    if not source_id:
        raise ValueError("id ist erforderlich.")

    validated: dict[str, Any] = {"id": source_id}
    raw_payload = args.get("payload")
    if raw_payload is not None:
        if not isinstance(raw_payload, dict):
            raise ValueError("payload muss ein Objekt sein.")
        if not raw_payload:
            raise ValueError("payload darf nicht leer sein.")
        validated["payload"] = dict(raw_payload)
        return validated

    if args.get("enabled") is not None:
        validated["enabled"] = _coerce_bool(args.get("enabled"))
    if args.get("status") is not None:
        status_value = str(args.get("status")).strip()
        if not status_value:
            raise ValueError("status darf nicht leer sein.")
        validated["status"] = status_value

    if "enabled" not in validated and "status" not in validated:
        raise ValueError("payload oder enabled/status ist erforderlich.")

    return validated


def _validate_news_common_args(args: dict[str, Any]) -> dict[str, Any]:
    validated: dict[str, Any] = {}

    limit = args.get("limit")
    if limit is not None:
        try:
            validated_limit = int(limit)
        except (TypeError, ValueError) as exc:
            raise ValueError("limit muss eine Ganzzahl sein.") from exc
        if validated_limit < 1:
            raise ValueError("limit muss mindestens 1 sein.")
        validated["limit"] = validated_limit

    tone = args.get("tone")
    if tone is not None:
        tone_value = str(tone).strip()
        if not tone_value:
            raise ValueError("tone darf nicht leer sein.")
        validated["tone"] = tone_value

    source = args.get("source")
    if source is not None:
        source_value = str(source).strip()
        if not source_value:
            raise ValueError("source darf nicht leer sein.")
        validated["source"] = source_value

    min_relevance = args.get("min_relevance")
    if min_relevance is not None:
        try:
            validated["min_relevance"] = int(min_relevance)
        except (TypeError, ValueError) as exc:
            raise ValueError("min_relevance muss eine Ganzzahl sein.") from exc

    max_stress = args.get("max_stress")
    if max_stress is not None:
        try:
            validated["max_stress"] = int(max_stress)
        except (TypeError, ValueError) as exc:
            raise ValueError("max_stress muss eine Ganzzahl sein.") from exc

    return validated


def _validate_mail_list_args(args: dict[str, Any]) -> dict[str, Any]:
    validated: dict[str, Any] = {}

    limit = args.get("limit")
    if limit is not None:
        try:
            validated_limit = int(limit)
        except (TypeError, ValueError) as exc:
            raise ValueError("limit muss eine Ganzzahl sein.") from exc
        if validated_limit < 1 or validated_limit > 50:
            raise ValueError("limit muss zwischen 1 und 50 liegen.")
        validated["limit"] = validated_limit

    query = args.get("q")
    if query is not None:
        query_value = str(query).strip()
        if len(query_value) > 240:
            raise ValueError("q ist zu lang.")
        if query_value:
            validated["q"] = query_value

    label_ids = args.get("label_ids")
    if label_ids is not None:
        if isinstance(label_ids, str):
            labels = [item.strip() for item in label_ids.split(",") if item.strip()]
        elif isinstance(label_ids, list):
            labels = [str(item).strip() for item in label_ids if str(item).strip()]
        else:
            raise ValueError("label_ids muss String oder Array sein.")
        validated["label_ids"] = labels[:10]

    if args.get("include_spam_trash") is not None:
        validated["include_spam_trash"] = _coerce_bool(args.get("include_spam_trash"))

    return validated


def _validate_mail_get_message_args(args: dict[str, Any]) -> dict[str, Any]:
    message_id = str(args.get("message_id") or args.get("id") or "").strip()
    if not message_id:
        raise ValueError("message_id ist erforderlich.")
    if len(message_id) > 256:
        raise ValueError("message_id ist zu lang.")
    return {"message_id": message_id}


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
    raise ValueError("enabled muss true oder false sein.")


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
