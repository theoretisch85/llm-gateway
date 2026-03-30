from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.auth import require_admin_api_auth, require_device_token
from app.config import get_settings
from app.core.roles import ActorContext, ROLE_ADMIN, ROLE_DEVICE
from app.orchestrator import ToolOrchestrator
from app.services.home_assistant import (
    HomeAssistantClient,
    HomeAssistantConfigError,
    HomeAssistantRequestError,
)
from app.services.home_assistant_memory import get_home_assistant_note_store
from app.tools.executor import ToolPermissionError


router = APIRouter(tags=["home-assistant"])
tool_orchestrator = ToolOrchestrator()


class HomeAssistantActionRequest(BaseModel):
    domain: str = Field(..., min_length=1)
    service: str = Field(..., min_length=1)
    entity_id: str | None = None
    service_data: dict[str, Any] = Field(default_factory=dict)


class HomeAssistantEntityNoteRequest(BaseModel):
    entity_id: str = Field(..., min_length=3)
    note: str = Field(..., min_length=1)


@router.get("/api/admin/home-assistant/status", dependencies=[Depends(require_admin_api_auth)])
async def admin_home_assistant_status() -> dict[str, object]:
    settings = get_settings()
    client = HomeAssistantClient(settings)
    try:
        return await client.status()
    except HomeAssistantConfigError as exc:
        return {
            "configured": False,
            "base_url": settings.home_assistant_base_url or "",
            "message": str(exc),
            "location_name": None,
            "version": None,
            "allowed_services": settings.parsed_home_assistant_allowed_services,
            "allowed_entity_prefixes": settings.parsed_home_assistant_allowed_entity_prefixes,
        }
    except HomeAssistantRequestError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


@router.get("/api/admin/home-assistant/entities")
async def admin_home_assistant_entities(
    request: Request,
    domain: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    auth_subject: str = Depends(require_admin_api_auth),
) -> dict[str, object]:
    settings = get_settings()
    try:
        entities = await tool_orchestrator.execute_tool(
            settings=settings,
            actor=ActorContext(
                actor_id=auth_subject or "admin",
                role=ROLE_ADMIN,
                source="api.home_assistant.entities",
            ),
            request_id=request.state.request_id,
            tool_name="ha.entities",
            arguments={"domain": domain, "limit": limit},
        )
    except HomeAssistantConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except HomeAssistantRequestError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    except ToolPermissionError as exc:
        raise HTTPException(status_code=403, detail=exc.message) from exc
    return {
        "domain": domain,
        "count": len(entities),
        "entities": entities,
    }


@router.post("/api/admin/home-assistant/action")
async def admin_home_assistant_action(
    payload: HomeAssistantActionRequest,
    request: Request,
    auth_subject: str = Depends(require_admin_api_auth),
) -> dict[str, object]:
    settings = get_settings()
    try:
        result = await tool_orchestrator.execute_tool(
            settings=settings,
            actor=ActorContext(
                actor_id=auth_subject or "admin",
                role=ROLE_ADMIN,
                source="api.home_assistant.action.admin",
            ),
            request_id=request.state.request_id,
            tool_name="ha.call",
            arguments={
                "domain": payload.domain,
                "service": payload.service,
                "entity_id": payload.entity_id,
                "service_data": payload.service_data,
            },
        )
        request.state.backend_called = True
        return result
    except HomeAssistantConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except HomeAssistantRequestError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ToolPermissionError as exc:
        raise HTTPException(status_code=403, detail=exc.message) from exc


@router.post("/api/device/home-assistant/action", dependencies=[Depends(require_device_token)])
async def device_home_assistant_action(payload: HomeAssistantActionRequest, request: Request) -> dict[str, object]:
    settings = get_settings()
    try:
        result = await tool_orchestrator.execute_tool(
            settings=settings,
            actor=ActorContext(
                actor_id="device_token",
                role=ROLE_DEVICE,
                source="api.home_assistant.action.device",
            ),
            request_id=request.state.request_id,
            tool_name="ha.call",
            arguments={
                "domain": payload.domain,
                "service": payload.service,
                "entity_id": payload.entity_id,
                "service_data": payload.service_data,
            },
        )
        request.state.backend_called = True
        return result
    except HomeAssistantConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except HomeAssistantRequestError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ToolPermissionError as exc:
        raise HTTPException(status_code=403, detail=exc.message) from exc


@router.get("/api/admin/home-assistant/notes", dependencies=[Depends(require_admin_api_auth)])
async def admin_home_assistant_notes(limit: int = Query(default=100, ge=1, le=500)) -> dict[str, object]:
    settings = get_settings()
    store = get_home_assistant_note_store(settings)
    if store is None:
        return {"persistent": False, "count": 0, "notes": []}
    notes = await store.list_notes(limit=limit)
    return {
        "persistent": True,
        "count": len(notes),
        "notes": [
            {
                "entity_id": item.entity_id,
                "note": item.note,
                "created_at": item.created_at,
                "updated_at": item.updated_at,
            }
            for item in notes
        ],
    }


@router.post("/api/admin/home-assistant/notes", dependencies=[Depends(require_admin_api_auth)])
async def admin_save_home_assistant_note(payload: HomeAssistantEntityNoteRequest) -> dict[str, object]:
    settings = get_settings()
    store = get_home_assistant_note_store(settings)
    if store is None:
        raise HTTPException(status_code=400, detail="DATABASE_URL ist nicht gesetzt. Home-Assistant-Notizen brauchen PostgreSQL.")
    note = await store.upsert_note(payload.entity_id, payload.note)
    return {
        "ok": True,
        "entity_id": note.entity_id,
        "note": note.note,
        "created_at": note.created_at,
        "updated_at": note.updated_at,
    }
