from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


ProfileStatusValue = Literal["online", "offline"]


class GatewayStatus(BaseModel):
    status: Literal["online"]


class ModelProfileStatus(BaseModel):
    profile_id: str
    display_name: str
    model: str | None = None
    configured_model: str
    base_url: str
    enabled: bool
    status: ProfileStatusValue
    latency_ms: float | None = None
    message: str | None = None


class StatusResponse(BaseModel):
    gateway: GatewayStatus
    profiles: list[ModelProfileStatus]
