from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from app.audit.tool_audit import audit_tool_execution
from app.config import Settings
from app.tools.registry import find_registered_tool, is_role_allowed


class ToolExecutionError(RuntimeError):
    def __init__(self, message: str, *, code: str, status_code: int) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code


class ToolNotFoundError(ToolExecutionError):
    def __init__(self, tool_name: str) -> None:
        super().__init__(
            f"Unbekanntes Tool: {tool_name}",
            code="tool_not_found",
            status_code=404,
        )


class ToolPermissionError(ToolExecutionError):
    def __init__(self, tool_name: str, role: str) -> None:
        super().__init__(
            f"Tool '{tool_name}' ist fuer Rolle '{role}' nicht erlaubt.",
            code="tool_forbidden",
            status_code=403,
        )


@dataclass(frozen=True)
class ToolExecutionContext:
    request_id: str
    actor_id: str
    actor_role: str
    source: str


class ToolExecutor:
    async def execute(
        self,
        *,
        settings: Settings,
        tool_name: str,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> Any:
        tool = find_registered_tool(tool_name)
        if tool is None:
            raise ToolNotFoundError(tool_name)

        if not is_role_allowed(tool, context.actor_role):
            raise ToolPermissionError(tool_name, context.actor_role)

        started_at = time.perf_counter()
        try:
            result = await tool.handler(settings, arguments)
        except Exception as exc:
            audit_tool_execution(
                request_id=context.request_id,
                actor_id=context.actor_id,
                actor_role=context.actor_role,
                source=context.source,
                tool_name=tool.name,
                arguments=arguments,
                duration_ms=(time.perf_counter() - started_at) * 1000,
                ok=False,
                error=exc,
            )
            raise

        audit_tool_execution(
            request_id=context.request_id,
            actor_id=context.actor_id,
            actor_role=context.actor_role,
            source=context.source,
            tool_name=tool.name,
            arguments=arguments,
            duration_ms=(time.perf_counter() - started_at) * 1000,
            ok=True,
            result=result,
        )
        return result
