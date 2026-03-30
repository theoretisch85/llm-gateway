from __future__ import annotations

from dataclasses import dataclass


ROLE_ADMIN = "admin"
ROLE_DEVICE = "device"
ROLE_SERVICE = "service"


@dataclass(frozen=True)
class ActorContext:
    actor_id: str
    role: str
    source: str


def normalize_mcp_role(auth_subject: str) -> str:
    # `require_mcp_auth` returns "device" for device tokens, all other
    # successful paths are admin-capable principals.
    return ROLE_DEVICE if auth_subject == ROLE_DEVICE else ROLE_ADMIN
