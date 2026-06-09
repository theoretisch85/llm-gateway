from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse

from app.api_errors import error_response
from app.auth import require_bearer_token
from app.config import get_settings
from app.schemas.reviewed_pipeline import ReviewedChatRequest, ReviewedChatResponse
from app.services.llamacpp_client import LlamaCppError, LlamaCppTimeoutError
from app.services.model_profiles import ModelProfileDisabledError, ModelProfileInvalidError
from app.services.reviewed_pipeline import ReviewedPipeline


logger = logging.getLogger(__name__)
router = APIRouter(tags=["pipelines"])
_reviewed_pipeline_lock = asyncio.Lock()


@router.post(
    "/v1/pipelines/reviewed-chat",
    response_model=ReviewedChatResponse,
    response_model_exclude_none=True,
    dependencies=[Depends(require_bearer_token)],
)
async def reviewed_chat(payload: ReviewedChatRequest, request: Request) -> ReviewedChatResponse | JSONResponse:
    settings = get_settings()
    pipeline = ReviewedPipeline(settings)
    request_id = request.state.request_id
    logger.info(
        "incoming reviewed pipeline request worker_model=%s reviewer_model=%s",
        payload.worker_model,
        payload.reviewer_model,
    )

    if _reviewed_pipeline_lock.locked():
        logger.warning("reviewed pipeline busy")
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content={
                "error": "reviewed_pipeline_busy",
                "message": "A reviewed-chat pipeline request is already running. Please retry shortly.",
            },
        )

    await _reviewed_pipeline_lock.acquire()
    try:
        request.state.backend_called = True
        return await pipeline.run(payload)
    except ModelProfileDisabledError as exc:
        logger.warning("reviewed pipeline model profile disabled error=%s", exc)
        return error_response(
            request_id=request_id,
            status_code=status.HTTP_400_BAD_REQUEST,
            message="Requested model profile is disabled.",
            error_type="model_profile_error",
            code="model_profile_disabled",
        )
    except ModelProfileInvalidError as exc:
        logger.error("reviewed pipeline model profile invalid error=%s", exc)
        return error_response(
            request_id=request_id,
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            message="Requested model profile is not configured correctly.",
            error_type="model_profile_error",
            code="model_profile_invalid",
        )
    except LlamaCppTimeoutError:
        logger.warning("reviewed pipeline worker timeout")
        return error_response(
            request_id=request_id,
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            message="Worker llama.cpp request timed out.",
            error_type="gateway_timeout",
            code="worker_timeout",
        )
    except LlamaCppError as exc:
        logger.error("reviewed pipeline worker error=%s", exc.message)
        return error_response(
            request_id=request_id,
            status_code=exc.status_code,
            message=exc.message,
            error_type="upstream_error",
            code=exc.code,
        )
    finally:
        _reviewed_pipeline_lock.release()
