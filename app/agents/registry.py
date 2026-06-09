from __future__ import annotations

from dataclasses import dataclass

from app.agents.schemas import RiskLevel


@dataclass(frozen=True)
class AgentTool:
    name: str
    description: str
    risk_level: RiskLevel
    requires_confirmation: bool


_TOOLS: dict[str, AgentTool] = {
    "home.read_state": AgentTool(
        name="home.read_state",
        description="Read Home Assistant entity state. Arguments: entity_id optional, domain optional, limit optional.",
        risk_level=RiskLevel.LOW,
        requires_confirmation=False,
    ),
    "home.call_service": AgentTool(
        name="home.call_service",
        description="Call an allowed Home Assistant service. Arguments: domain, service, entity_id optional, service_data optional.",
        risk_level=RiskLevel.HIGH,
        requires_confirmation=True,
    ),
    "memory.store_fact": AgentTool(
        name="memory.store_fact",
        description="Store a fact in the existing session memory system. Arguments: fact, session_id optional, source optional.",
        risk_level=RiskLevel.MEDIUM,
        requires_confirmation=False,
    ),
}


def list_tools() -> list[AgentTool]:
    return list(_TOOLS.values())


def get_tool(tool_name: str) -> AgentTool | None:
    normalized = (tool_name or "").strip()
    if not normalized:
        return None
    return _TOOLS.get(normalized)
