from __future__ import annotations

from typing import Any

from app.config import Settings
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
        actor: ActorContext,
        request_id: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> Any:
        return await self._executor.execute(
            settings=settings,
            tool_name=tool_name,
            arguments=arguments,
            context=ToolExecutionContext(
                request_id=request_id,
                actor_id=actor.actor_id,
                actor_role=actor.role,
                source=actor.source,
            ),
        )
