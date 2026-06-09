from __future__ import annotations

from fastapi import APIRouter, Depends

from app.auth import require_bearer_token
from app.config import get_settings
from app.schemas.status import StatusResponse
from app.services.profile_status import ProfileStatusChecker


router = APIRouter(tags=["status"])


@router.get("/v1/status", response_model=StatusResponse, response_model_exclude_none=True, dependencies=[Depends(require_bearer_token)])
async def status() -> StatusResponse:
    settings = get_settings()
    checker = ProfileStatusChecker(settings)
    return await checker.check_all()
