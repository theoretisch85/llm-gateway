from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class MCPTool(BaseModel):
    name: str
    description: str
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)


class MCPToolsResponse(BaseModel):
    tools: list[MCPTool]


class MCPCallRequest(BaseModel):
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class MCPCallResponse(BaseModel):
    tool: str
    ok: bool
    result: Any | None = None
    error: str | None = None
