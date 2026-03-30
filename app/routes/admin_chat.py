import json
import logging
import re
from textwrap import dedent

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse

from app.api_errors import error_response
from app.auth import get_admin_session_username, require_admin_api_auth
from app.config import get_settings
from app.context_guard import ContextGuardError, fit_messages_to_budget
from app.core.roles import ActorContext, ROLE_ADMIN
from app.orchestrator import ToolOrchestrator
from app.schemas.chat import ChatMessage
from app.schemas.admin_chat import (
    AdminChatRequest,
    AdminChatResponse,
    AdminMemoryOverviewResponse,
    AdminMemorySummaryResponse,
    AdminSessionCreateRequest,
    AdminSessionRenameRequest,
    AdminSessionResponse,
)
from app.services.llamacpp_client import LlamaCppClient, LlamaCppError, LlamaCppTimeoutError
from app.services.home_assistant import HomeAssistantClient, HomeAssistantConfigError, HomeAssistantRequestError
from app.services.home_assistant_intent import classify_home_assistant_intent
from app.services.home_assistant_memory import (
    get_home_assistant_alias_store,
    get_home_assistant_note_store,
    normalize_home_assistant_alias,
    parse_home_assistant_alias_instruction,
    parse_home_assistant_note_instruction,
)
from app.services.model_router import ModelRouter
from app.services.session_memory import ChatSession, get_session_store
from app.services.storage_library import get_document_contexts
from app.tools.executor import ToolExecutionError


logger = logging.getLogger(__name__)
router = APIRouter(tags=["admin-chat"])
tool_orchestrator = ToolOrchestrator()


@router.get("/internal/chat", response_class=HTMLResponse, response_model=None)
async def admin_chat_page(request: Request) -> HTMLResponse | RedirectResponse:
    if not get_admin_session_username(request):
        return RedirectResponse(url="/admin/login?next=/internal/admin%3Ftab%3Dchat", status_code=303)
    return HTMLResponse(
        _admin_chat_html(),
        headers={"Cache-Control": "no-store, max-age=0"},
    )


@router.get("/api/admin/sessions", dependencies=[Depends(require_admin_api_auth)])
async def list_sessions() -> list[AdminSessionResponse]:
    settings = get_settings()
    store = get_session_store(settings)
    return [_serialize_session(item) for item in await store.list_sessions()]


@router.get("/api/admin/memory/overview", dependencies=[Depends(require_admin_api_auth)], response_model=AdminMemoryOverviewResponse)
async def get_memory_overview(limit_sessions: int = 12, limit_summaries: int = 12, session_id: str | None = None) -> AdminMemoryOverviewResponse:
    settings = get_settings()
    store = get_session_store(settings)
    stats = await store.get_memory_stats()
    sessions = [_serialize_session(item) for item in await store.list_sessions(limit=max(1, min(limit_sessions, 30)))]
    summaries = [
        _serialize_memory_summary(item)
        for item in await store.list_memory_summaries(
            session_id=session_id,
            limit=max(1, min(limit_summaries, 40)),
        )
    ]
    return AdminMemoryOverviewResponse(
        store_mode=str(stats.get("store_mode") or "memory"),
        persistent=bool(stats.get("persistent")),
        sessions_count=int(stats.get("sessions_count") or 0),
        messages_count=int(stats.get("messages_count") or 0),
        summaries_count=int(stats.get("summaries_count") or 0),
        sessions=sessions,
        summaries=summaries,
    )


@router.post("/api/admin/sessions", dependencies=[Depends(require_admin_api_auth)], response_model=AdminSessionResponse)
async def create_session(payload: AdminSessionCreateRequest) -> AdminSessionResponse:
    settings = get_settings()
    store = get_session_store(settings)
    session = await store.create_session(title=payload.title, mode=payload.mode)
    return _serialize_session(session)


@router.get("/api/admin/sessions/{session_id}", dependencies=[Depends(require_admin_api_auth)], response_model=AdminSessionResponse)
async def get_session(session_id: str) -> AdminSessionResponse:
    settings = get_settings()
    session = await _require_session(get_session_store(settings), session_id)
    return _serialize_session(session)


