from __future__ import annotations

from dataclasses import dataclass

from app.core.roles import ActorContext


@dataclass(frozen=True)
class RequestContext:
    request_id: str
    role: str
    principal_id: str
    source: str

    @classmethod
    def from_actor(cls, *, request_id: str, actor: ActorContext) -> "RequestContext":
        return cls(
            request_id=request_id,
            role=actor.role,
            principal_id=actor.actor_id,
            source=actor.source,
        )

