from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse

from app.agents.runtime import AgentRuntime
from app.agents.schemas import AgentInput, AgentRun
from app.api_errors import error_response
from app.auth import require_mcp_auth
from app.config import get_settings
from app.core.request_context import RequestContext
from app.core.roles import normalize_mcp_role
from app.services.home_assistant import HomeAssistantConfigError, HomeAssistantRequestError
from app.services.llamacpp_client import LlamaCppError, LlamaCppTimeoutError


logger = logging.getLogger(__name__)
router = APIRouter(tags=["agents"])


@router.post("/agent/run", response_model=AgentRun)
async def run_agent(
    payload: AgentInput,
    request: Request,
    auth_subject: str = Depends(require_mcp_auth),
) -> AgentRun | JSONResponse:
    settings = get_settings()
    role = normalize_mcp_role(auth_subject)
    runtime = AgentRuntime(
        request_context=RequestContext(
            request_id=request.state.request_id,
            role=role,
            principal_id=auth_subject or role,
            source="api.agent.run",
        ),
        settings=settings,
    )
    logger.info(
        "agent run request allow_actions=%s model_preference=%s role=%s",
        payload.allow_actions,
        payload.model_preference,
        role,
    )

    try:
        request.state.backend_called = True
        return await runtime.run(payload)
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
            code=exc.code or "agent_llm_error",
        )
    except HomeAssistantConfigError as exc:
        return error_response(
            request_id=request.state.request_id,
            status_code=status.HTTP_400_BAD_REQUEST,
            message=str(exc),
            error_type="invalid_request_error",
            code="home_assistant_config_error",
        )
    except HomeAssistantRequestError as exc:
        return error_response(
            request_id=request.state.request_id,
            status_code=exc.status_code,
            message=exc.message,
            error_type="upstream_error",
            code="home_assistant_request_failed",
        )
    except ValueError as exc:
        return error_response(
            request_id=request.state.request_id,
            status_code=status.HTTP_400_BAD_REQUEST,
            message=str(exc),
            error_type="invalid_request_error",
            code="agent_invalid_request",
        )
