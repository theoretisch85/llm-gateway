import logging

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse, StreamingResponse

from app.api_errors import error_response
from app.auth import require_bearer_token
from app.config import get_settings
from app.context_guard import ContextGuardError, fit_messages_to_budget
from app.schemas.chat import ChatCompletionRequest
from app.services.llamacpp_client import LlamaCppClient, LlamaCppError, LlamaCppTimeoutError


logger = logging.getLogger(__name__)
router = APIRouter(tags=["chat"])


@router.post("/v1/chat/completions", response_model=None, dependencies=[Depends(require_bearer_token)])
async def create_chat_completion(
    payload: ChatCompletionRequest,
    request: Request,
) -> JSONResponse | StreamingResponse:
    settings = get_settings()
    client = LlamaCppClient(settings)
    request_id = request.state.request_id
    logger.info(
        "incoming chat request model=%s stream=%s messages=%s",
        payload.model,
        payload.stream,
        len(payload.messages),
    )

    try:
        guard_result = fit_messages_to_budget(
            messages=payload.messages,
            max_context_tokens=settings.backend_context_window,
            response_reserve_tokens=payload.max_tokens or settings.context_response_reserve,
            chars_per_token=settings.context_chars_per_token,
        )
    except ContextGuardError as exc:
        logger.warning("context guard rejected request error=%s", exc.message)
        return error_response(
            request_id=request_id,
            status_code=status.HTTP_400_BAD_REQUEST,
            message=exc.message,
            error_type="context_length_error",
            code=exc.code,
        )

    if guard_result.truncated:
        logger.warning(
            "context guard compacted request dropped_messages=%s estimated_prompt_tokens=%s",
            guard_result.dropped_messages,
            guard_result.estimated_prompt_tokens,
        )
    else:
        logger.info("context guard estimated_prompt_tokens=%s", guard_result.estimated_prompt_tokens)

    backend_payload = payload.model_dump(exclude_none=True)
    backend_payload["messages"] = guard_result.messages
    target = settings.resolve_target_for_public_model(payload.model)
    backend_payload["model"] = target.backend_name
    if backend_payload.get("max_tokens") is None:
        backend_payload["max_tokens"] = settings.default_max_tokens
    logger.info(
        "backend model mapping public_model=%s backend_model=%s base_url=%s",
        payload.model,
        backend_payload["model"],
        target.base_url,
    )

    try:
        request.state.backend_called = True
        if payload.stream:
            stream = client.stream_chat_completion(
                backend_payload=backend_payload,
                public_model_name=target.public_name,
                backend_model_name=target.backend_name,
                request_id=request_id,
                base_url=target.base_url,
            )
            return StreamingResponse(
                stream,
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                },
            )

        response_payload = await client.create_chat_completion(backend_payload, base_url=target.base_url)
        response_payload["model"] = settings.map_backend_to_public_model(
            response_payload.get("model", backend_payload["model"])
        )
        response_payload["request_id"] = request_id
        logger.info("chat completion succeeded public_model=%s", response_payload["model"])
        return JSONResponse(content=response_payload)
    except LlamaCppTimeoutError:
        logger.warning("request timeout method=%s path=%s", request.method, request.url.path)
        return error_response(
            request_id=request_id,
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            message="Upstream llama.cpp request timed out.",
            error_type="gateway_timeout",
            code="upstream_timeout",
        )
    except LlamaCppError as exc:
        logger.error("request failed method=%s path=%s error=%s", request.method, request.url.path, exc.message)
        return error_response(
            request_id=request_id,
            status_code=exc.status_code,
            message=exc.message,
            error_type="upstream_error",
            code=exc.code,
        )
