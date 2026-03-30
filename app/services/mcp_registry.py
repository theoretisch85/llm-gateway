from __future__ import annotations

from typing import Any

from app.tools.registry import (
    find_registered_tool,
    get_builtin_tool_names,
    list_tool_rows,
)


def get_builtin_mcp_tool_names() -> set[str]:
    return get_builtin_tool_names()


def get_mcp_tools() -> list[dict[str, Any]]:
    return list_tool_rows()


def find_mcp_tool(tool_name: str) -> dict[str, Any] | None:
    tool = find_registered_tool(tool_name)
    if tool is None:
        return None
    return {
        "name": tool.name,
        "description": tool.description,
        "input_schema": tool.input_schema,
        "output_schema": tool.output_schema,
        "handler": tool.handler,
        "requires_admin": tool.requires_admin,
        "allowed_roles": list(tool.allowed_roles),
    }
