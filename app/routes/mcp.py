from __future__ import annotations

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse

from app.api_errors import error_response
from app.auth import require_mcp_auth
from app.config import get_settings
from app.core.roles import ActorContext, normalize_mcp_role
from app.orchestrator import ToolOrchestrator
from app.schemas.mcp import MCPCallRequest, MCPCallResponse, MCPToolsResponse, MCPTool
from app.services.home_assistant import HomeAssistantConfigError, HomeAssistantRequestError
from app.tools.executor import ToolNotFoundError, ToolPermissionError


router = APIRouter(tags=["mcp"])
tool_orchestrator = ToolOrchestrator()


@router.get("/api/mcp/tools", response_model=MCPToolsResponse)
async def list_mcp_tools(auth_subject: str = Depends(require_mcp_auth)) -> MCPToolsResponse:
    role = normalize_mcp_role(auth_subject)
    visible_tools = tool_orchestrator.list_tools_for_role(role)
    tools = [
        MCPTool(
            name=item["name"],
            description=item["description"],
            input_schema=item["input_schema"],
            output_schema=item["output_schema"],
        )
        for item in visible_tools
    ]
    return MCPToolsResponse(tools=tools)


@router.post("/api/mcp/call", response_model=None)
async def call_mcp_tool(
    payload: MCPCallRequest,
    request: Request,
    auth_subject: str = Depends(require_mcp_auth),
) -> MCPCallResponse | JSONResponse:
    role = normalize_mcp_role(auth_subject)
    actor = ActorContext(
        actor_id=auth_subject or "unknown",
        role=role,
        source="api.mcp",
    )
    settings = get_settings()
    try:
        result = await tool_orchestrator.execute_tool(
            settings=settings,
            actor=actor,
            request_id=request.state.request_id,
            tool_name=payload.tool,
            arguments=payload.arguments,
        )
    except ToolNotFoundError:
        return error_response(
            request_id=request.state.request_id,
            status_code=status.HTTP_404_NOT_FOUND,
            message=f"Unbekanntes MCP-Tool: {payload.tool}",
            error_type="invalid_request_error",
            code="mcp_tool_not_found",
        )
    except ToolPermissionError:
        return error_response(
            request_id=request.state.request_id,
            status_code=status.HTTP_403_FORBIDDEN,
            message=f"MCP-Tool '{payload.tool}' ist nur fuer Admin/API-Token erlaubt.",
            error_type="permission_error",
            code="mcp_forbidden_for_device",
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
            code="mcp_invalid_arguments",
        )

    return MCPCallResponse(tool=payload.tool, ok=True, result=result)
