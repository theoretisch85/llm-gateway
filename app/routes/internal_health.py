import logging

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse

from app.auth import require_bearer_token
from app.config import get_settings
from app.services.llamacpp_client import LlamaCppClient, LlamaCppError, LlamaCppTimeoutError


logger = logging.getLogger(__name__)
router = APIRouter(tags=["internal-health"])


@router.get("/internal/health", dependencies=[Depends(require_bearer_token)])
async def internal_health(request: Request) -> JSONResponse:
    settings = get_settings()
    client = LlamaCppClient(settings)
    request_id = request.state.request_id

    payload = {
        "status": "ok",
        "gateway": {"status": "ok"},
        "backend": {
            "status": "ok",
            "base_url": settings.llamacpp_base_url,
            "model": settings.backend_model_name,
            "latency_ms": None,
        },
    }

    try:
        request.state.backend_called = True
        models_response, latency_ms = await client.fetch_models()
        payload["backend"]["latency_ms"] = latency_ms

        model_available = _backend_model_available(models_response, settings.backend_model_name)
        if not model_available:
            payload["status"] = "error"
            payload["backend"]["status"] = "error"
            payload["backend"]["message"] = "Configured backend model is not available."
            logger.warning(
                "internal health failed request_id=%s reason=backend_model_missing model=%s",
                request_id,
                settings.backend_model_name,
            )
            return JSONResponse(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, content=payload)

        return JSONResponse(status_code=status.HTTP_200_OK, content=payload)
    except LlamaCppTimeoutError:
        payload["status"] = "error"
        payload["backend"]["status"] = "error"
        payload["backend"]["message"] = "Backend readiness check timed out."
        logger.warning("internal health timeout request_id=%s base_url=%s", request_id, settings.llamacpp_base_url)
        return JSONResponse(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, content=payload)
    except LlamaCppError as exc:
        payload["status"] = "error"
        payload["backend"]["status"] = "error"
        payload["backend"]["message"] = exc.message
        logger.warning(
            "internal health backend error request_id=%s base_url=%s error=%s",
            request_id,
            settings.llamacpp_base_url,
            exc.message,
        )
        return JSONResponse(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, content=payload)


def _backend_model_available(models_response: dict, expected_model: str) -> bool:
    data = models_response.get("data")
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and item.get("id") == expected_model:
                return True

    models = models_response.get("models")
    if isinstance(models, list):
        for item in models:
            if isinstance(item, dict) and item.get("model") == expected_model:
                return True
            if isinstance(item, dict) and item.get("name") == expected_model:
                return True

    return False
