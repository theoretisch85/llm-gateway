from __future__ import annotations

from fastapi import APIRouter

from app.routes.admin import router as admin_router
from app.routes.admin_auth import router as admin_auth_router
from app.routes.admin_chat import router as admin_chat_router
from app.routes.chat import router as chat_router
from app.routes.device import router as device_router
from app.routes.health import router as health_router
from app.routes.home_assistant import router as home_assistant_router
from app.routes.internal_health import router as internal_health_router
from app.routes.mcp import router as mcp_router
from app.routes.metrics import router as metrics_router
from app.routes.models import router as models_router


def all_routers() -> list[APIRouter]:
    return [
        health_router,
        admin_auth_router,
        admin_router,
        admin_chat_router,
        device_router,
        home_assistant_router,
        internal_health_router,
        metrics_router,
        mcp_router,
        models_router,
        chat_router,
    ]