@router.delete("/api/admin/sessions/{session_id}", dependencies=[Depends(require_admin_api_auth)])
async def delete_session(session_id: str) -> dict[str, bool]:
    settings = get_settings()
    store = get_session_store(settings)
    deleted = await store.delete_session(session_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Session not found.")
    return {"deleted": True}


@router.post("/api/admin/sessions/{session_id}/reset", dependencies=[Depends(require_admin_api_auth)], response_model=AdminSessionResponse)
async def reset_session(session_id: str) -> AdminSessionResponse:
    settings = get_settings()
    store = get_session_store(settings)
    session = await store.reset_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found.")
    return _serialize_session(session)


@router.post("/api/admin/sessions/{session_id}/rename", dependencies=[Depends(require_admin_api_auth)], response_model=AdminSessionResponse)
async def rename_session(session_id: str, payload: AdminSessionRenameRequest) -> AdminSessionResponse:
    settings = get_settings()
    store = get_session_store(settings)
    clean_title = payload.title.strip()
    if not clean_title:
        raise HTTPException(status_code=400, detail="Titel darf nicht leer sein.")
    session = await store.rename_session(session_id, clean_title)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found.")
    return _serialize_session(session)


@router.post(
    "/api/admin/sessions/{session_id}/chat",
    response_model=None,
)
async def admin_chat(
    payload: AdminChatRequest,
    session_id: str,
    request: Request,
    auth_subject: str = Depends(require_admin_api_auth),
) -> AdminChatResponse | JSONResponse:
    settings = get_settings()
    store = get_session_store(settings)
    session = await _require_session(store, session_id)
    client = LlamaCppClient(settings)

    try:
        alias_instruction = parse_home_assistant_alias_instruction(payload.message)
        if alias_instruction:
            alias, entity_ids = alias_instruction
            alias_store = get_home_assistant_alias_store(settings)
            if alias_store is None:
                return error_response(
                    request_id=request.state.request_id,
                    status_code=status.HTTP_400_BAD_REQUEST,
                    message="DATABASE_URL ist nicht gesetzt. Home-Assistant-Aliase brauchen PostgreSQL.",
                    error_type="invalid_request_error",
                    code="home_assistant_alias_store_unavailable",
                )
            domains = {item.split(".", 1)[0].strip().lower() for item in entity_ids if "." in item}
            if len(domains) != 1:
                return error_response(
                    request_id=request.state.request_id,
                    status_code=status.HTTP_400_BAD_REQUEST,
                    message="Ein HA-Alias darf aktuell nur Entities aus genau einer Domain enthalten.",
                    error_type="invalid_request_error",
                    code="home_assistant_alias_domain_mismatch",
                )
            await store.add_message(session_id, "user", payload.message)
            saved_alias = await alias_store.upsert_alias(
                alias=alias,
                domain=next(iter(domains)),
                entity_ids=entity_ids,
                learned_from=payload.message,
            )
            assistant_message = await store.add_message(
                session_id,
                "assistant",
                f"Gemerkt: Alias '{saved_alias.alias}' -> {', '.join(saved_alias.entity_ids)}",
                model_used="system",
            )
            return AdminChatResponse(
                session=_serialize_session(await _require_session(store, session_id)),
                assistant_message=_serialize_message(assistant_message),
                resolved_model=session.resolved_model or "system",
                route_reason="home_assistant_alias_saved",
            )

        note_instruction = parse_home_assistant_note_instruction(payload.message)
        if note_instruction:
            entity_id, note = note_instruction
            note_store = get_home_assistant_note_store(settings)
            if note_store is None:
                return error_response(
                    request_id=request.state.request_id,
                    status_code=status.HTTP_400_BAD_REQUEST,
                    message="DATABASE_URL ist nicht gesetzt. Home-Assistant-Notizen brauchen PostgreSQL.",
                    error_type="invalid_request_error",
                    code="home_assistant_note_store_unavailable",
                )
            await store.add_message(session_id, "user", payload.message)
            saved_note = await note_store.upsert_note(entity_id, note)
            assistant_message = await store.add_message(
                session_id,
                "assistant",
                f"Gemerkt: {saved_note.entity_id} -> {saved_note.note}",
                model_used="system",
            )
            return AdminChatResponse(
                session=_serialize_session(await _require_session(store, session_id)),
                assistant_message=_serialize_message(assistant_message),
                resolved_model=session.resolved_model or "system",
                route_reason="home_assistant_note_saved",
            )

        ha_intent_result = await _try_handle_home_assistant_intent_stage(settings, payload.message, session=session)
        if ha_intent_result is not None:
            await store.add_message(session_id, "user", payload.message)
            await store.update_route(session_id, "home_assistant", ha_intent_result["route_reason"], payload.mode or session.mode)
            if ha_intent_result.get("backend_called"):
                request.state.backend_called = True
            assistant_message = await store.add_message(
                session_id,
                "assistant",
                ha_intent_result["assistant_text"],
                model_used="home_assistant",
            )
            return AdminChatResponse(
                session=_serialize_session(await _require_session(store, session_id)),
                assistant_message=_serialize_message(assistant_message),
                resolved_model="home_assistant",
                route_reason=ha_intent_result["route_reason"],
            )

        ha_action_result = await _try_handle_home_assistant_action(settings, payload.message, session=session)
        if ha_action_result is not None:
            await store.add_message(session_id, "user", payload.message)
            await store.update_route(session_id, "home_assistant", ha_action_result["route_reason"], payload.mode or session.mode)
            request.state.backend_called = True
            assistant_message = await store.add_message(
                session_id,
                "assistant",
                ha_action_result["assistant_text"],
                model_used="home_assistant",
            )
            return AdminChatResponse(
                session=_serialize_session(await _require_session(store, session_id)),
                assistant_message=_serialize_message(assistant_message),
                resolved_model="home_assistant",
                route_reason=ha_action_result["route_reason"],
            )

        ha_query_result = await _try_handle_home_assistant_lookup(settings, payload.message)
        if ha_query_result is not None:
            await store.add_message(session_id, "user", payload.message)
            await store.update_route(session_id, "home_assistant", ha_query_result["route_reason"], payload.mode or session.mode)
            if ha_query_result.get("backend_called"):
                request.state.backend_called = True
            assistant_message = await store.add_message(
                session_id,
                "assistant",
                ha_query_result["assistant_text"],
                model_used="home_assistant",
            )
            return AdminChatResponse(
                session=_serialize_session(await _require_session(store, session_id)),
                assistant_message=_serialize_message(assistant_message),
                resolved_model="home_assistant",
                route_reason=ha_query_result["route_reason"],
            )

        gateway_ops_result = await _try_handle_gateway_ops_action(
            settings=settings,
            message=payload.message,
            request_id=request.state.request_id,
            actor_id=auth_subject or "admin",
        )
        if gateway_ops_result is not None:
            await store.add_message(session_id, "user", payload.message)
            await store.update_route(session_id, "gateway_ops", gateway_ops_result["route_reason"], payload.mode or session.mode)
            assistant_message = await store.add_message(
                session_id,
                "assistant",
                gateway_ops_result["assistant_text"],
                model_used="gateway_ops",
            )
            return AdminChatResponse(
                session=_serialize_session(await _require_session(store, session_id)),
                assistant_message=_serialize_message(assistant_message),
                resolved_model="gateway_ops",
                route_reason=gateway_ops_result["route_reason"],
            )

        decision, backend_payload = await _prepare_admin_backend_payload(settings, session, payload)
        await store.add_message(session_id, "user", payload.message)
        await store.update_route(session_id, decision.resolved_model, decision.reason, payload.mode or session.mode)
        request.state.backend_called = True
        response_payload = await client.create_chat_completion(backend_payload, base_url=decision.target_base_url)
        assistant_text = _extract_assistant_text(response_payload)
        response_metrics = _extract_response_metrics(response_payload)
        assistant_message = await store.add_message(
            session_id,
            "assistant",
            assistant_text,
            model_used=decision.resolved_model,
            prompt_tokens=response_metrics["prompt_tokens"],
            completion_tokens=response_metrics["completion_tokens"],
            total_tokens=response_metrics["total_tokens"],
            tokens_per_second=response_metrics["tokens_per_second"],
        )
        return AdminChatResponse(
            session=_serialize_session(await _require_session(store, session_id)),
            assistant_message=_serialize_message(assistant_message),
            resolved_model=decision.resolved_model,
            route_reason=decision.reason,
        )
    except ContextGuardError as exc:
        return error_response(
            request_id=request.state.request_id,
            status_code=status.HTTP_400_BAD_REQUEST,
            message=exc.message,
            error_type="context_length_error",
            code=exc.code,
        )
    except HomeAssistantConfigError as exc:
        return error_response(
            request_id=request.state.request_id,
            status_code=status.HTTP_400_BAD_REQUEST,
            message=str(exc),
            error_type="invalid_request_error",
            code=_home_assistant_error_code(exc),
        )
    except HomeAssistantRequestError as exc:
        return error_response(
            request_id=request.state.request_id,
            status_code=exc.status_code,
            message=exc.message,
            error_type="upstream_error",
            code="home_assistant_request_failed",
        )
    except (ToolExecutionError, ValueError) as exc:
        return error_response(
            request_id=request.state.request_id,
            status_code=status.HTTP_400_BAD_REQUEST,
            message=str(exc),
            error_type="invalid_request_error",
            code="gateway_ops_failed",
        )
    except LlamaCppTimeoutError:
        return error_response(
            request_id=request.state.request_id,
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            message="Upstream llama.cpp request timed out.",
            error_type="gateway_timeout",
            code="upstream_timeout",
        )
    except LlamaCppError as exc:
        return error_response(
            request_id=request.state.request_id,
            status_code=exc.status_code,
            message=exc.message,
            error_type="upstream_error",
            code=exc.code,
        )


@router.post(
    "/api/admin/sessions/{session_id}/chat/stream",
    response_model=None,
)
async def admin_chat_stream(
    payload: AdminChatRequest,
    session_id: str,
    request: Request,
    auth_subject: str = Depends(require_admin_api_auth),
) -> StreamingResponse | JSONResponse:
    settings = get_settings()
    store = get_session_store(settings)
    session = await _require_session(store, session_id)
    client = LlamaCppClient(settings)

    try:
        alias_instruction = parse_home_assistant_alias_instruction(payload.message)
        if alias_instruction:
            alias, entity_ids = alias_instruction
            alias_store = get_home_assistant_alias_store(settings)
            if alias_store is None:
                return error_response(
                    request_id=request.state.request_id,
                    status_code=status.HTTP_400_BAD_REQUEST,
                    message="DATABASE_URL ist nicht gesetzt. Home-Assistant-Aliase brauchen PostgreSQL.",
                    error_type="invalid_request_error",
                    code="home_assistant_alias_store_unavailable",
                )
            domains = {item.split(".", 1)[0].strip().lower() for item in entity_ids if "." in item}
            if len(domains) != 1:
                return error_response(
                    request_id=request.state.request_id,
                    status_code=status.HTTP_400_BAD_REQUEST,
                    message="Ein HA-Alias darf aktuell nur Entities aus genau einer Domain enthalten.",
                    error_type="invalid_request_error",
                    code="home_assistant_alias_domain_mismatch",
                )
            await store.add_message(session_id, "user", payload.message)
            await alias_store.upsert_alias(
                alias=alias,
                domain=next(iter(domains)),
                entity_ids=entity_ids,
                learned_from=payload.message,
            )

            async def alias_stream():
                confirmation = {
                    "id": f"chatcmpl-ha-alias-{session_id}",
                    "object": "chat.completion.chunk",
                    "created": 0,
                    "model": "system",
                    "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
                    "request_id": request.state.request_id,
                }
                content = {
                    **confirmation,
                    "choices": [{"index": 0, "delta": {"content": f"Gemerkt: Alias '{normalize_home_assistant_alias(alias)}' -> {', '.join(entity_ids)}"}, "finish_reason": "stop"}],
                }
                yield f"data: {json.dumps(confirmation, ensure_ascii=False)}\n\n".encode("utf-8")
                yield f"data: {json.dumps(content, ensure_ascii=False)}\n\n".encode("utf-8")
                yield b"data: [DONE]\n\n"
                await store.add_message(
                    session_id,
                    "assistant",
                    f"Gemerkt: Alias '{normalize_home_assistant_alias(alias)}' -> {', '.join(entity_ids)}",
                    model_used="system",
                )

            return StreamingResponse(
                alias_stream(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
            )

        note_instruction = parse_home_assistant_note_instruction(payload.message)
        if note_instruction:
            entity_id, note = note_instruction
            note_store = get_home_assistant_note_store(settings)
            if note_store is None:
                return error_response(
                    request_id=request.state.request_id,
                    status_code=status.HTTP_400_BAD_REQUEST,
                    message="DATABASE_URL ist nicht gesetzt. Home-Assistant-Notizen brauchen PostgreSQL.",
                    error_type="invalid_request_error",
                    code="home_assistant_note_store_unavailable",
                )
            await store.add_message(session_id, "user", payload.message)
            await note_store.upsert_note(entity_id, note)
            confirmation = {
                "id": f"chatcmpl-ha-note-{session_id}",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": "system",
                "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
                "request_id": request.state.request_id,
            }
            content = {
                **confirmation,
                "choices": [{"index": 0, "delta": {"content": f"Gemerkt: {entity_id} -> {note}"}, "finish_reason": "stop"}],
            }

            async def note_stream():
                yield f"data: {json.dumps(confirmation, ensure_ascii=False)}\n\n".encode("utf-8")
                yield f"data: {json.dumps(content, ensure_ascii=False)}\n\n".encode("utf-8")
                yield b"data: [DONE]\n\n"
                await store.add_message(
                    session_id,
                    "assistant",
                    f"Gemerkt: {entity_id} -> {note}",
                    model_used="system",
                )

            return StreamingResponse(
                note_stream(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
            )

        ha_intent_result = await _try_handle_home_assistant_intent_stage(settings, payload.message, session=session)
        if ha_intent_result is not None:
            await store.add_message(session_id, "user", payload.message)
            await store.update_route(session_id, "home_assistant", ha_intent_result["route_reason"], payload.mode or session.mode)
            if ha_intent_result.get("backend_called"):
                request.state.backend_called = True

            async def intent_stream():
                confirmation = {
                    "id": f"chatcmpl-ha-intent-{session_id}",
                    "object": "chat.completion.chunk",
                    "created": 0,
                    "model": "home_assistant",
                    "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
                    "request_id": request.state.request_id,
                }
                content = {
                    **confirmation,
                    "choices": [{"index": 0, "delta": {"content": ha_intent_result["assistant_text"]}, "finish_reason": "stop"}],
                }
                yield f"data: {json.dumps(confirmation, ensure_ascii=False)}\n\n".encode("utf-8")
                yield f"data: {json.dumps(content, ensure_ascii=False)}\n\n".encode("utf-8")
                yield b"data: [DONE]\n\n"
                await store.add_message(
                    session_id,
                    "assistant",
                    ha_intent_result["assistant_text"],
                    model_used="home_assistant",
                )

            return StreamingResponse(
                intent_stream(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
            )

        ha_action_result = await _try_handle_home_assistant_action(settings, payload.message, session=session)
        if ha_action_result is not None:
            await store.add_message(session_id, "user", payload.message)
            await store.update_route(session_id, "home_assistant", ha_action_result["route_reason"], payload.mode or session.mode)
            request.state.backend_called = True

            async def action_stream():
                confirmation = {
                    "id": f"chatcmpl-ha-action-{session_id}",
                    "object": "chat.completion.chunk",
                    "created": 0,
                    "model": "home_assistant",
                    "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
                    "request_id": request.state.request_id,
                }
                content = {
                    **confirmation,
                    "choices": [{"index": 0, "delta": {"content": ha_action_result["assistant_text"]}, "finish_reason": "stop"}],
                }
                yield f"data: {json.dumps(confirmation, ensure_ascii=False)}\n\n".encode("utf-8")
                yield f"data: {json.dumps(content, ensure_ascii=False)}\n\n".encode("utf-8")
                yield b"data: [DONE]\n\n"
                await store.add_message(
                    session_id,
                    "assistant",
                    ha_action_result["assistant_text"],
                    model_used="home_assistant",
                )

            return StreamingResponse(
                action_stream(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
            )

        ha_query_result = await _try_handle_home_assistant_lookup(settings, payload.message)
        if ha_query_result is not None:
            await store.add_message(session_id, "user", payload.message)
            await store.update_route(session_id, "home_assistant", ha_query_result["route_reason"], payload.mode or session.mode)
            if ha_query_result.get("backend_called"):
                request.state.backend_called = True

            async def query_stream():
                confirmation = {
                    "id": f"chatcmpl-ha-query-{session_id}",
                    "object": "chat.completion.chunk",
                    "created": 0,
                    "model": "home_assistant",
                    "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
                    "request_id": request.state.request_id,
                }
                content = {
                    **confirmation,
                    "choices": [{"index": 0, "delta": {"content": ha_query_result["assistant_text"]}, "finish_reason": "stop"}],
                }
                yield f"data: {json.dumps(confirmation, ensure_ascii=False)}\n\n".encode("utf-8")
                yield f"data: {json.dumps(content, ensure_ascii=False)}\n\n".encode("utf-8")
                yield b"data: [DONE]\n\n"
                await store.add_message(
                    session_id,
                    "assistant",
                    ha_query_result["assistant_text"],
                    model_used="home_assistant",
                )

            return StreamingResponse(
                query_stream(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
            )

        gateway_ops_result = await _try_handle_gateway_ops_action(
            settings=settings,
            message=payload.message,
            request_id=request.state.request_id,
            actor_id=auth_subject or "admin",
        )
        if gateway_ops_result is not None:
            await store.add_message(session_id, "user", payload.message)
            await store.update_route(session_id, "gateway_ops", gateway_ops_result["route_reason"], payload.mode or session.mode)

            async def gateway_ops_stream():
                confirmation = {
                    "id": f"chatcmpl-gateway-ops-{session_id}",
                    "object": "chat.completion.chunk",
                    "created": 0,
                    "model": "gateway_ops",
                    "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
                    "request_id": request.state.request_id,
                }
                content = {
                    **confirmation,
                    "choices": [{"index": 0, "delta": {"content": gateway_ops_result["assistant_text"]}, "finish_reason": "stop"}],
                }
                yield f"data: {json.dumps(confirmation, ensure_ascii=False)}\n\n".encode("utf-8")
                yield f"data: {json.dumps(content, ensure_ascii=False)}\n\n".encode("utf-8")
                yield b"data: [DONE]\n\n"
                await store.add_message(
                    session_id,
                    "assistant",
                    gateway_ops_result["assistant_text"],
                    model_used="gateway_ops",
                )

            return StreamingResponse(
                gateway_ops_stream(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
            )

        decision, backend_payload = await _prepare_admin_backend_payload(settings, session, payload)
        backend_payload["stream"] = True
    except ContextGuardError as exc:
        return error_response(
            request_id=request.state.request_id,
            status_code=status.HTTP_400_BAD_REQUEST,
            message=exc.message,
            error_type="context_length_error",
            code=exc.code,
        )
    except HomeAssistantConfigError as exc:
        return error_response(
            request_id=request.state.request_id,
            status_code=status.HTTP_400_BAD_REQUEST,
            message=str(exc),
            error_type="invalid_request_error",
            code=_home_assistant_error_code(exc),
        )
    except HomeAssistantRequestError as exc:
        return error_response(
            request_id=request.state.request_id,
            status_code=exc.status_code,
            message=exc.message,
            error_type="upstream_error",
            code="home_assistant_request_failed",
        )
    except (ToolExecutionError, ValueError) as exc:
        return error_response(
            request_id=request.state.request_id,
            status_code=status.HTTP_400_BAD_REQUEST,
            message=str(exc),
            error_type="invalid_request_error",
            code="gateway_ops_failed",
        )

    await store.add_message(session_id, "user", payload.message)
    await store.update_route(session_id, decision.resolved_model, decision.reason, payload.mode or session.mode)
    request.state.backend_called = True

    async def event_stream():
        chunks: list[str] = []
        response_metrics: dict[str, int | float | None] = {
            "prompt_tokens": None,
            "completion_tokens": None,
            "total_tokens": None,
            "tokens_per_second": None,
        }
        try:
            async for chunk in client.stream_chat_completion(
                backend_payload=backend_payload,
                public_model_name=decision.resolved_model,
                backend_model_name=settings.resolve_target_for_public_model(decision.resolved_model).backend_name,
                request_id=request.state.request_id,
                base_url=decision.target_base_url,
            ):
                text_part = _extract_content_from_sse(chunk)
                if text_part:
                    chunks.append(text_part)
                stream_metrics = _extract_metrics_from_sse(chunk)
                for key, value in stream_metrics.items():
                    if value is not None:
                        response_metrics[key] = value
                yield chunk
        except (LlamaCppError, LlamaCppTimeoutError) as exc:
            payload = {
                "error": {
                    "message": getattr(exc, "message", "Streaming request failed."),
                    "type": "upstream_error",
                    "code": getattr(exc, "code", "upstream_error"),
                    "request_id": request.state.request_id,
                }
            }
            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")
            yield b"data: [DONE]\n\n"
        finally:
            final_text = "".join(chunks).strip()
            if final_text:
                await store.add_message(
                    session_id,
                    "assistant",
                    final_text,
                    model_used=decision.resolved_model,
                    prompt_tokens=response_metrics["prompt_tokens"],
                    completion_tokens=response_metrics["completion_tokens"],
                    total_tokens=response_metrics["total_tokens"],
                    tokens_per_second=response_metrics["tokens_per_second"],
                )

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


async def _prepare_admin_backend_payload(settings, session: ChatSession, payload: AdminChatRequest):
    model_router = ModelRouter(settings)
    decision = model_router.decide(payload.mode or session.mode, payload.message, len(session.messages))
    history_messages = [
        {"role": item.role, "content": item.content}
        for item in session.messages[-12:]
    ]
    prompt_messages: list[ChatMessage] = []

    context_blocks = [
        "Du bist der eingebaute Admin-Assistent dieser llm-gateway Plattform. Antworte praezise, technisch und umsetzungsorientiert.",
    ]
    if payload.document_ids:
        context_blocks.extend(await _load_document_context_blocks(settings, payload.document_ids))
    if payload.include_home_assistant or _message_wants_home_assistant_context(payload.message):
        context_blocks.extend(await _load_home_assistant_context_blocks(settings, payload.message))
    if session.summary:
        context_blocks.append(f"Bisherige Session-Zusammenfassung:\n{session.summary}")
    prompt_messages.extend(ChatMessage(role=item["role"], content=item["content"]) for item in history_messages)
    user_message = payload.message.strip()
    if context_blocks:
        user_message = (
            "Arbeitskontext fuer diese Antwort:\n\n"
            + "\n\n".join(context_blocks)
            + "\n\nAktuelle Anfrage:\n"
            + user_message
        )
    prompt_messages.append(ChatMessage(role="user", content=user_message))

    guard_result = fit_messages_to_budget(
        messages=prompt_messages,
        max_context_tokens=settings.backend_context_window,
        response_reserve_tokens=payload.max_tokens or settings.context_response_reserve,
        chars_per_token=settings.context_chars_per_token,
    )
    target = settings.resolve_target_for_public_model(decision.resolved_model)
    backend_payload = {
        "model": target.backend_name,
        "messages": guard_result.messages,
        "stream": False,
        "temperature": payload.temperature,
        "max_tokens": payload.max_tokens or settings.default_max_tokens,
    }
    return decision, backend_payload


async def _load_document_context_blocks(settings, document_ids: list[str]) -> list[str]:
    contexts = await get_document_contexts(settings, document_ids)
    blocks: list[str] = []
    for item in contexts:
        content = (item.get("extracted_text") or item.get("text_excerpt") or "").strip()
        if not content:
            continue
        if len(content) > 5000:
            content = content[:5000] + "..."
        title = item.get("title") or item.get("file_name") or "Dokument"
        asset_kind = str(item.get("asset_kind") or "document")
        label = "Bild-Kontext" if asset_kind == "image" else "Dokument-Kontext"
        blocks.append(f"{label}: {title}\n{content}")
    return blocks


def _message_wants_home_assistant_context(message: str) -> bool:
    lowered = (message or "").lower()
    if "home assistant" in lowered or "entity" in lowered:
        return True
    ha_keywords = ("licht", "lampe", "schalter", "fenster", "window", "klima", "thermostat", "temperatur")
    if any(keyword in lowered for keyword in ha_keywords):
        return True
    return bool(re.search(r"\b(?:light|switch|climate|script)\.[a-z0-9_]+\b", lowered))


_GATEWAY_INSTALL_COMMANDS = {
    "git": "install_git",
    "curl": "install_curl",
    "gh": "install_gh",
    "github cli": "install_gh",
    "ripgrep": "install_ripgrep",
    "rg": "install_ripgrep",
    "htop": "install_htop",
    "tmux": "install_tmux",
}


def _normalize_gateway_ops_text(message: str) -> str:
    normalized = (message or "").strip().lower()
    normalized = normalized.replace("github-cli", "github cli")
    normalized = re.sub(r"[^a-z0-9äöüß.\- ]+", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized


def _parse_gateway_ops_action(message: str) -> dict[str, str] | None:
    # Keep gateway host access explicit and narrow: only a handful of
    # allowlisted maintenance tasks may be triggered from chat.
    normalized = _normalize_gateway_ops_text(message)
    if not normalized:
        return None

    if normalized.startswith("wie ") or "wie installiere ich" in normalized:
        return None

    request_like = any(
        phrase in normalized
        for phrase in (
            "installiere",
            "installier",
            " apt install",
            "apt-get install",
            "kannst du",
            "kannstd",
            "mach ",
            "mache ",
            "bitte",
            "zeige ",
            "liste ",
            "list ",
            "aktualisiere",
            "update",
        )
    )
    if not request_like:
        return None

    if "apt update" in normalized or "apt-get update" in normalized or "paketlisten" in normalized or "paketliste" in normalized:
        return {"command_name": "apt_update", "label": "apt update"}

    if ("skills" in normalized or "skill" in normalized) and any(term in normalized for term in ("zeige", "liste", "list", "welche")):
        return {"command_name": "skills", "label": "skills anzeigen"}

    if any(term in normalized for term in ("tools", "werkzeuge")) and any(term in normalized for term in ("zeige", "liste", "list", "welche")):
        return {"command_name": "tools", "label": "tools anzeigen"}

    install_requested = any(term in normalized for term in ("installiere", "installier", " apt install", "apt-get install"))
    if not install_requested:
        return None

    for alias, command_name in _GATEWAY_INSTALL_COMMANDS.items():
        if alias in normalized:
            return {"command_name": command_name, "label": f"{alias} installieren"}
    return None


async def _try_handle_gateway_ops_action(
    *,
    settings,
    message: str,
    request_id: str,
    actor_id: str,
) -> dict[str, str] | None:
    parsed = _parse_gateway_ops_action(message)
    if parsed is None:
        return None

    result = await tool_orchestrator.execute_tool(
        settings=settings,
        actor=ActorContext(
            actor_id=actor_id or "admin",
            role=ROLE_ADMIN,
            source="api.admin_chat.gateway_ops",
        ),
        request_id=request_id,
        tool_name="gateway.ops",
        arguments={
            "target": "gateway",
            "command": parsed["command_name"],
        },
    )
    assistant_text = f"Gateway-Ops ausgefuehrt: {parsed['label']}.\n\n{result.get('output') or 'OK'}"
    return {
        "assistant_text": assistant_text,
        "route_reason": "gateway_ops_action",
    }


def _classify_home_assistant_intent(message: str) -> str:
    text = (message or "").strip()
    if not text:
        return "none"
    if parse_home_assistant_note_instruction(text):
        return "note"
    if _parse_home_assistant_retry_action(text) is not None:
        return "action"
    if _parse_home_assistant_action(text) is not None:
        return "action"
    lowered = text.lower()
    question_markers = ("warum", "wieso", "weshalb", "welche", "welcher", "welches", "wie", "status", "ist", "sind")
    if any(marker in lowered for marker in question_markers):
        return "query"
    return "none"


def _message_might_need_home_assistant_engine(message: str, session: ChatSession | None = None) -> bool:
    # Keep obviously relevant messages on the HA path even if the user switches
    # to follow-up language and stops naming the target entity explicitly.
    if _message_wants_home_assistant_context(message):
        return True

    # Follow-up phrases often stop naming the entity directly. We still want
    # them to go through the HA intent stage if there was a recent HA action.
    lowered = (message or "").lower()
    if any(term in lowered for term in ("auf", "zu", "oeffne", "öffne", "schliesse", "schließe", "nochmal", "erneut", "wieder")):
        return True

    if session is None:
        return False
    if _extract_last_home_assistant_action_context(session) is None:
        return False
    return any(term in lowered for term in ("es", "sie", "das", "den", "doch", "bitte", "mach", "mache", "schalte"))


async def _load_home_assistant_context_blocks(settings, message: str) -> list[str]:
    client = HomeAssistantClient(settings)
    entities = await client.list_entities(limit=120)
    if not entities:
        return ["Home Assistant ist erreichbar, liefert aber aktuell keine sichtbaren Entities."]

    explicit_ids = set(re.findall(r"\b(?:light|switch|climate|script)\.[a-z0-9_]+\b", (message or "").lower()))
    note_store = get_home_assistant_note_store(settings)
    notes = await note_store.list_notes(limit=200) if note_store is not None else []
    notes_by_id = {item.entity_id: item.note for item in notes}
    alias_store = get_home_assistant_alias_store(settings)
    aliases = await alias_store.list_aliases(limit=50) if alias_store is not None else []

    selected: list[dict] = []
    if explicit_ids:
        selected = [item for item in entities if str(item.get("entity_id") or "").lower() in explicit_ids]
    else:
        keywords = set(_home_assistant_search_tokens((message or "").lower()))
        for item in entities:
            entity_id = str(item.get("entity_id") or "").lower()
            friendly_name = str(item.get("friendly_name") or "").lower()
            note = str(notes_by_id.get(entity_id) or "").lower()
            haystack = f"{entity_id} {friendly_name} {note}"
            if keywords and any(keyword in haystack for keyword in keywords):
                selected.append(item)
        if not selected:
            noted_ids = set(notes_by_id)
            selected = [item for item in entities if str(item.get("entity_id") or "").lower() in noted_ids][:20]
        if not selected:
            selected = entities[:20]

    entity_lines: list[str] = []
    for item in selected[:30]:
        entity_id = str(item.get("entity_id") or "")
        state = str(item.get("state") or "-")
        friendly_name = str(item.get("friendly_name") or "-")
        note = notes_by_id.get(entity_id.lower())
        line = f"- {entity_id} | state={state} | name={friendly_name}"
        if note:
            line += f" | note={note}"
        entity_lines.append(line)

    blocks = [
        "Home-Assistant-Entity-Kontext:\n" + "\n".join(entity_lines),
    ]
    if notes:
        note_lines = [f"- {item.entity_id}: {item.note}" for item in notes[:20]]
        blocks.append("Gemerkte Home-Assistant-Bedeutungen:\n" + "\n".join(note_lines))
    if aliases:
        alias_lines = [f"- {item.alias} -> {', '.join(item.entity_ids)}" for item in aliases[:20]]
        blocks.append("Gelernte Home-Assistant-Aliase:\n" + "\n".join(alias_lines))
    blocks.append(
        "Wenn der Nutzer eine Entity-Bedeutung dauerhaft speichern will, soll er schreiben: "
        "'Merke HA <entity_id>: <Beschreibung>'."
    )
    blocks.append(
        "Wenn der Nutzer dem System einen Ausdruck beibringen will, soll er schreiben: "
        "'Merke HA Alias <ausdruck>: <entity_id>' oder "
        "'Wenn ich <ausdruck> sage, meine ich <entity_id>'."
    )
    return blocks


async def _try_handle_home_assistant_action(settings, message: str, session: ChatSession | None = None) -> dict[str, str] | None:
    follow_up = _parse_home_assistant_follow_up_action(message)
    if follow_up is not None and session is not None:
        result = await _run_home_assistant_session_follow_up(
            settings=settings,
            session=session,
            service=str(follow_up["service"]),
            parsed_domain="light",
            service_data={},
            route_reason="home_assistant_follow_up_action",
            label="Home Assistant Follow-up ausgefuehrt",
        )
        if result is not None:
            return result

    retry_action = _parse_home_assistant_retry_action(message)
    if retry_action is not None and session is not None:
        # Retry phrases like "versuch es nochmal" should re-run the last
        # successful HA action instead of falling back to the LLM.
        last_context = _extract_last_home_assistant_action_context(session)
        if last_context is not None:
            result = await _run_home_assistant_session_follow_up(
                settings=settings,
                session=session,
                service=str(last_context["service"]),
                parsed_domain=str(last_context["domain"]),
                service_data={},
                route_reason="home_assistant_retry_action",
                label="Home Assistant erneut ausgefuehrt",
            )
            if result is not None:
                return result

    intent = _classify_home_assistant_intent(message)
    if intent != "action":
        return None

    parsed = _parse_home_assistant_action(message)
    if parsed is None:
        return None

    return await _execute_home_assistant_parsed_action(
        settings=settings,
        parsed=parsed,
        message=message,
        session=session,
        route_reason="home_assistant_action",
    )


async def _try_handle_home_assistant_intent_stage(settings, message: str, session: ChatSession | None = None) -> dict[str, str] | None:
    if not _message_might_need_home_assistant_engine(message, session):
        return None

    # The intent stage gives the model one structured chance to decide whether
    # the user wants normal chat, an HA lookup or an HA action.
    alias_store = get_home_assistant_alias_store(settings)
    alias_lines: list[str] = []
    if alias_store is not None:
        aliases = await alias_store.list_aliases(limit=12)
        alias_lines = [f"{item.alias} -> {', '.join(item.entity_ids)}" for item in aliases]

    last_context = _extract_last_home_assistant_action_context(session)
    last_action_summary = ""
    if last_context is not None:
        last_action_summary = (
            f"{last_context['domain']}.{last_context['service']} -> {', '.join(last_context['entity_ids'])}"
        )

    decision = await classify_home_assistant_intent(
        settings,
        message=message,
        last_action_summary=last_action_summary,
        alias_lines=alias_lines,
    )
    if decision is None or decision.intent == "chat":
        return None

    if decision.intent == "ha_query":
        lookup_text = decision.target or message
        result = await _try_handle_home_assistant_lookup(settings, lookup_text)
        if result is not None:
            result["backend_called"] = True
        return result

    if decision.intent != "ha_action" or not decision.service:
        return None

    if decision.use_last_context and session is not None:
        result = await _run_home_assistant_session_follow_up(
            settings=settings,
            session=session,
            service=decision.service,
            parsed_domain=decision.domain_hint or "light",
            service_data={"temperature": decision.temperature} if decision.temperature is not None else {},
            route_reason="home_assistant_intent_action",
            label="Home Assistant intent-basiert ausgefuehrt",
        )
        if result is not None:
            result["backend_called"] = True
            return result

    parsed = {
        "domain": decision.domain_hint or _guess_home_assistant_domain_from_target(decision.target or message),
        "service": decision.service,
        "target": _normalize_home_assistant_action_target(decision.target or message),
        "service_data": {"temperature": decision.temperature} if decision.temperature is not None else {},
        "all_matches": bool(decision.all_matches),
    }
    result = await _execute_home_assistant_parsed_action(
        settings=settings,
        parsed=parsed,
        message=message,
        session=session,
        route_reason="home_assistant_intent_action",
    )
    if result is not None:
        result["backend_called"] = True
    return result


async def _execute_home_assistant_parsed_action(
    *,
    settings,
    parsed: dict[str, object],
    message: str,
    session: ChatSession | None,
    route_reason: str,
) -> dict[str, str] | None:
    # Both the intent stage and the old heuristic parser end up here so alias
    # learning, validation and execution stay identical.

    client = HomeAssistantClient(settings)
    service = str(parsed["service"])
    service_data = dict(parsed["service_data"])
    parsed_domain = str(parsed["domain"])
    normalized_target = normalize_home_assistant_alias(str(parsed["target"]))
    alias_store = get_home_assistant_alias_store(settings)

    if session is not None and _looks_like_home_assistant_reference_target(str(parsed["target"])):
        result = await _run_home_assistant_session_follow_up(
            settings=settings,
            session=session,
            service=service,
            parsed_domain=parsed_domain,
            service_data=service_data,
            route_reason="home_assistant_context_action",
            label="Home Assistant kontextbezogen ausgefuehrt",
        )
        if result is not None:
            return result

    if alias_store is not None and normalized_target:
        learned_alias = await alias_store.find_alias(normalized_target, parsed_domain)
        if learned_alias is not None and learned_alias.entity_ids and (
            not bool(parsed.get("all_matches")) or len(learned_alias.entity_ids) > 1
        ):
            payload = dict(service_data)
            payload["entity_id"] = learned_alias.entity_ids
            await client.call_service(domain=learned_alias.domain, service=service, entity_id=None, service_data=payload)
            assistant_text = (
                f"Home Assistant ausgefuehrt ueber gelernten Alias '{learned_alias.alias}': "
                f"{learned_alias.domain}.{service} fuer {len(learned_alias.entity_ids)} Entities -> "
                + ", ".join(learned_alias.entity_ids)
                + "."
            )
            return {
                "assistant_text": assistant_text,
                "route_reason": route_reason,
            }

    multi_targets = _expand_home_assistant_target_parts(str(parsed["target"]), parsed_domain)
    if len(multi_targets) > 1:
        resolved_items: list[dict[str, str]] = []
        for target_part in multi_targets:
            resolved = await _resolve_home_assistant_entity(settings, client, target_part)
            if resolved is None:
                raise HomeAssistantConfigError(
                    f"Keine passende Home-Assistant-Entity fuer '{target_part}' gefunden. Nutze am besten die genaue Entity-ID oder speichere zuerst eine Notiz mit 'Merke HA <entity_id>: ...'."
                )
            resolved_items.append(resolved)

        domains = {str(item["entity_id"]).split(".", 1)[0].strip().lower() for item in resolved_items}
        if len(domains) != 1:
            raise HomeAssistantConfigError(
                f"Die Zielauswahl fuer '{parsed['target']}' mischt mehrere Entity-Typen: {', '.join(sorted(domains))}. "
                "Bitte den Zieltyp klarer nennen, z. B. nur Licht oder nur Schalter."
            )

        resolved_domain = next(iter(domains))
        domain = _resolve_effective_home_assistant_domain(
            parsed_domain=parsed_domain,
            resolved_domain=resolved_domain,
            service=service,
        )
        entity_ids = _dedupe_entity_ids([str(item["entity_id"]) for item in resolved_items])
        payload = dict(service_data)
        payload["entity_id"] = entity_ids
        await client.call_service(domain=domain, service=service, entity_id=None, service_data=payload)
        await _maybe_learn_home_assistant_alias(
            settings,
            alias=normalized_target,
            domain=domain,
            entity_ids=entity_ids,
            learned_from=message,
        )

        detail = f"{domain}.{service}"
        if service_data:
            detail += f" {json.dumps(service_data, ensure_ascii=False)}"
        assistant_text = (
            f"Home Assistant ausgefuehrt: {detail} fuer {len(entity_ids)} Entities -> "
            + ", ".join(entity_ids)
            + "."
        )
        return {
            "assistant_text": assistant_text,
            "route_reason": route_reason,
        }

    if bool(parsed.get("all_matches")):
        resolved_entities = await _resolve_home_assistant_entities(
            settings,
            client,
            parsed["target"],
            preferred_domain=parsed_domain,
        )
        if not resolved_entities:
            raise HomeAssistantConfigError(
                f"Keine passenden Home-Assistant-Entities fuer '{parsed['target']}' gefunden. Nutze am besten die genaue Entity-ID oder speichere zuerst eine Notiz mit 'Merke HA <entity_id>: ...'."
            )

        domains = {str(item["entity_id"]).split(".", 1)[0].strip().lower() for item in resolved_entities}
        if len(domains) != 1:
            raise HomeAssistantConfigError(
                f"Die Mehrfachauswahl fuer '{parsed['target']}' ist nicht eindeutig genug: {', '.join(sorted(domains))}. "
                "Bitte den Zieltyp klarer nennen, z. B. Licht, Schalter oder die genaue Entity-ID."
            )

        resolved_domain = next(iter(domains))
        domain = _resolve_effective_home_assistant_domain(
            parsed_domain=parsed_domain,
            resolved_domain=resolved_domain,
            service=service,
        )
        entity_ids = [str(item["entity_id"]) for item in resolved_entities]
        payload = dict(service_data)
        payload["entity_id"] = entity_ids
        await client.call_service(domain=domain, service=service, entity_id=None, service_data=payload)
        await _maybe_learn_home_assistant_alias(
            settings,
            alias=normalized_target,
            domain=domain,
            entity_ids=entity_ids,
            learned_from=message,
            allow_group_alias=False,
        )

        detail = f"{domain}.{service}"
        if service_data:
            detail += f" {json.dumps(service_data, ensure_ascii=False)}"
        assistant_text = (
            f"Home Assistant ausgefuehrt: {detail} fuer {len(entity_ids)} Entities -> "
            + ", ".join(entity_ids)
            + "."
        )
    else:
        resolved = await _resolve_home_assistant_entity(settings, client, parsed["target"])
        if resolved is None:
            raise HomeAssistantConfigError(
                f"Keine passende Home-Assistant-Entity fuer '{parsed['target']}' gefunden. Nutze am besten die genaue Entity-ID oder speichere zuerst eine Notiz mit 'Merke HA <entity_id>: ...'."
            )

        resolved_domain = str(resolved["entity_id"]).split(".", 1)[0].strip().lower()
        domain = _resolve_effective_home_assistant_domain(
            parsed_domain=parsed_domain,
            resolved_domain=resolved_domain,
            service=service,
        )
        entity_id = resolved["entity_id"]
        await client.call_service(domain=domain, service=service, entity_id=entity_id, service_data=service_data)
        await _maybe_learn_home_assistant_alias(
            settings,
            alias=normalized_target,
            domain=domain,
            entity_ids=[entity_id],
            learned_from=message,
        )

        detail = f"{domain}.{service}"
        if service_data:
            detail += f" {json.dumps(service_data, ensure_ascii=False)}"
        assistant_text = f"Home Assistant ausgefuehrt: {entity_id} -> {detail}."
        if resolved.get("friendly_name"):
            assistant_text += f" Name: {resolved['friendly_name']}."
        if resolved.get("note"):
            assistant_text += f" Merker: {resolved['note']}."
    return {
        "assistant_text": assistant_text,
        "route_reason": route_reason,
    }


async def _try_handle_home_assistant_lookup(settings, message: str) -> dict[str, str] | None:
    lookup = _parse_home_assistant_lookup_request(message)
    if lookup is None:
        return None

    client = HomeAssistantClient(settings)
    entities = await client.list_entities(domain=lookup.get("domain"), limit=300)
    if not entities:
        return {
            "assistant_text": "Home Assistant ist erreichbar, aber es wurden keine passenden Entities gefunden.",
            "route_reason": "home_assistant_lookup",
            "backend_called": True,
        }

    tokens = _home_assistant_search_tokens(str(lookup.get("target") or ""))
    selected: list[dict[str, object]] = []
    for item in entities:
        entity_id = str(item.get("entity_id") or "")
        friendly_name = str(item.get("friendly_name") or "")
        haystack = f"{entity_id.lower()} {friendly_name.lower()}"
        if not tokens or any(token in haystack for token in tokens):
            selected.append(item)

    if not selected and tokens:
        selected = entities[:20]

    if not selected:
        return {
            "assistant_text": f"Keine passenden Home-Assistant-Entities fuer '{lookup.get('target')}' gefunden.",
            "route_reason": "home_assistant_lookup",
            "backend_called": True,
        }

    lines = []
    for item in selected[:20]:
        lines.append(
            f"- {item.get('entity_id')} | state={item.get('state')} | name={item.get('friendly_name') or '-'}"
        )
    return {
        "assistant_text": "Gefundene Home-Assistant-Entities:\n" + "\n".join(lines),
        "route_reason": "home_assistant_lookup",
        "backend_called": True,
    }


async def _run_home_assistant_session_follow_up(
    *,
    settings,
    session: ChatSession,
    service: str,
    parsed_domain: str,
    service_data: dict[str, object],
    route_reason: str,
    label: str,
) -> dict[str, str] | None:
    last_context = _extract_last_home_assistant_action_context(session)
    if last_context is None:
        return None

    entity_ids = _dedupe_entity_ids(list(last_context["entity_ids"]))
    if not entity_ids:
        return None

    resolved_domain = str(last_context["domain"])
    domain = _resolve_effective_home_assistant_domain(
        parsed_domain=parsed_domain,
        resolved_domain=resolved_domain,
        service=service,
    )
    payload = dict(service_data)
    payload["entity_id"] = entity_ids
    client = HomeAssistantClient(settings)
    await client.call_service(domain=domain, service=service, entity_id=None, service_data=payload)
    return {
        "assistant_text": (
            f"{label}: {domain}.{service} fuer {len(entity_ids)} Entities -> "
            + ", ".join(entity_ids)
            + "."
        ),
        "route_reason": route_reason,
    }


def _parse_home_assistant_action(message: str) -> dict[str, object] | None:
    text = _normalize_home_assistant_action_message(message)
    if not text:
        return None

    all_matches = _message_requests_all_matches(text)

    on_off = re.match(
        r"^\s*(?:ha\s+)?(?:kannst\s+du\s+)?(?:bitte\s+)?(?:schalte|mach|mache)\s+(?P<target>.+?)\s+(?P<state>ein|an|aus|auf|zu)\s*[.!?]?\s*$",
        text,
        re.IGNORECASE,
    )
    if on_off:
        state = on_off.group("state").lower()
        target = _normalize_home_assistant_action_target(on_off.group("target"))
        domain = _guess_home_assistant_domain_from_target(target)
        service = "turn_on" if state in {"ein", "an", "auf"} else "turn_off"
        return {
            "domain": domain,
            "service": service,
            "target": target,
            "service_data": {},
            "all_matches": all_matches,
        }

    embedded_on_off = re.search(
        r"\b(?:schalte|mach|mache)\s+(?P<target>.+?)\s+(?P<state>ein|an|aus|auf|zu)\s*[.!?]?\s*$",
        text,
        re.IGNORECASE,
    )
    if embedded_on_off:
        state = embedded_on_off.group("state").lower()
        target = _normalize_home_assistant_action_target(embedded_on_off.group("target"))
        domain = _guess_home_assistant_domain_from_target(target)
        service = "turn_on" if state in {"ein", "an", "auf"} else "turn_off"
        return {
            "domain": domain,
            "service": service,
            "target": target,
            "service_data": {},
            "all_matches": all_matches,
        }

    make_on_off = re.match(
        r"^\s*(?:kannst\s+du\s+)?(?:bitte\s+)?(?P<target>.+?)\s+(?P<state>ein|an|aus|auf|zu)\s+(?:machen|schalten)\s*(?:bitte)?\s*[.!?]?\s*$",
        text,
        re.IGNORECASE,
    )
    if make_on_off:
        state = make_on_off.group("state").lower()
        target = _normalize_home_assistant_action_target(make_on_off.group("target"))
        domain = _guess_home_assistant_domain_from_target(target)
        service = "turn_on" if state in {"ein", "an", "auf"} else "turn_off"
        return {
            "domain": domain,
            "service": service,
            "target": target,
            "service_data": {},
            "all_matches": all_matches,
        }

    trailing_all = re.match(
        r"^\s*(?:kannst\s+du\s+)?(?:bitte\s+)?(?P<target>.+?)\s+(?P<state>ein|an|aus|auf|zu)\s+(?:machen|schalten)\s+alle(?:\s+bitte)?\s*[.!?]?\s*$",
        text,
        re.IGNORECASE,
    )
    if trailing_all:
        state = trailing_all.group("state").lower()
        target = _normalize_home_assistant_action_target(trailing_all.group("target"))
        domain = _guess_home_assistant_domain_from_target(target)
        service = "turn_on" if state in {"ein", "an", "auf"} else "turn_off"
        return {
            "domain": domain,
            "service": service,
            "target": target,
            "service_data": {},
            "all_matches": True,
        }

    open_close = re.match(
        r"^\s*(?:ha\s+)?(?:kannst\s+du\s+)?(?:bitte\s+)?(?P<verb>oeffne|öffne|schliesse|schließe)\s+(?P<target>.+?)\s*[.!?]?\s*$",
        text,
        re.IGNORECASE,
    )
    if open_close:
        verb = open_close.group("verb").lower()
        target = _normalize_home_assistant_action_target(open_close.group("target"))
        domain = _guess_home_assistant_domain_from_target(target)
        service = "turn_on" if verb in {"oeffne", "öffne"} else "turn_off"
        return {
            "domain": domain,
            "service": service,
            "target": target,
            "service_data": {},
            "all_matches": all_matches,
        }

    activate = re.match(
        r"^\s*(?:ha\s+)?(?P<verb>aktiviere|deaktiviere|einschalten|ausschalten)\s+(?P<target>.+?)\s*[.!?]?\s*$",
        text,
        re.IGNORECASE,
    )
    if activate:
        verb = activate.group("verb").lower()
        target = _normalize_home_assistant_action_target(activate.group("target"))
        domain = _guess_home_assistant_domain_from_target(target)
        service = "turn_off" if verb in {"deaktiviere", "ausschalten"} else "turn_on"
        return {
            "domain": domain,
            "service": service,
            "target": target,
            "service_data": {},
            "all_matches": all_matches,
        }

    delegated_make = re.search(
        r"\b(?:ich\s+wollte\s+nur\s+dass?\s+du|ich\s+will\s+dass?\s+du|ich\s+moechte\s+dass?\s+du|ich\s+möchte\s+dass?\s+du)\s+"
        r"(?P<target>.+?)\s+(?P<state>ein|an|aus|auf|zu)\s+mach(?:st|en)?\s*[.!?]?\s*$",
        text,
        re.IGNORECASE,
    )
    if delegated_make:
        state = delegated_make.group("state").lower()
        target = _normalize_home_assistant_action_target(delegated_make.group("target"))
        domain = _guess_home_assistant_domain_from_target(target)
        service = "turn_on" if state in {"ein", "an", "auf"} else "turn_off"
        return {
            "domain": domain,
            "service": service,
            "target": target,
            "service_data": {},
            "all_matches": all_matches,
        }

    climate = re.match(
        r"^\s*(?:ha\s+)?(?:setze|stelle)\s+(?P<target>.+?)\s+(?:auf\s+)?(?P<temp>\d+(?:[.,]\d+)?)\s*(?:grad|°c|°)?\s*[.!?]?\s*$",
        text,
        re.IGNORECASE,
    )
    if climate:
        raw_temp = climate.group("temp").replace(",", ".")
        return {
            "domain": "climate",
            "service": "set_temperature",
            "target": _normalize_home_assistant_action_target(climate.group("target")),
            "service_data": {"temperature": float(raw_temp)},
            "all_matches": False,
        }

    script_match = re.match(
        r"^\s*(?:ha\s+)?(?:starte|fuehre)\s+(?P<target>.+?)(?:\s+aus)?\s*[.!?]?\s*$",
        text,
        re.IGNORECASE,
    )
    if script_match:
        return {
            "domain": "script",
            "service": "turn_on",
            "target": _normalize_home_assistant_action_target(script_match.group("target")),
            "service_data": {},
            "all_matches": False,
        }
    return None


def _normalize_home_assistant_action_message(message: str) -> str:
    text = (message or "").strip()
    text = re.sub(
        r"^\s*(?:ok(?:ay)?|ja|jo|gut|also|naja|hm+|hmm+)\s*[,;:!-]*\s*",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"^\s*(?:und\s+)?(?:dann\s+)?", "", text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip()


def _parse_home_assistant_follow_up_action(message: str) -> dict[str, str] | None:
    text = (message or "").strip().lower()
    if not text:
        return None

    patterns = [
        re.compile(
            r"^\s*(?:ok(?:ay)?\s+)?(?:und\s+)?(?:jetzt\s+)?(?:bitte\s+)?(?:wieder\s+)?(?P<state>an|ein|aus|auf|zu)(?:\s+bitte)?\s*[.!?]?\s*$"
        ),
        re.compile(
            r"^\s*(?:ok(?:ay)?\s+)?(?:und\s+)?(?:jetzt\s+)?(?:bitte\s+)?(?:mach|mache|schalte)\s+(?:sie|es|die|den|das)\s+(?:jetzt\s+)?(?:wieder\s+)?(?P<state>an|ein|aus|auf|zu)(?:\s+bitte)?\s*[.!?]?\s*$"
        ),
    ]
    for pattern in patterns:
        match = pattern.match(text)
        if not match:
            continue
        state = match.group("state").lower()
        return {"service": "turn_on" if state in {"an", "ein", "auf"} else "turn_off"}
    return None


def _parse_home_assistant_lookup_request(message: str) -> dict[str, str] | None:
    text = _normalize_home_assistant_action_message(message)
    if not text:
        return None

    patterns = [
        re.compile(r"^\s*(?:suche|such)\s+(?:mal\s+)?nach\s+(?P<target>.+?)\s*[.!?]?\s*$", re.IGNORECASE),
        re.compile(r"^\s*(?:liste|zeig(?:e)?)\s+(?P<target>.+?)\s+(?:auf|an)\s*[.!?]?\s*$", re.IGNORECASE),
        re.compile(r"^\s*welche\s+(?P<target>.+?)\s+gibt\s+es\s*[.!?]?\s*$", re.IGNORECASE),
    ]
    for pattern in patterns:
        match = pattern.match(text)
        if not match:
            continue
        target = _normalize_home_assistant_action_target(match.group("target"))
        return {"target": target, "domain": _guess_home_assistant_domain_from_target(target)}
    return None


def _parse_home_assistant_retry_action(message: str) -> dict[str, str] | None:
    text = (message or "").strip().lower()
    if not text:
        return None

    retry_patterns = (
        r"\bversuch(?:'s| es)?\s+noch\s*mal\b",
        r"\bprobier(?:'s| es)?\s+noch\s*mal\b",
        r"\bprobiere(?:\s+es)?\s+noch\s*mal\b",
        r"\bmach(?:\s+es)?\s+noch\s*mal\b",
        r"\bschalte(?:\s+es)?\s+noch\s*mal\b",
        r"\bnoch\s*mal\b",
        r"\berneut\b",
        r"\bwiederhole\s+das\b",
    )
    if any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in retry_patterns):
        return {"action": "retry"}
    return None


def _extract_last_home_assistant_action_context(session: ChatSession | None) -> dict[str, object] | None:
    if session is None:
        return None

    # Follow-up phrases like "ok und jetzt wieder aus" should reuse the
    # last successful HA action from the same chat session.
    for message in reversed(session.messages):
        if message.role != "assistant":
            continue
        if (message.model_used or "").strip().lower() != "home_assistant":
            continue
        parsed = _parse_home_assistant_action_context_from_text(message.content)
        if parsed is not None:
            return parsed
    return None


def _parse_home_assistant_action_context_from_text(text: str) -> dict[str, object] | None:
    content = (text or "").strip()
    if not content:
        return None

    multi_match = re.search(
        r"Home Assistant (?:Follow-up )?ausgefuehrt(?: ueber gelernten Alias '[^']+')?: "
        r"(?P<domain>light|switch|climate|script)\.(?P<service>[a-z_]+)"
        r"(?:\s+\{.*?\})?\s+fuer\s+\d+\s+Entities\s+->",
        content,
        flags=re.IGNORECASE,
    )
    if multi_match:
        tail = content.split("->", 1)[1] if "->" in content else ""
        entity_ids = _dedupe_entity_ids(
            re.findall(
                r"(?:light|switch|climate|script)\.[a-z0-9_]+",
                tail.lower(),
            )
        )
        if entity_ids:
            return {
                "domain": multi_match.group("domain").lower(),
                "service": multi_match.group("service").lower(),
                "entity_ids": entity_ids,
            }

    single_match = re.search(
        r"Home Assistant ausgefuehrt:\s+"
        r"(?P<entity_id>(?:light|switch|climate|script)\.[a-z0-9_]+)\s+->\s+"
        r"(?P<domain>light|switch|climate|script)\.(?P<service>[a-z_]+)"
        r"(?:\s+\{.*?\})?\.",
        content,
        flags=re.IGNORECASE,
    )
    if single_match:
        entity_id = single_match.group("entity_id").lower()
        return {
            "domain": single_match.group("domain").lower(),
            "service": single_match.group("service").lower(),
            "entity_ids": [entity_id],
        }

    return None


def _message_requests_all_matches(text: str) -> bool:
    lowered = f" {(text or '').lower()} "
    return any(
        needle in lowered
        for needle in (" alle ", " dalle ", " alles ", " sämtliche ", " saemtliche ", " lichter ", " lcihter ", " lampen ")
    )


def _expand_home_assistant_target_parts(target: str, domain: str) -> list[str]:
    normalized = (target or "").strip()
    if not normalized:
        return []

    if "," in normalized:
        raw_parts = [part.strip() for part in normalized.split(",") if part.strip()]
    elif re.search(r"\s+und\s+", normalized, flags=re.IGNORECASE):
        raw_parts = [part.strip() for part in re.split(r"\s+und\s+", normalized, flags=re.IGNORECASE) if part.strip()]
    else:
        return [normalized]

    if len(raw_parts) <= 1:
        return [normalized]

    suffix = _infer_home_assistant_shared_suffix(normalized, domain)
    expanded: list[str] = []
    for index, part in enumerate(raw_parts):
        lowered = part.lower()
        if index < len(raw_parts) - 1 and suffix and suffix not in lowered:
            expanded.append(f"{part} {suffix}".strip())
        else:
            expanded.append(part)
    return expanded


def _infer_home_assistant_shared_suffix(target: str, domain: str) -> str:
    lowered = (target or "").lower()
    candidates_by_domain = {
        "light": ("licht", "lampe"),
        "switch": ("schalter", "steckdose", "switch"),
        "climate": ("thermostat", "klima", "heizung"),
        "script": ("script", "szene"),
    }
    for candidate in candidates_by_domain.get(domain, ()):
        if candidate in lowered:
            return candidate
    return ""


def _normalize_home_assistant_action_target(raw_target: str) -> str:
    target = (raw_target or "").strip()
    target = re.sub(r"^\s*(?:ok(?:ay)?|ja|jo|gut|also|naja)\s+", "", target, flags=re.IGNORECASE)
    target = re.sub(r"^\s*naja\s+", "", target, flags=re.IGNORECASE)
    target = re.sub(r"^\s*du\s+sollst\s+", "", target, flags=re.IGNORECASE)
    target = re.sub(r"^\s*sollst\s+du\s+", "", target, flags=re.IGNORECASE)
    target = re.sub(r"^(?:(?:kannst|kanns|kanns)\s+du\s+)", "", target, flags=re.IGNORECASE)
    target = re.sub(r"^(?:(?:koenntest|könntest|koennten|könnten)\s+du\s+)", "", target, flags=re.IGNORECASE)
    target = re.sub(r"^(?:(?:bitte|mal|nochmal|noch\s+mal|wieder)\s+)+", "", target, flags=re.IGNORECASE)
    target = re.sub(r"^(?:alle\s+|alles\s+)", "", target, flags=re.IGNORECASE)
    target = re.sub(r"^(?:das|den|die|dem|der)\s+", "", target, flags=re.IGNORECASE)
    target = re.sub(r"\bim\s+", "", target, flags=re.IGNORECASE)
    target = re.sub(r"\bin\s+dem\s+", "", target, flags=re.IGNORECASE)
    target = re.sub(r"\bgaming\s+zimmer\b", "gamingzimmer", target, flags=re.IGNORECASE)
    target = re.sub(r"\blcihter\b", "licht", target, flags=re.IGNORECASE)
    target = re.sub(r"\blichter\b", "licht", target, flags=re.IGNORECASE)
    target = re.sub(r"\bfenstern\b", "fenster", target, flags=re.IGNORECASE)
    target = re.sub(r"\blampen\b", "lampe", target, flags=re.IGNORECASE)
    target = re.sub(r"\blch\b", "licht", target, flags=re.IGNORECASE)
    target = re.sub(r"\blich\b", "licht", target, flags=re.IGNORECASE)
    target = re.sub(r"\blciht\b", "licht", target, flags=re.IGNORECASE)
    target = re.sub(r"\bbitte\b", "", target, flags=re.IGNORECASE)
    target = re.sub(r"\bdoch\b", "", target, flags=re.IGNORECASE)
    target = re.sub(r"\bjetzt\b", "", target, flags=re.IGNORECASE)
    target = re.sub(r"\bwieder\b", "", target, flags=re.IGNORECASE)
    target = re.sub(r"\bimmer\s+noch\b", "", target, flags=re.IGNORECASE)
    target = re.sub(r"\s+(?:alle|alles)$", "", target, flags=re.IGNORECASE)
    target = re.sub(r"\s+", " ", target).strip()
    return target


def _looks_like_home_assistant_reference_target(target: str) -> bool:
    # Short follow-up targets like "es", "das wieder" or "nochmal" should be
    # mapped back to the last successful HA action instead of running a new
    # entity search against Home Assistant.
    lowered = re.sub(r"[^0-9A-Za-z_ÄÖÜäöüß ]+", " ", (target or "").lower())
    tokens = [token for token in lowered.split() if token]
    if not tokens:
        return False

    reference_tokens = {
        "es",
        "sie",
        "ihn",
        "ihm",
        "ihnen",
        "das",
        "den",
        "die",
        "der",
        "dem",
        "dies",
        "dieses",
        "diese",
        "dieser",
        "wieder",
        "jetzt",
        "bitte",
        "noch",
        "mal",
        "nochmal",
        "nochmals",
        "erneut",
        "doch",
    }
    # Pure reference targets like "es" or "das wieder" should reuse the last
    # successful HA action instead of being resolved like a new entity name.
    return all(token in reference_tokens for token in tokens)


def _guess_home_assistant_domain_from_target(target: str) -> str:
    lowered = (target or "").lower()
    if (
        lowered.startswith("switch.")
        or "schalter" in lowered
        or "steckdose" in lowered
        or "fenster" in lowered
        or "window" in lowered
    ):
        return "switch"
    if lowered.startswith("script."):
        return "script"
    if lowered.startswith("climate.") or "thermostat" in lowered or "heizung" in lowered or "klima" in lowered:
        return "climate"
    return "light"


def _resolve_effective_home_assistant_domain(*, parsed_domain: str, resolved_domain: str, service: str) -> str:
    normalized_service = (service or "").strip().lower()
    if normalized_service == "set_temperature":
        if resolved_domain != "climate":
            raise HomeAssistantConfigError(
                f"Die erkannte Entity ist vom Typ '{resolved_domain}', aber fuer set_temperature wird eine climate-Entity gebraucht."
            )
        return "climate"
    if normalized_service == "turn_on" and parsed_domain == "script":
        if resolved_domain != "script":
            raise HomeAssistantConfigError(
                f"Die erkannte Entity ist vom Typ '{resolved_domain}', aber der Befehl wurde als Script-Aufruf erkannt."
            )
        return "script"
    if normalized_service in {"turn_on", "turn_off"} and resolved_domain in {"light", "switch", "script"}:
        return resolved_domain
    return parsed_domain


async def _resolve_home_assistant_entity(settings, client: HomeAssistantClient, target_text: str) -> dict[str, str] | None:
    target = (target_text or "").strip()
    if not target:
        return None

    explicit_match = re.search(r"\b(?:light|switch|climate|script)\.[a-z0-9_]+\b", target.lower())
    entities = await client.list_entities(limit=200)
    note_store = get_home_assistant_note_store(settings)
    notes = await note_store.list_notes(limit=200) if note_store is not None else []
    notes_by_id = {item.entity_id: item.note for item in notes}

    if explicit_match:
        explicit_id = explicit_match.group(0)
        for item in entities:
            entity_id = str(item.get("entity_id") or "").lower()
            if entity_id == explicit_id:
                return {
                    "entity_id": entity_id,
                    "friendly_name": str(item.get("friendly_name") or ""),
                    "note": str(notes_by_id.get(entity_id) or ""),
                }

    normalized_target = target.lower()
    tokens = _home_assistant_search_tokens(normalized_target)
    scored_matches: list[tuple[int, dict[str, str]]] = []

    for item in entities:
        entity_id = str(item.get("entity_id") or "").lower()
        entity_domain = entity_id.split(".", 1)[0] if "." in entity_id else ""
        preferred_domain = _guess_home_assistant_domain_from_target(target)
        if preferred_domain and entity_domain != preferred_domain:
            continue
        friendly_name = str(item.get("friendly_name") or "").strip()
        note = str(notes_by_id.get(entity_id) or "")
        friendly_lower = friendly_name.lower().strip()
        note_lower = note.lower()
        haystack = f"{entity_id} {friendly_lower} {note_lower}"
        score = 0
        if normalized_target == entity_id or normalized_target == friendly_lower:
            score += 100
        if normalized_target and normalized_target in haystack:
            score += 40
        for token in tokens:
            if token in haystack:
                score += 10
        if tokens:
            entity_words = {
                token
                for token in re.findall(r"[0-9A-Za-z_ÄÖÜäöüß]{2,}", f"{entity_id.replace('.', ' ').replace('_', ' ')} {friendly_lower}")
                if token.lower() not in {"light", "switch", "climate", "script"}
            }
            extra_words = [word for word in entity_words if word.lower() not in tokens]
            score -= len(extra_words)
        if score > 0:
            scored_matches.append(
                (
                    score,
                    {
                        "entity_id": entity_id,
                        "friendly_name": friendly_name,
                        "note": note,
                    },
                )
            )

    if not scored_matches:
        return None

    scored_matches.sort(key=lambda item: item[0], reverse=True)
    best_score = scored_matches[0][0]
    best_match = scored_matches[0][1]

    ambiguous = [
        item
        for score, item in scored_matches[1:6]
        if score == best_score
    ]
    if ambiguous:
        candidates = [best_match, *ambiguous]
        candidate_text = ", ".join(
            f"{item['entity_id']}" + (f" ({item['friendly_name']})" if item.get("friendly_name") else "")
            for item in candidates
        )
        raise HomeAssistantConfigError(
            f"Mehrdeutige Home-Assistant-Entity fuer '{target}': {candidate_text}. "
            "Bitte nutze die genaue Entity-ID oder speichere eine Notiz mit 'Merke HA <entity_id>: ...'."
        )

    return best_match


async def _resolve_home_assistant_entities(
    settings,
    client: HomeAssistantClient,
    target_text: str,
    *,
    preferred_domain: str | None = None,
) -> list[dict[str, str]]:
    target = (target_text or "").strip()
    if not target:
        return []

    entities = await client.list_entities(limit=300)
    note_store = get_home_assistant_note_store(settings)
    notes = await note_store.list_notes(limit=300) if note_store is not None else []
    notes_by_id = {item.entity_id: item.note for item in notes}
    normalized_target = target.lower()
    tokens = _home_assistant_search_tokens(normalized_target)
    domain_generic_tokens = {
        "light": {"licht", "lampe"},
        "switch": {"schalter", "steckdose", "switch"},
        "climate": {"klima", "thermostat", "heizung", "temperatur"},
        "script": {"script", "szene"},
    }
    generic_tokens = domain_generic_tokens.get((preferred_domain or "").lower(), set())
    meaning_tokens = [token for token in tokens if token not in generic_tokens]

    scored: list[tuple[int, dict[str, str]]] = []
    for item in entities:
        entity_id = str(item.get("entity_id") or "").lower()
        entity_domain = entity_id.split(".", 1)[0] if "." in entity_id else ""
        if preferred_domain and entity_domain != preferred_domain:
            continue

        friendly_name = str(item.get("friendly_name") or "").strip()
        note = str(notes_by_id.get(entity_id) or "")
        haystack = f"{entity_id} {friendly_name.lower()} {note.lower()}"
        score = 0
        if normalized_target and normalized_target in haystack:
            score += 60
        matching_tokens = [token for token in tokens if token in haystack]
        meaning_matches = [token for token in meaning_tokens if token in haystack]
        score += len(matching_tokens) * 8
        score += len(meaning_matches) * 12
        if meaning_tokens and not meaning_matches:
            continue
        if not meaning_tokens and not matching_tokens:
            continue
        if score <= 0:
            continue
        scored.append(
            (
                score,
                {
                    "entity_id": entity_id,
                    "friendly_name": friendly_name,
                    "note": note,
                },
            )
        )

    deduped: list[dict[str, str]] = []
    seen: set[str] = set()
    scored.sort(key=lambda item: item[0], reverse=True)
    for _, item in scored:
        entity_id = item["entity_id"]
        if entity_id not in seen:
            deduped.append(item)
            seen.add(entity_id)
    return deduped


def _home_assistant_search_tokens(text: str) -> list[str]:
    stopwords = {
        "ha",
        "home",
        "assistant",
        "ein",
        "an",
        "aus",
        "auf",
        "grad",
        "bitte",
        "alle",
        "alles",
        "mach",
        "schalte",
        "setze",
        "stelle",
        "aktiviere",
        "deaktiviere",
        "einschalten",
        "ausschalten",
        "starte",
        "fuehre",
        "das",
        "den",
        "die",
        "der",
        "dem",
        "des",
        "im",
        "in",
        "am",
        "und",
        "von",
        "zu",
        "mit",
    }
    tokens = re.findall(r"[0-9A-Za-z_ÄÖÜäöüß]{2,}", text or "")
    result: list[str] = []
    for token in tokens:
        lowered = token.lower()
        if lowered not in stopwords:
            result.append(lowered)
            if lowered.endswith("zimmer") and len(lowered) > 6:
                stem = lowered[:-6]
                if stem and stem not in stopwords:
                    result.append(stem)
    return result


def _home_assistant_error_code(exc: Exception) -> str:
    message = str(exc).lower()
    if "mehrdeutig" in message:
        return "home_assistant_entity_ambiguous"
    if "keine passende" in message:
        return "home_assistant_entity_not_found"
    return "home_assistant_not_configured"


def _dedupe_entity_ids(entity_ids: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in entity_ids:
        normalized = (item or "").strip().lower()
        if normalized and normalized not in seen:
            result.append(normalized)
            seen.add(normalized)
    return result


async def _maybe_learn_home_assistant_alias(
    settings,
    *,
    alias: str,
    domain: str,
    entity_ids: list[str],
    learned_from: str,
    allow_group_alias: bool = True,
) -> None:
    alias_store = get_home_assistant_alias_store(settings)
    if alias_store is None:
        return

    clean_alias = normalize_home_assistant_alias(alias)
    clean_entity_ids = _dedupe_entity_ids(entity_ids)
    if not clean_alias or not clean_entity_ids:
        return
    if re.search(r"\b(?:light|switch|climate|script)\.[a-z0-9_]+\b", clean_alias):
        return
    if len(clean_alias) < 4:
        return
    if len(clean_entity_ids) == 1 and _looks_like_home_assistant_group_alias(clean_alias):
        return
    if len(clean_entity_ids) > 1 and not allow_group_alias:
        return

    await alias_store.upsert_alias(
        alias=clean_alias,
        domain=domain,
        entity_ids=clean_entity_ids,
        learned_from=learned_from,
    )


def _looks_like_home_assistant_group_alias(alias: str) -> bool:
    lowered = (alias or "").lower()
    return any(marker in lowered for marker in ("zimmer", "lichter", "lampen", "gruppe", "alle "))


async def _require_session(store, session_id: str) -> ChatSession:
    session = await store.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found.")
    return session


def _serialize_message(message):
    from app.schemas.admin_chat import AdminChatMessageResponse

    return AdminChatMessageResponse(
        id=message.id,
        role=message.role,
        content=message.content,
        model_used=message.model_used,
        prompt_tokens=message.prompt_tokens,
        completion_tokens=message.completion_tokens,
        total_tokens=message.total_tokens,
        tokens_per_second=message.tokens_per_second,
        created_at=message.created_at,
    )


def _serialize_memory_summary(summary) -> AdminMemorySummaryResponse:
    return AdminMemorySummaryResponse(
        id=summary.id,
        session_id=summary.session_id,
        session_title=summary.session_title,
        summary_kind=summary.summary_kind,
        content=summary.content,
        source_message_count=summary.source_message_count,
        resolved_model=summary.resolved_model,
        created_at=summary.created_at,
    )


def _serialize_session(session: ChatSession) -> AdminSessionResponse:
    settings = get_settings()
    return AdminSessionResponse(
        id=session.id,
        title=session.title,
        mode=session.mode,
        resolved_model=session.resolved_model,
        route_reason=session.route_reason,
        summary=session.summary,
        token_estimate=_estimate_session_tokens(session, settings.context_chars_per_token),
        message_count=len(session.messages),
        created_at=session.created_at,
        updated_at=session.updated_at,
        messages=[_serialize_message(item) for item in session.messages],
    )


def _extract_assistant_text(response_payload: dict) -> str:
    choices = response_payload.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    content = message.get("content")
    return content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)


def _extract_response_metrics(response_payload: dict) -> dict[str, int | float | None]:
    usage = response_payload.get("usage") or {}
    timings = response_payload.get("timings") or {}
    prompt_tokens = usage.get("prompt_tokens")
    completion_tokens = usage.get("completion_tokens")
    total_tokens = usage.get("total_tokens")
    tokens_per_second = timings.get("predicted_per_second")

    return {
        "prompt_tokens": int(prompt_tokens) if isinstance(prompt_tokens, int) else None,
        "completion_tokens": int(completion_tokens) if isinstance(completion_tokens, int) else None,
        "total_tokens": int(total_tokens) if isinstance(total_tokens, int) else None,
        "tokens_per_second": round(float(tokens_per_second), 2) if isinstance(tokens_per_second, (int, float)) else None,
    }


def _estimate_session_tokens(session: ChatSession, chars_per_token: float) -> int:
    total_chars = sum(len(item.content) for item in session.messages)
    if session.summary:
        total_chars += len(session.summary)
    if total_chars <= 0:
        return 0
    divisor = chars_per_token if chars_per_token > 0 else 4.0
    return max(1, int(total_chars / divisor))


def _extract_content_from_sse(chunk: bytes) -> str:
    if not chunk.startswith(b"data: "):
        return ""
    payload = chunk[6:].strip()
    if payload == b"[DONE]":
        return ""
    try:
        data = json.loads(payload.decode("utf-8"))
    except json.JSONDecodeError:
        return ""
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    delta = choices[0].get("delta") or {}
    content = delta.get("content")
    return content if isinstance(content, str) else ""


def _extract_metrics_from_sse(chunk: bytes) -> dict[str, int | float | None]:
    empty = {
        "prompt_tokens": None,
        "completion_tokens": None,
        "total_tokens": None,
        "tokens_per_second": None,
    }
    if not chunk.startswith(b"data: "):
        return empty

    payload = chunk[6:].strip()
    if payload == b"[DONE]":
        return empty

    try:
        data = json.loads(payload.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return empty

    if not isinstance(data, dict):
        return empty

    return _extract_response_metrics(data)


def _admin_chat_html() -> str:
    return dedent(
        """\
        <!doctype html>
        <html lang="de">
        <head>
          <meta charset="utf-8">
          <meta name="viewport" content="width=device-width, initial-scale=1">
          <title>llm-gateway chat</title>
          <style>
            :root {
              --bg:#0b0f14;
              --card:#11161d;
              --card-2:#161c24;
              --ink:#d8e4ef;
              --muted:#7f92a3;
              --line:#2b3744;
              --accent:#8be28b;
              --accent-2:#67c1ff;
              --warn:#ffcc6a;
            }
            * { box-sizing:border-box; }
            body {
              margin:0;
              font-family:ui-monospace,SFMono-Regular,Consolas,"Liberation Mono",monospace;
              background:
                linear-gradient(180deg,#10151c 0%, var(--bg) 100%);
              color:var(--ink);
            }
            main { padding:20px; display:grid; grid-template-columns:minmax(260px, 320px) minmax(0, 1fr); gap:18px; min-height:100vh; }
            .panel {
              position:relative;
              background:linear-gradient(180deg, var(--card-2), var(--card));
              border:1px solid var(--line);
              border-radius:10px;
              padding:44px 18px 18px;
              min-width:0;
            }
            .panel::before {
              content:"";
              position:absolute;
              top:0;
              left:0;
              right:0;
              height:28px;
              border-bottom:1px solid var(--line);
              border-radius:10px 10px 0 0;
              background:
                radial-gradient(circle at 16px 14px, #ff5f56 0 4px, transparent 5px),
                radial-gradient(circle at 34px 14px, #ffbd2e 0 4px, transparent 5px),
                radial-gradient(circle at 52px 14px, #27c93f 0 4px, transparent 5px),
                linear-gradient(180deg, #1a222b, #161d26);
            }
            .muted { color:var(--muted); }
            button, select, textarea, input { font:inherit; }
            button {
              border:1px solid var(--line);
              border-radius:8px;
              padding:10px 14px;
              cursor:pointer;
              background:#1b2a1f;
              color:var(--accent);
              font-weight:700;
              text-transform:uppercase;
              letter-spacing:.05em;
            }
            button.secondary { background:#1a222b; color:var(--ink); }
            select, textarea, input[type="text"], input[type="file"] {
              width:100%;
              border:1px solid var(--line);
              border-radius:8px;
              padding:10px 12px;
              background:#0a0f14;
              color:var(--ink);
            }
            input[type="file"] {
              padding:8px 10px;
            }
            select[multiple] {
              min-height:140px;
            }
            select:focus, textarea:focus, input[type="text"]:focus, input[type="file"]:focus {
              outline:none;
              border-color:#4b8d4b;
              box-shadow:0 0 0 2px rgba(139,226,139,.10);
            }
            textarea { min-height:110px; resize:vertical; }
            .actions { display:flex; gap:10px; flex-wrap:wrap; margin-top:12px; }
            .session-summary {
              margin-top:14px;
              padding:12px;
              border:1px solid var(--line);
              border-radius:8px;
              background:#0c1117;
            }
            .session-summary h2 {
              margin:0 0 8px;
              font-size:1.05rem;
            }
            .session-list { display:flex; flex-direction:column; gap:10px; margin-top:16px; }
            .session-item {
              padding:12px;
              border:1px solid var(--line);
              border-radius:8px;
              background:#0c1117;
              cursor:pointer;
              color:var(--ink);
              text-align:left;
            }
            .session-item.active {
              border-color:#4b8d4b;
              background:#151d17;
            }
            .messages { min-height:420px; max-height:62vh; overflow:auto; display:flex; flex-direction:column; gap:12px; margin:14px 0; }
            .message {
              border:1px solid var(--line);
              border-radius:8px;
              padding:12px;
              background:#0c1117;
            }
            .message.user { border-left:5px solid var(--accent); }
            .message.assistant { border-left:5px solid var(--warn); }
            .message-header {
              display:flex;
              align-items:center;
              justify-content:space-between;
              gap:12px;
              margin-bottom:10px;
            }
            .message-role {
              color:var(--ink);
              font-size:.82rem;
              font-weight:700;
              text-transform:uppercase;
              letter-spacing:.08em;
            }
            .message-meta {
              color:var(--muted);
              font-size:.76rem;
              text-align:right;
            }
            .message-body p {
              margin:0 0 10px;
              line-height:1.45;
            }
            .message-body p:last-child {
              margin-bottom:0;
            }
            .message-body ul,
            .message-body ol {
              margin:0 0 10px 18px;
              padding:0;
              line-height:1.45;
            }
            .inline-code {
              display:inline-block;
              padding:1px 6px;
              border-radius:6px;
              border:1px solid var(--line);
              background:#101820;
              color:var(--accent-2);
            }
            .code-block {
              margin:12px 0;
              border:1px solid var(--line);
              border-radius:8px;
              overflow:hidden;
              background:#090d12;
            }
            .code-shell {
              box-shadow:inset 3px 0 0 var(--accent);
            }
            .code-generic {
              box-shadow:inset 3px 0 0 var(--accent-2);
            }
            .code-header {
              padding:8px 12px;
              background:#141b23;
              color:var(--muted);
              font-size:.75rem;
              font-weight:700;
              text-transform:uppercase;
              letter-spacing:.08em;
            }
            .code-shell .code-header {
              background:#122018;
              color:#9de69d;
            }
            .code-body {
              margin:0;
              padding:12px;
              max-height:360px;
              overflow:auto;
              background:#0a0f14;
              color:#d7e3ef;
            }
            .composer { display:grid; grid-template-columns:minmax(0, 1fr); gap:12px; }
            .composer-main { min-width:0; }
            .primary-actions { display:flex; gap:10px; flex-wrap:wrap; margin-top:12px; }
            .primary-actions button { min-width:120px; }
            .composer-side {
              display:grid;
              grid-template-columns:repeat(auto-fit, minmax(220px, 1fr));
              gap:12px;
              align-items:start;
            }
            .upload-box {
              border:1px solid var(--line);
              border-radius:8px;
              padding:12px;
              background:#0c1117;
            }
            .upload-grid {
              display:grid;
              gap:10px;
              margin-top:10px;
            }
            .hint {
              margin-top:8px;
              color:var(--muted);
              font-size:.78rem;
              line-height:1.4;
            }
            .topbar { display:flex; justify-content:flex-end; gap:16px; align-items:flex-start; }
            .status {
              margin-top:10px;
              padding:10px 12px;
              border-radius:8px;
              background:rgba(139,226,139,.10);
              border:1px solid #284131;
            }
            .stats {
              display:grid;
              grid-template-columns:repeat(3, minmax(120px, 1fr));
              gap:10px;
              margin:0;
              min-width:420px;
              max-width:520px;
            }
            .stat {
              border:1px solid var(--line);
              border-radius:8px;
              padding:10px;
              background:#0c1117;
            }
            .stat-label {
              color:var(--muted);
              font-size:.76rem;
              text-transform:uppercase;
              letter-spacing:.05em;
            }
            .stat-value {
              margin-top:6px;
              color:var(--accent);
              font-size:1.15rem;
              font-weight:700;
              line-height:1.2;
            }
            .stat-meta {
              margin-top:4px;
              color:var(--muted);
              font-size:.78rem;
            }
            h1, h2 {
              letter-spacing:.10em;
              text-transform:uppercase;
            }
            pre { white-space:pre-wrap; word-break:break-word; margin:0; }
            @media (max-width: 980px) {
              main { grid-template-columns:1fr; }
              .stats { grid-template-columns:1fr; }
            }
          </style>
        </head>
        <body>
          <main>
            <section class="panel">
              <h1>Admin Chat</h1>
              <div class="session-summary">
                <h2 id="sessionTitle">Keine Session</h2>
                <div class="muted" id="sessionMeta">Lege links eine Session an oder lade eine bestehende.</div>
              </div>
              <div class="actions">
                <button type="button" onclick="createSession()">Neue Session</button>
                <button type="button" class="secondary" onclick="loadSessions()">Sessions laden</button>
              </div>
              <div id="sessionList" class="session-list"></div>
            </section>
            <section class="panel">
              <div class="topbar">
                <div style="display:flex; flex-direction:column; gap:10px; align-items:flex-end;">
                  <label style="min-width:220px; width:220px;">
                    <span>Modus</span>
                    <select id="mode">
                      <option value="auto">Auto-Routing</option>
                      <option value="fast">Fast Model</option>
                      <option value="deep">Deep Model</option>
                    </select>
                  </label>
                </div>
              </div>
              <div id="messages" class="messages"></div>
              <div class="composer">
                <div class="composer-main">
                  <label>
                    <span>Nachricht</span>
                    <textarea id="prompt" placeholder="Schreibe hier direkt an die AI-Plattform..."></textarea>
                  </label>
                  <div class="primary-actions">
                    <button type="button" onclick="sendMessage()">Senden</button>
                    <button type="button" class="secondary" onclick="renameSession()">Umbenennen</button>
                    <button type="button" class="secondary" onclick="resetSession()">Reset</button>
                    <button type="button" class="secondary" onclick="deleteSession()">Loeschen</button>
                  </div>
                </div>
                <div class="composer-side">
                  <div class="upload-box">
                    <div class="stat-label">Datei direkt in den Chat laden</div>
                    <div class="upload-grid">
                      <input id="uploadTitle" type="text" placeholder="optional Titel fuer Text/PDF/Bild" />
                      <input id="uploadFile" type="file" accept=".txt,.md,.markdown,.log,.csv,.json,.yaml,.yml,.pdf,.jpg,.jpeg,.png,.webp,.gif,image/*" />
                      <button id="uploadButton" type="button" class="secondary" onclick="uploadDocument()">Upload</button>
                    </div>
                    <div id="uploadHint" class="hint">Der Upload nutzt das aktive Storage-Profil.</div>
                  </div>
                  <label>
                    <span>Dokumente als Kontext</span>
                    <select id="documentIds" multiple></select>
                  </label>
                  <label>
                    <span>Streaming</span>
                    <select id="streaming">
                      <option value="false">Nein</option>
                      <option value="true">Ja</option>
                    </select>
                  </label>
                  <label>
                    <span>Home Assistant lesen</span>
                    <select id="includeHomeAssistant">
                      <option value="false">Nur wenn im Text erkannt</option>
                      <option value="true">Immer einbeziehen</option>
                    </select>
                  </label>
                </div>
              </div>
              <div id="status" class="status">Session anlegen und loschatten.</div>
            </section>
          </main>
          <script>
            const sessionList = document.getElementById("sessionList");
            const messagesNode = document.getElementById("messages");
            const sessionTitle = document.getElementById("sessionTitle");
            const sessionMeta = document.getElementById("sessionMeta");
            const promptInput = document.getElementById("prompt");
            const modeInput = document.getElementById("mode");
            const statusNode = document.getElementById("status");
            const streamingInput = document.getElementById("streaming");
            const includeHomeAssistantInput = document.getElementById("includeHomeAssistant");
            const documentIdsInput = document.getElementById("documentIds");
            const uploadTitleInput = document.getElementById("uploadTitle");
            const uploadFileInput = document.getElementById("uploadFile");
            const uploadButton = document.getElementById("uploadButton");
            const uploadHint = document.getElementById("uploadHint");
            let currentSessionId = null;

            function setStatus(text, error = false) {
              statusNode.textContent = text;
              statusNode.style.background = error ? "#f9dddd" : "#e4f3e8";
              statusNode.style.color = error ? "#942f2f" : "#16231b";
            }

            function headers() {
              return { "Content-Type": "application/json" };
            }

            function errorMessageFrom(data, fallback) {
              if (data && typeof data === "object") {
                if (data.detail) return data.detail;
                if (data.error && typeof data.error === "object" && data.error.message) {
                  return data.error.message;
                }
              }
              return fallback;
            }

            function selectedDocumentIds() {
              return Array.from(documentIdsInput.selectedOptions || []).map((item) => item.value).filter(Boolean);
            }

            function scrollMessagesToBottom() {
              requestAnimationFrame(() => {
                messagesNode.scrollTop = messagesNode.scrollHeight;
              });
            }

            function escapeHtml(value) {
              return String(value || "")
                .replace(/&/g, "&amp;")
                .replace(/</g, "&lt;")
                .replace(/>/g, "&gt;")
                .replace(/"/g, "&quot;")
                .replace(/'/g, "&#39;");
            }

            function renderTextSegment(segment) {
              const escaped = escapeHtml(segment || "").trim();
              if (!escaped) return "";
              return escaped
                .split(/\\n{2,}/)
                .map((part) => renderTextBlock(part))
                .join("");
            }

            function renderInlineMarkup(text) {
              return String(text || "")
                .replace(/`([^`]+)`/g, '<code class="inline-code">$1</code>')
                .replace(/\\*\\*([^*]+)\\*\\*/g, '<strong>$1</strong>');
            }

            function renderTextBlock(part) {
              const lines = String(part || "").split("\\n").filter((line) => line.trim());
              if (!lines.length) return "";

              const bulletLines = lines.every((line) => /^[-*]\\s+/.test(line.trim()));
              if (bulletLines) {
                return `<ul>${lines.map((line) => `<li>${renderInlineMarkup(line.trim().replace(/^[-*]\\s+/, ""))}</li>`).join("")}</ul>`;
              }

              const numberedLines = lines.every((line) => /^\\d+\\.\\s+/.test(line.trim()));
              if (numberedLines) {
                return `<ol>${lines.map((line) => `<li>${renderInlineMarkup(line.trim().replace(/^\\d+\\.\\s+/, ""))}</li>`).join("")}</ol>`;
              }

              return `<p>${renderInlineMarkup(lines.join("<br>"))}</p>`;
            }

            function renderCodeBlock(language, code) {
              const lang = (language || "text").trim().toLowerCase();
              const label = lang || "code";
              const shellLike = ["bash", "sh", "zsh", "shell", "console", "cmd", "powershell"].includes(lang);
              const themeClass = shellLike ? "code-shell" : "code-generic";
              return `<div class="code-block ${themeClass}"><div class="code-header">${escapeHtml(label)}</div><pre class="code-body"><code>${escapeHtml(code || "")}</code></pre></div>`;
            }

            function renderMessageContent(text) {
              const source = String(text || "");
              const pattern = /```([a-zA-Z0-9_+-]*)\\n([\\s\\S]*?)```/g;
              let result = "";
              let lastIndex = 0;
              let match;

              while ((match = pattern.exec(source)) !== null) {
                result += renderTextSegment(source.slice(lastIndex, match.index));
                result += renderCodeBlock(match[1], match[2]);
                lastIndex = pattern.lastIndex;
              }

              result += renderTextSegment(source.slice(lastIndex));
              return result || "<p></p>";
            }

            function renderMessages(items) {
              messagesNode.innerHTML = "";
              for (const item of items) {
                const node = document.createElement("div");
                node.className = `message ${item.role}`;
                const metaParts = [];
                if (item.model_used) metaParts.push(`Modell: ${item.model_used}`);
                if (typeof item.completion_tokens === "number") metaParts.push(`Out: ${item.completion_tokens} tok`);
                if (typeof item.total_tokens === "number") metaParts.push(`Gesamt: ${item.total_tokens} tok`);
                if (typeof item.tokens_per_second === "number") metaParts.push(`${item.tokens_per_second.toFixed(2)} t/s`);
                const metaHtml = metaParts.length
                  ? `<div class="message-meta">${metaParts.join(" | ")}</div>`
                  : "";
                node.innerHTML = `
                  <div class="message-header">
                    <div class="message-role">${escapeHtml(item.role)}</div>
                    ${metaHtml}
                  </div>
                  <div class="message-body">${renderMessageContent(item.content)}</div>
                `;
                messagesNode.appendChild(node);
              }
              scrollMessagesToBottom();
            }

            function renderSession(session) {
              currentSessionId = session.id;
              sessionTitle.textContent = session.title;
              modeInput.value = session.mode || "auto";
              sessionMeta.textContent = `Modus: ${session.mode} | Modell: ${session.resolved_model || "-"} | Regel: ${session.route_reason || "-"} | Nachrichten: ${session.message_count || 0} | ca. Tokens: ${session.token_estimate || 0}`;
              renderMessages(session.messages || []);
            }

            async function loadAvailableDocuments(selectedDocumentId = null) {
              try {
                const res = await fetch("/api/admin/storage/overview", { headers: headers() });
                if (!res.ok) throw new Error(`Dokumente fehlgeschlagen: ${res.status}`);
                const data = await res.json();
                const documents = data.documents || [];
                const activeProfile = data.active_profile || null;
                documentIdsInput.innerHTML = "";
                for (const item of documents) {
                  const option = document.createElement("option");
                  option.value = item.id;
                  option.textContent = `${item.title || item.file_name} | ${item.storage_location_name || "-"}`;
                  if (selectedDocumentId && item.id === selectedDocumentId) {
                    option.selected = true;
                  }
                  documentIdsInput.appendChild(option);
                }
                if (activeProfile) {
                  uploadHint.textContent = `Ablage: ${activeProfile.name} (${activeProfile.backend_type})`;
                  uploadButton.disabled = false;
                } else {
                  uploadHint.textContent = "Kein aktives Storage-Profil. Bitte zuerst im Storage-Tab eines anlegen.";
                  uploadButton.disabled = true;
                }
              } catch (_error) {
                documentIdsInput.innerHTML = "";
                uploadHint.textContent = "Storage-Status konnte nicht geladen werden.";
                uploadButton.disabled = true;
              }
            }

            async function uploadDocument() {
              const file = uploadFileInput.files && uploadFileInput.files[0];
              if (!file) {
                setStatus("Bitte zuerst eine Datei auswaehlen.", true);
                return;
              }

              try {
                setStatus("Upload laeuft...");
                const formData = new FormData();
                formData.append("DOCUMENT_FILE", file);
                if (uploadTitleInput.value.trim()) {
                  formData.append("DOCUMENT_TITLE", uploadTitleInput.value.trim());
                }
                const res = await fetch("/api/admin/storage/upload", {
                  method: "POST",
                  body: formData,
                });
                const data = await res.json();
                if (!res.ok) {
                  throw new Error(errorMessageFrom(data, `Upload fehlgeschlagen: ${res.status}`));
                }
                const document = data.document || {};
                await loadAvailableDocuments(document.id || null);
                uploadTitleInput.value = "";
                uploadFileInput.value = "";
                setStatus(`Dokument '${document.title || document.file_name || "Upload"}' hochgeladen und als Kontext verfuegbar.`);
              } catch (error) {
                setStatus(error.message, true);
              }
            }

            async function loadSessions() {
              try {
                const res = await fetch("/api/admin/sessions", { headers: headers() });
                if (!res.ok) throw new Error(`Sessions fehlgeschlagen: ${res.status}`);
                const sessions = await res.json();
                sessionList.innerHTML = "";
                for (const session of sessions) {
                  const button = document.createElement("button");
                  button.type = "button";
                  button.className = `session-item ${session.id === currentSessionId ? "active" : ""}`;
                  button.innerHTML = `<strong>${session.title}</strong><div class="muted">${session.mode} | ${session.resolved_model || "noch kein Modell"}</div><div class="muted">ca. ${session.token_estimate || 0} Tokens | ${session.message_count || 0} Msg</div>`;
                  button.onclick = () => openSession(session.id);
                  sessionList.appendChild(button);
                }
                if (!currentSessionId && sessions.length) {
                  renderSession(sessions[0]);
                }
                setStatus("Sessions geladen.");
              } catch (error) {
                setStatus(error.message, true);
              }
            }

            async function createSession() {
              try {
                const res = await fetch("/api/admin/sessions", {
                  method: "POST",
                  headers: headers(),
                  body: JSON.stringify({ mode: modeInput.value }),
                });
                if (!res.ok) throw new Error(`Session anlegen fehlgeschlagen: ${res.status}`);
                const session = await res.json();
                renderSession(session);
                await loadSessions();
                setStatus("Neue Session angelegt.");
              } catch (error) {
                setStatus(error.message, true);
              }
            }

            async function openSession(sessionId) {
              try {
                const res = await fetch(`/api/admin/sessions/${sessionId}`, { headers: headers() });
                if (!res.ok) throw new Error(`Session laden fehlgeschlagen: ${res.status}`);
                const session = await res.json();
                renderSession(session);
                await loadSessions();
              } catch (error) {
                setStatus(error.message, true);
              }
            }

            async function resetSession() {
              if (!currentSessionId) return;
              try {
                const res = await fetch(`/api/admin/sessions/${currentSessionId}/reset`, {
                  method: "POST",
                  headers: headers(),
                });
                if (!res.ok) throw new Error(`Reset fehlgeschlagen: ${res.status}`);
                const session = await res.json();
                renderSession(session);
                await loadSessions();
                setStatus("Session zurueckgesetzt.");
              } catch (error) {
                setStatus(error.message, true);
              }
            }

            async function renameSession() {
              if (!currentSessionId) return;
              const currentTitle = sessionTitle.textContent || "";
              const nextTitle = window.prompt("Neuer Session-Name:", currentTitle);
              if (nextTitle === null) return;
              const cleanTitle = String(nextTitle || "").trim();
              if (!cleanTitle) {
                setStatus("Titel darf nicht leer sein.", true);
                return;
              }

              try {
                const res = await fetch(`/api/admin/sessions/${currentSessionId}/rename`, {
                  method: "POST",
                  headers: headers(),
                  body: JSON.stringify({ title: cleanTitle }),
                });
                const data = await res.json();
                if (!res.ok) throw new Error(errorMessageFrom(data, `Umbenennen fehlgeschlagen: ${res.status}`));
                renderSession(data);
                await loadSessions();
                setStatus("Session umbenannt.");
              } catch (error) {
                setStatus(error.message, true);
              }
            }

            async function deleteSession() {
              if (!currentSessionId) return;
              try {
                const res = await fetch(`/api/admin/sessions/${currentSessionId}`, {
                  method: "DELETE",
                  headers: headers(),
                });
                if (!res.ok) throw new Error(`Loeschen fehlgeschlagen: ${res.status}`);
                currentSessionId = null;
                sessionTitle.textContent = "Keine Session";
                sessionMeta.textContent = "Lege links eine Session an oder lade eine bestehende.";
                renderMessages([]);
                await loadSessions();
                setStatus("Session geloescht.");
              } catch (error) {
                setStatus(error.message, true);
              }
            }

            async function sendMessage() {
              if (!currentSessionId) {
                await createSession();
                if (!currentSessionId) return;
              }
              const message = promptInput.value.trim();
              if (!message) return;
              const mode = modeInput.value;
              promptInput.value = "";

              if (streamingInput.value === "true") {
                await streamMessage(message, mode);
                return;
              }

              try {
                setStatus("Request laeuft...");
                const res = await fetch(`/api/admin/sessions/${currentSessionId}/chat`, {
                  method: "POST",
                  headers: headers(),
                  body: JSON.stringify({
                    message,
                    mode,
                    document_ids: selectedDocumentIds(),
                    include_home_assistant: includeHomeAssistantInput.value === "true",
                  }),
                });
                const data = await res.json();
                if (!res.ok) throw new Error(errorMessageFrom(data, `Chat fehlgeschlagen: ${res.status}`));
                renderSession(data.session);
                await loadSessions();
                const tps = data.assistant_message && typeof data.assistant_message.tokens_per_second === "number"
                  ? ` | ${data.assistant_message.tokens_per_second.toFixed(2)} t/s`
                  : "";
                setStatus(`Antwort erhalten via ${data.resolved_model} (${data.route_reason})${tps}.`);
              } catch (error) {
                setStatus(error.message, true);
              }
            }

            async function streamMessage(message, mode) {
              const userNode = document.createElement("div");
              userNode.className = "message user";
              userNode.innerHTML = `
                <div class="message-header">
                  <div class="message-role">user</div>
                </div>
                <div class="message-body">${renderMessageContent(message)}</div>
              `;
              messagesNode.appendChild(userNode);

              const assistantNode = document.createElement("div");
              assistantNode.className = "message assistant";
              const assistantBody = document.createElement("div");
              assistantBody.className = "message-body";
              let assistantText = "";
              assistantNode.innerHTML = '<div class="message-header"><div class="message-role">assistant</div></div>';
              assistantNode.appendChild(assistantBody);
              messagesNode.appendChild(assistantNode);
              scrollMessagesToBottom();

              try {
                setStatus("Streaming laeuft...");
                const res = await fetch(`/api/admin/sessions/${currentSessionId}/chat/stream`, {
                  method: "POST",
                  headers: headers(),
                  body: JSON.stringify({
                    message,
                    mode,
                    document_ids: selectedDocumentIds(),
                    include_home_assistant: includeHomeAssistantInput.value === "true",
                  }),
                });
                if (!res.ok) {
                  const data = await res.json();
                  throw new Error(errorMessageFrom(data, `Streaming fehlgeschlagen: ${res.status}`));
                }

                const reader = res.body.getReader();
                const decoder = new TextDecoder();
                let buffer = "";
                let streamTps = null;

                while (true) {
                  const { value, done } = await reader.read();
                  if (done) break;
                  buffer += decoder.decode(value, { stream: true });
                  const parts = buffer.split("\\n\\n");
                  buffer = parts.pop() || "";
                  for (const part of parts) {
                    if (!part.startsWith("data: ")) continue;
                    const payload = part.slice(6);
                    if (payload === "[DONE]") continue;
                    const data = JSON.parse(payload);
                    if (data.error && data.error.message) {
                      assistantText = data.error.message;
                      assistantBody.innerHTML = renderMessageContent(assistantText);
                      throw new Error(data.error.message);
                    }
                    if (data.timings && typeof data.timings.predicted_per_second === "number") {
                      streamTps = data.timings.predicted_per_second;
                    }
                    let delta = "";
                    if (data.choices && data.choices[0] && data.choices[0].delta && typeof data.choices[0].delta.content === "string") {
                      delta = data.choices[0].delta.content;
                    }
                    if (delta) {
                      assistantText += delta;
                      assistantBody.innerHTML = renderMessageContent(assistantText);
                      scrollMessagesToBottom();
                    }
                  }
                }

                await openSession(currentSessionId);
                await loadSessions();
                const tps = typeof streamTps === "number" ? ` | ${streamTps.toFixed(2)} t/s` : "";
                setStatus(`Streaming abgeschlossen${tps}.`);
              } catch (error) {
                setStatus(error.message, true);
              }
            }

            loadSessions();
            loadAvailableDocuments();
          </script>
        </body>
        </html>
        """
    )
