from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class DecisionType(str, Enum):
    RESPOND = "respond"
    ACTION = "action"
    CLARIFY = "clarify"
    DENY = "deny"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AgentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(..., min_length=1)
    model_preference: Literal["auto", "fast", "deep"] = "auto"
    allow_actions: bool = False
    session_id: str | None = None


class ActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_name: str = Field(..., min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)
    risk: RiskLevel = RiskLevel.LOW


class AgentDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: DecisionType
    reply: str = ""
    action: ActionRequest | None = None

    @model_validator(mode="after")
    def validate_action_shape(self) -> "AgentDecision":
        if self.decision == DecisionType.ACTION and self.action is None:
            raise ValueError("action is required when decision=action")
        if self.decision != DecisionType.ACTION and self.action is not None:
            raise ValueError("action must be null unless decision=action")
        return self


class ActionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_name: str
    status: Literal["executed", "denied", "requires_confirmation", "failed"]
    ok: bool
    executed: bool = False
    risk: RiskLevel
    requires_confirmation: bool = False
    result: Any | None = None
    error: str | None = None


class AgentRun(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    created_at: datetime = Field(default_factory=_utcnow)
    resolved_model: str
    route_reason: str
    decision: AgentDecision
    action_result: ActionResult | None = None
    final_reply: str
