from __future__ import annotations

import json
import re
from dataclasses import dataclass

from app.config import Settings
from app.services.llamacpp_client import LlamaCppClient, LlamaCppError, LlamaCppTimeoutError


@dataclass
class HomeAssistantIntentDecision:
    intent: str
    service: str | None = None
    target: str | None = None
    domain_hint: str | None = None
    use_last_context: bool = False
    all_matches: bool = False
    temperature: float | None = None


async def classify_home_assistant_intent(
    settings: Settings,
    *,
    message: str,
    last_action_summary: str = "",
    alias_lines: list[str] | None = None,
) -> HomeAssistantIntentDecision | None:
    # This is a narrow classifier, not a free-form tool call. The model only
    # decides whether the message is normal chat, an HA lookup or an HA action.
    client = LlamaCppClient(settings)
    target = settings.effective_fast_model
    aliases = alias_lines or []

    system_prompt = (
        "Du klassifizierst deutsche Admin-Chat-Nachrichten fuer Home Assistant. "
        "Antworte nur mit einem JSON-Objekt und ohne Markdown. "
        "Erlaubte intents: chat, ha_query, ha_action. "
        "Erlaubte services: turn_on, turn_off, set_temperature oder null. "
        "Erlaubte domain_hint: light, switch, climate, script oder null. "
        "Regeln: "
        "1) Wenn der Nutzer ein Geraet steuern will -> ha_action. "
        "2) Wenn der Nutzer Entities suchen, listen oder identifizieren will -> ha_query. "
        "3) Wenn der Nutzer nur erklaert, diskutiert oder debuggt -> chat. "
        "4) 'auf' oder 'oeffnen' bedeutet meist turn_on fuer Schalter/Fenster. "
        "5) 'zu', 'schliessen', 'schliesse', 'schließe' oder 'aus' bedeutet meist turn_off. "
        "6) Wenn der Nutzer mit 'es', 'das', 'sie', 'wieder', 'nochmal' auf die letzte Aktion verweist, setze use_last_context=true. "
        "7) Wenn mehrere passende Ziele gemeint sind, setze all_matches=true. "
        "8) Wenn unsicher, lieber intent=chat. "
        "Schema: "
        '{"intent":"chat|ha_query|ha_action","service":"turn_on|turn_off|set_temperature|null","target":"kurze normalisierte phrase oder leer","domain_hint":"light|switch|climate|script|null","use_last_context":true,"all_matches":false,"temperature":null}'
    )

    context_lines: list[str] = []
    if last_action_summary:
        context_lines.append(f"Letzte HA-Aktion: {last_action_summary}")
    if aliases:
        context_lines.append("Bekannte HA-Aliase:")
        context_lines.extend(f"- {line}" for line in aliases[:12])

    user_prompt = "Nachricht:\n" + message.strip()
    if context_lines:
        user_prompt += "\n\nKontext:\n" + "\n".join(context_lines)

    payload = {
        "model": target.backend_name,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "stream": False,
        "temperature": 0.0,
        "max_tokens": 220,
    }

    try:
        response = await client.create_chat_completion(payload, base_url=target.base_url)
    except (LlamaCppError, LlamaCppTimeoutError):
        return None

    try:
        raw_content = str(response["choices"][0]["message"]["content"]).strip()
    except (KeyError, IndexError, TypeError):
        return None

    parsed = _extract_json_object(raw_content)
    if not isinstance(parsed, dict):
        return None

    intent = str(parsed.get("intent") or "").strip().lower()
    if intent not in {"chat", "ha_query", "ha_action"}:
        return None

    service = parsed.get("service")
    normalized_service = str(service).strip().lower() if isinstance(service, str) and service.strip() else None
    if normalized_service not in {None, "turn_on", "turn_off", "set_temperature"}:
        normalized_service = None

    domain_hint = parsed.get("domain_hint")
    normalized_domain = str(domain_hint).strip().lower() if isinstance(domain_hint, str) and domain_hint.strip() else None
    if normalized_domain not in {None, "light", "switch", "climate", "script"}:
        normalized_domain = None

    target_text = parsed.get("target")
    normalized_target = str(target_text).strip() if isinstance(target_text, str) else ""

    temperature = None
    raw_temperature = parsed.get("temperature")
    if isinstance(raw_temperature, (int, float)):
        temperature = float(raw_temperature)
    elif isinstance(raw_temperature, str):
        match = re.search(r"-?\d+(?:\.\d+)?", raw_temperature.replace(",", "."))
        if match:
            temperature = float(match.group(0))

    return HomeAssistantIntentDecision(
        intent=intent,
        service=normalized_service,
        target=normalized_target or None,
        domain_hint=normalized_domain,
        use_last_context=bool(parsed.get("use_last_context")),
        all_matches=bool(parsed.get("all_matches")),
        temperature=temperature,
    )


def _extract_json_object(text: str) -> dict | None:
    candidate = text.strip()
    try:
        parsed = json.loads(candidate)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{.*\}", candidate, flags=re.DOTALL)
    if not match:
        return None

    # Some models wrap the JSON in prose; keep the parser tolerant but only
    # accept the first real object.
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None
