from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.api_errors import error_response
from app.auth import require_device_token
from app.config import get_settings
from app.context_guard import ContextGuardError
from app.routes.admin_chat import _extract_assistant_text, _prepare_admin_backend_payload
from app.services.llamacpp_client import LlamaCppClient, LlamaCppError, LlamaCppTimeoutError
from app.services.session_memory import get_session_store


router = APIRouter(tags=["device"])


class DeviceAskRequest(BaseModel):
    message: str = Field(..., min_length=1)
    mode: str = "auto"
    session_id: str | None = None
    max_tokens: int | None = None
    document_ids: list[str] = []


@router.post("/api/device/ask", dependencies=[Depends(require_device_token)])
async def device_ask(payload: DeviceAskRequest, request: Request) -> JSONResponse:
    settings = get_settings()
    store = get_session_store(settings)
    client = LlamaCppClient(settings)

    if payload.session_id:
        session = await store.get_session(payload.session_id)
    else:
        session = await store.create_session(title="Pi Session", mode=payload.mode)

    if session is None:
        return error_response(
            request_id=request.state.request_id,
            status_code=status.HTTP_404_NOT_FOUND,
            message="Session not found.",
            error_type="not_found_error",
            code="session_not_found",
        )

    try:
        decision, backend_payload = await _prepare_admin_backend_payload(
            settings,
            session,
            type("Payload", (), {"message": payload.message, "mode": payload.mode, "temperature": None, "max_tokens": payload.max_tokens, "document_ids": payload.document_ids})(),
        )
        await store.add_message(session.id, "user", payload.message)
        await store.update_route(session.id, decision.resolved_model, decision.reason, payload.mode or session.mode)
        request.state.backend_called = True
        response_payload = await client.create_chat_completion(backend_payload, base_url=decision.target_base_url)
        assistant_text = _extract_assistant_text(response_payload)
        await store.add_message(session.id, "assistant", assistant_text, model_used=decision.resolved_model)
        return JSONResponse(
            {
                "session_id": session.id,
                "text": assistant_text,
                "resolved_model": decision.resolved_model,
                "route_reason": decision.reason,
            }
        )
    except ContextGuardError as exc:
        return error_response(
            request_id=request.state.request_id,
            status_code=status.HTTP_400_BAD_REQUEST,
            message=exc.message,
            error_type="context_length_error",
            code=exc.code,
        )
    except LlamaCppTimeoutError:
        return error_response(
            request_id=request.state.request_id,
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            message="Upstream llama.cpp request timed out.",
            error_type="gateway_timeout",
            code="upstream_timeout",
        )
    except LlamaCppError as exc:
        return error_response(
            request_id=request.state.request_id,
            status_code=exc.status_code,
            message=exc.message,
            error_type="upstream_error",
            code=exc.code,
        )
