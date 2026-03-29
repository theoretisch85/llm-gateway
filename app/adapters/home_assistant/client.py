from __future__ import annotations

from collections.abc import Iterable

import httpx

from app.config import Settings


class HomeAssistantConfigError(RuntimeError):
    pass


class HomeAssistantRequestError(RuntimeError):
    def __init__(self, message: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class HomeAssistantClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _require_config(self) -> tuple[str, str]:
        base_url = (self.settings.home_assistant_base_url or "").rstrip("/")
        token = self.settings.home_assistant_token or ""
        if not base_url:
            raise HomeAssistantConfigError("HOME_ASSISTANT_BASE_URL ist nicht gesetzt.")
        if not token:
            raise HomeAssistantConfigError("HOME_ASSISTANT_TOKEN ist nicht gesetzt.")
        lowered_token = token.strip().lower()
        if lowered_token.startswith("http://") or lowered_token.startswith("https://"):
            raise HomeAssistantConfigError(
                "HOME_ASSISTANT_TOKEN sieht wie eine URL aus. Bitte hier einen echten Long-Lived Access Token eintragen, nicht die Home-Assistant-URL."
            )
        return base_url, token

    async def _request(self, method: str, path: str, *, json_payload: dict | None = None) -> dict | list:
        base_url, token = self._require_config()
        url = f"{base_url}{path}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=self.settings.home_assistant_timeout_seconds) as client:
                response = await client.request(method, url, headers=headers, json=json_payload)
        except httpx.TimeoutException as exc:
            raise HomeAssistantRequestError("Home Assistant request timed out.", status_code=504) from exc
        except httpx.HTTPError as exc:
            raise HomeAssistantRequestError(f"Home Assistant request failed: {exc}", status_code=502) from exc

        if response.status_code >= 400:
            message = response.text.strip() or f"Home Assistant error {response.status_code}"
            raise HomeAssistantRequestError(message, status_code=response.status_code)

        if not response.content:
            return {}
        return response.json()

    async def status(self) -> dict[str, object]:
        info = await self._request("GET", "/api/")
        return {
            "configured": True,
            "base_url": (self.settings.home_assistant_base_url or "").rstrip("/"),
            "message": info.get("message") if isinstance(info, dict) else "ok",
            "location_name": info.get("location_name") if isinstance(info, dict) else None,
            "version": info.get("version") if isinstance(info, dict) else None,
            "allowed_services": self.settings.parsed_home_assistant_allowed_services,
            "allowed_entity_prefixes": self.settings.parsed_home_assistant_allowed_entity_prefixes,
        }

    async def list_entities(self, domain: str | None = None, limit: int = 100) -> list[dict]:
        payload = await self._request("GET", "/api/states")
        if not isinstance(payload, list):
            return []
        normalized_domain = domain.strip().lower() if domain else None
        allowed_prefixes = self.settings.parsed_home_assistant_allowed_entity_prefixes
        entities: list[dict] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            entity_id = str(item.get("entity_id") or "")
            if not entity_id:
                continue
            if normalized_domain and not entity_id.startswith(f"{normalized_domain}."):
                continue
            if allowed_prefixes and not self._entity_allowed(entity_id):
                continue
            entities.append(
                {
                    "entity_id": entity_id,
                    "state": item.get("state"),
                    "friendly_name": (item.get("attributes") or {}).get("friendly_name"),
                }
            )
            if len(entities) >= limit:
                break
        return entities

    def validate_action(self, domain: str, service: str, entity_ids: Iterable[str] | None = None) -> None:
        full_service = f"{domain.strip().lower()}.{service.strip().lower()}"
        allowed_services = self.settings.parsed_home_assistant_allowed_services
        if allowed_services and full_service not in allowed_services:
            raise HomeAssistantConfigError(f"Home Assistant service {full_service} ist nicht freigegeben.")

        if entity_ids:
            for entity_id in entity_ids:
                if not self._entity_allowed(entity_id):
                    raise HomeAssistantConfigError(f"Entity {entity_id} ist nicht freigegeben.")

    async def call_service(
        self,
        *,
        domain: str,
        service: str,
        entity_id: str | None = None,
        service_data: dict | None = None,
    ) -> dict[str, object]:
        payload = dict(service_data or {})
        entity_ids = _extract_entity_ids(entity_id, payload)
        self.validate_action(domain, service, entity_ids)
        if entity_id and "entity_id" not in payload:
            payload["entity_id"] = entity_id
        result = await self._request("POST", f"/api/services/{domain}/{service}", json_payload=payload)
        return {
            "ok": True,
            "domain": domain,
            "service": service,
            "entity_ids": entity_ids,
            "result": result,
        }

    def _entity_allowed(self, entity_id: str) -> bool:
        prefixes = self.settings.parsed_home_assistant_allowed_entity_prefixes
        if not prefixes:
            return True
        lowered = entity_id.strip().lower()
        return any(lowered.startswith(prefix) for prefix in prefixes)


def _extract_entity_ids(entity_id: str | None, payload: dict) -> list[str]:
    result: list[str] = []
    if entity_id:
        result.append(entity_id)

    raw = payload.get("entity_id")
    if isinstance(raw, str):
        result.extend(item.strip() for item in raw.split(",") if item.strip())
    elif isinstance(raw, list):
        result.extend(str(item).strip() for item in raw if str(item).strip())

    deduped: list[str] = []
    seen: set[str] = set()
    for item in result:
        if item not in seen:
            deduped.append(item)
            seen.add(item)
    return deduped
