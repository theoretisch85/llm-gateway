from __future__ import annotations

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse

from app.api_errors import error_response
from app.auth import require_mcp_auth
from app.config import get_settings
from app.schemas.mcp import MCPCallRequest, MCPCallResponse, MCPToolsResponse, MCPTool
from app.services.home_assistant import HomeAssistantConfigError, HomeAssistantRequestError
from app.services.mcp_registry import find_mcp_tool, get_mcp_tools


router = APIRouter(tags=["mcp"])


@router.get("/api/mcp/tools", dependencies=[Depends(require_mcp_auth)], response_model=MCPToolsResponse)
async def list_mcp_tools() -> MCPToolsResponse:
    tools = [
        MCPTool(
            name=item["name"],
            description=item["description"],
            input_schema=item["input_schema"],
            output_schema=item["output_schema"],
        )
        for item in get_mcp_tools()
    ]
    return MCPToolsResponse(tools=tools)


@router.post("/api/mcp/call", dependencies=[Depends(require_mcp_auth)], response_model=None)
async def call_mcp_tool(payload: MCPCallRequest, request: Request) -> MCPCallResponse | JSONResponse:
    tool = find_mcp_tool(payload.tool)
    if not tool:
        return error_response(
            request_id=request.state.request_id,
            status_code=status.HTTP_404_NOT_FOUND,
            message=f"Unbekanntes MCP-Tool: {payload.tool}",
            error_type="invalid_request_error",
            code="mcp_tool_not_found",
        )

    settings = get_settings()
    try:
        result = await tool["handler"](settings, payload.arguments)
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
