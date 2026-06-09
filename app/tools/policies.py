from __future__ import annotations

from app.core.roles import ROLE_ADMIN, ROLE_DEVICE, ROLE_USER


SUPPORTED_ROLES = {
    ROLE_ADMIN,
    ROLE_USER,
    ROLE_DEVICE,
}


def is_tool_allowed_for_role(*, role: str, allowed_roles: tuple[str, ...]) -> bool:
    normalized_role = (role or "").strip().lower()
    if normalized_role not in SUPPORTED_ROLES:
        return False
    return normalized_role in allowed_roles
