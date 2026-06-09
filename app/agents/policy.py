from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.agents.registry import AgentTool, get_tool
from app.agents.schemas import ActionRequest, RiskLevel


@dataclass(frozen=True)
class PolicyDecision:
    status: Literal["allowed", "denied", "requires_confirmation"]
    reason: str
    tool: AgentTool | None = None


class AgentPolicy:
    def evaluate(self, action: ActionRequest, *, allow_actions: bool) -> PolicyDecision:
        tool = get_tool(action.tool_name)
        if tool is None:
            return PolicyDecision(
                status="denied",
                reason=f"Unknown tool: {action.tool_name}",
                tool=None,
            )

        if not allow_actions:
            return PolicyDecision(
                status="denied",
                reason="Actions are disabled for this run.",
                tool=tool,
            )

        if tool.risk_level == RiskLevel.HIGH:
            return PolicyDecision(
                status="requires_confirmation",
                reason=f"Tool '{tool.name}' requires confirmation before execution.",
                tool=tool,
            )

        if tool.requires_confirmation:
            return PolicyDecision(
                status="requires_confirmation",
                reason=f"Tool '{tool.name}' requires confirmation before execution.",
                tool=tool,
            )

        return PolicyDecision(
            status="allowed",
            reason="Action allowed.",
            tool=tool,
        )
