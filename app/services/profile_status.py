from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from app.config import Settings
from app.schemas.status import GatewayStatus, ModelProfileStatus, StatusResponse
from app.services.model_profiles import ModelProfile, list_model_profiles


STATUS_TIMEOUT_SECONDS = 3.0


class ProfileStatusChecker:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def check_all(self) -> StatusResponse:
        profiles = list_model_profiles(self._settings)
        results = await asyncio.gather(*(self._check_profile(profile) for profile in profiles))
        return StatusResponse(gateway=GatewayStatus(status="online"), profiles=list(results))

    async def _check_profile(self, profile: ModelProfile) -> ModelProfileStatus:
        if not profile.base_url:
            return self._offline(profile, message="base_url is missing.")

        health_url = f"{profile.base_url.rstrip('/')}/health"
        models_url = f"{profile.base_url.rstrip('/')}/v1/models"
        timeout = httpx.Timeout(STATUS_TIMEOUT_SECONDS)

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                started_at = time.perf_counter()
                health_response = await client.get(health_url)
                latency_ms = round((time.perf_counter() - started_at) * 1000, 2)
                if health_response.status_code >= 400:
                    return self._offline(
                        profile,
                        latency_ms=latency_ms,
                        message=f"health returned HTTP {health_response.status_code}",
                    )

                model_name = await self._fetch_backend_model(client, models_url)
                return ModelProfileStatus(
                    profile_id=profile.profile_id,
                    display_name=profile.display_name,
                    model=model_name or profile.public_model,
                    configured_model=profile.public_model,
                    base_url=profile.base_url,
                    enabled=profile.enabled,
                    status="online",
                    latency_ms=latency_ms,
                )
        except httpx.TimeoutException:
            return self._offline(profile, message="backend status check timed out.")
        except httpx.HTTPError as exc:
            return self._offline(profile, message=f"backend status check failed: {exc.__class__.__name__}")

    async def _fetch_backend_model(self, client: httpx.AsyncClient, models_url: str) -> str | None:
        try:
            response = await client.get(models_url)
        except (httpx.TimeoutException, httpx.HTTPError):
            return None
        if response.status_code >= 400:
            return None

        try:
            payload = response.json()
        except ValueError:
            return None
        return self._extract_model_name(payload)

    def _extract_model_name(self, payload: dict[str, Any]) -> str | None:
        data = payload.get("data")
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and isinstance(item.get("id"), str):
                    return item["id"]

        models = payload.get("models")
        if isinstance(models, list):
            for item in models:
                if not isinstance(item, dict):
                    continue
                if isinstance(item.get("model"), str):
                    return item["model"]
                if isinstance(item.get("name"), str):
                    return item["name"]

        return None

    def _offline(
        self,
        profile: ModelProfile,
        *,
        latency_ms: float | None = None,
        message: str | None = None,
    ) -> ModelProfileStatus:
        return ModelProfileStatus(
            profile_id=profile.profile_id,
            display_name=profile.display_name,
            model=None,
            configured_model=profile.public_model,
            base_url=profile.base_url,
            enabled=profile.enabled,
            status="offline",
            latency_ms=latency_ms,
            message=message,
        )
