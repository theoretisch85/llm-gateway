from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import quote

import httpx

from app.config import Settings


logger = logging.getLogger(__name__)


class CalmNewsConfigError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 503, code: str = "calm_news_config_error") -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


class CalmNewsRequestError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int = 502,
        code: str = "calm_news_request_failed",
        upstream_request_id: str | None = None,
        upstream_status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code
        self.upstream_request_id = upstream_request_id
        self.upstream_status_code = upstream_status_code


class CalmNewsClient:
    upstream_name = "calm_news"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def get_latest(self, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return await self._request("GET", "/api/news/latest", params=params)

    async def get_calm(self, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return await self._request("GET", "/api/news/calm", params=params)

    async def get_positive(self, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return await self._request("GET", "/api/news/positive", params=params)

    async def get_relevant(self, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return await self._request("GET", "/api/news/relevant", params=params)

    async def get_article(self, article_id: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        article_key = str(article_id or "").strip()
        if not article_key:
            raise ValueError("id ist erforderlich.")
        return await self._request("GET", f"/api/news/{quote(article_key, safe='')}", params=params)

    async def get_system_status(self) -> dict[str, Any]:
        return await self._request("GET", "/api/system/status")

    async def get_sources(self, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return await self._request("GET", "/api/sources", params=params)

    async def trigger_ingest(self, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return await self._request("POST", "/api/ingest/run", params=params)

    async def get_last_ingest(self, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return await self._request("GET", "/api/ingest/last", params=params)

    async def set_source_status(self, source_id: str, *, payload: dict[str, Any]) -> dict[str, Any]:
        source_key = str(source_id or "").strip()
        if not source_key:
            raise ValueError("id ist erforderlich.")
        if not isinstance(payload, dict) or not payload:
            raise ValueError("payload ist erforderlich.")
        return await self._request(
            "PATCH",
            f"/api/sources/{quote(source_key, safe='')}",
            json_payload=_normalize_source_payload(payload),
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        base_url = self._require_config()
        url = f"{base_url}{path}"
        request_params = _clean_mapping(params)
        request_json = _clean_mapping(json_payload)

        try:
            async with httpx.AsyncClient(timeout=self.settings.calm_news_timeout_seconds) as client:
                response = await client.request(method, url, params=request_params, json=request_json)
        except httpx.TimeoutException as exc:
            logger.warning(
                "calm_news timeout method=%s url=%s base_url=%s timeout=%s",
                method,
                url,
                base_url,
                self.settings.calm_news_timeout_seconds,
            )
            raise CalmNewsRequestError(
                "calm_news request timed out.",
                status_code=504,
                code="calm_news_upstream_timeout",
            ) from exc
        except httpx.HTTPError as exc:
            logger.warning("calm_news request failed method=%s url=%s base_url=%s error=%s", method, url, base_url, exc)
            raise CalmNewsRequestError(
                f"calm_news request failed: {exc}",
                status_code=502,
                code="calm_news_upstream_unreachable",
            ) from exc

        payload = _parse_response_payload(response)
        upstream_request_id = _extract_request_id(response=response, payload=payload)

        if response.status_code >= 400:
            message = _extract_error_message(payload, response)
            logger.warning(
                "calm_news upstream error method=%s url=%s base_url=%s status=%s upstream_request_id=%s",
                method,
                url,
                base_url,
                response.status_code,
                upstream_request_id or "-",
            )
            raise CalmNewsRequestError(
                message,
                status_code=response.status_code,
                code=f"calm_news_upstream_{response.status_code}",
                upstream_request_id=upstream_request_id,
                upstream_status_code=response.status_code,
            )

        result: dict[str, Any] = {
            "success": True,
            "upstream": self.upstream_name,
            "data": payload,
        }
        if upstream_request_id:
            result["request_id"] = upstream_request_id
        return result

    def _require_config(self) -> str:
        if not self.settings.calm_news_enabled:
            raise CalmNewsConfigError(
                "CALM_NEWS_ENABLED ist false. calm_news ist im Gateway deaktiviert.",
                status_code=503,
                code="calm_news_disabled",
            )

        base_url = (self.settings.calm_news_base_url or "").strip().rstrip("/")
        if not base_url:
            raise CalmNewsConfigError(
                "CALM_NEWS_BASE_URL ist nicht gesetzt.",
                status_code=503,
                code="calm_news_base_url_missing",
            )
        return base_url


def _clean_mapping(mapping: dict[str, Any] | None) -> dict[str, Any] | None:
    if not mapping:
        return None
    cleaned = {key: value for key, value in mapping.items() if value is not None and value != ""}
    return cleaned or None


def _parse_response_payload(response: httpx.Response) -> Any:
    if not response.content:
        return {}

    content_type = response.headers.get("content-type", "").lower()
    if "application/json" in content_type:
        try:
            return response.json()
        except (ValueError, json.JSONDecodeError):
            return response.text

    try:
        return response.json()
    except (ValueError, json.JSONDecodeError):
        return response.text


def _extract_request_id(*, response: httpx.Response, payload: Any) -> str | None:
    header_request_id = response.headers.get("X-Request-ID") or response.headers.get("x-request-id")
    if header_request_id:
        return str(header_request_id)

    if isinstance(payload, dict):
        request_id = payload.get("request_id")
        if request_id is not None:
            return str(request_id)
        error = payload.get("error")
        if isinstance(error, dict) and error.get("request_id") is not None:
            return str(error["request_id"])

    return None


def _extract_error_message(payload: Any, response: httpx.Response) -> str:
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
        if payload.get("message"):
            return str(payload["message"])
        if payload.get("detail"):
            return str(payload["detail"])

    text = response.text.strip()
    if text:
        return text
    return f"calm_news returned HTTP {response.status_code}."


def _normalize_source_payload(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(payload)
    enabled = normalized.get("enabled")
    status = normalized.get("status")
    if status in (None, "") and enabled is not None:
        coerced_enabled = _coerce_bool(enabled)
        normalized["enabled"] = coerced_enabled
        normalized["status"] = "active" if coerced_enabled else "disabled"
    return normalized


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off"}:
            return False
    raise ValueError("enabled muss true oder false sein.")
