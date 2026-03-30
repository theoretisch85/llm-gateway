from __future__ import annotations

from typing import Any

from app.config import Settings
from app.core.request_context import RequestContext
from app.core.roles import ActorContext
from app.tools.executor import ToolExecutionContext, ToolExecutor
from app.tools.registry import list_tool_rows


class ToolOrchestrator:
    def __init__(self) -> None:
        self._executor = ToolExecutor()

    def list_tools_for_role(self, role: str) -> list[dict[str, Any]]:
        return list_tool_rows(role=role)

    async def execute_tool(
        self,
        *,
        settings: Settings,
        context: RequestContext | None = None,
        actor: ActorContext | None = None,
        request_id: str | None = None,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> Any:
        resolved_context = context
        if resolved_context is None:
            if actor is None or not request_id:
                raise ValueError("context oder actor+request_id ist erforderlich.")
            resolved_context = RequestContext.from_actor(request_id=request_id, actor=actor)

        return await self._executor.execute(
            settings=settings,
            tool_name=tool_name,
            arguments=arguments,
            context=ToolExecutionContext(
                request_id=resolved_context.request_id,
                role=resolved_context.role,
                principal_id=resolved_context.principal_id,
                source=resolved_context.source,
            ),
        )
