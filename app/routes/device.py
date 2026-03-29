import json
from types import SimpleNamespace

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.api_errors import error_response
from app.auth import require_device_token
from app.config import get_settings
from app.context_guard import ContextGuardError
from app.routes.admin_chat import (
    _extract_assistant_text,
    _home_assistant_error_code,
    _prepare_admin_backend_payload,
    _try_handle_home_assistant_action,
    _try_handle_home_assistant_intent_stage,
    _try_handle_home_assistant_lookup,
)
from app.services.home_assistant import HomeAssistantConfigError, HomeAssistantRequestError
from app.services.llamacpp_client import LlamaCppClient, LlamaCppError, LlamaCppTimeoutError
from app.services.session_memory import get_session_store
from app.services.storage_library import upload_document


router = APIRouter(tags=["device"])

DEVICE_RESPONSE_STYLE_SYSTEM = (
    "Antwortstil fuer Kai (Voice Device): "
    "antworte immer so kurz wie moeglich, maximal 1-2 kurze Saetze. "
    "Keine Beispiele, kein Code, keine Listen, keine langen Erklaerungen, "
    "keine Vorrede. Nur die direkte Antwort oder Aktion."
)


class DeviceAskRequest(BaseModel):
    message: str = Field(..., min_length=1)
    mode: str = "auto"
    session_id: str | None = None
    max_tokens: int | None = None
    document_ids: list[str] = []


async def _run_device_message(
    *,
    request: Request,
    session,
    message: str,
    mode: str,
    max_tokens: int | None,
    document_ids: list[str],
) -> JSONResponse:
    # Device text requests and snapshot events share the same routing/context path
    # so Pi, camera nodes and browser chat stay behaviorally consistent.
    settings = get_settings()
    store = get_session_store(settings)
    client = LlamaCppClient(settings)
    home_assistant_response = await _try_handle_device_home_assistant(
        request=request,
        settings=settings,
        store=store,
        session=session,
        message=message,
        mode=mode,
    )
    if home_assistant_response is not None:
        return home_assistant_response

    decision, backend_payload = await _prepare_admin_backend_payload(
        settings,
        session,
        SimpleNamespace(
            message=message,
            mode=mode,
            temperature=None,
            max_tokens=max_tokens,
            include_home_assistant=False,
            document_ids=document_ids,
        ),
    )
    # Keep Pi/Kai voice responses intentionally short without changing web/admin chat behavior.
    messages = backend_payload.get("messages")
    if isinstance(messages, list):
        backend_payload["messages"] = [
            {"role": "system", "content": DEVICE_RESPONSE_STYLE_SYSTEM},
            *messages,
        ]
    default_limit = max(64, min(settings.default_max_tokens, 220))
    backend_payload["max_tokens"] = min(int(backend_payload.get("max_tokens") or default_limit), 220)
    await store.add_message(session.id, "user", message)
    await store.update_route(session.id, decision.resolved_model, decision.reason, mode or session.mode)
    request.state.backend_called = True
    response_payload = await client.create_chat_completion(backend_payload, base_url=decision.target_base_url)
    assistant_text = _extract_assistant_text(response_payload)
    await store.add_message(session.id, "assistant", assistant_text, model_used=decision.resolved_model)
    return JSONResponse(
        {
            "session_id": session.id,
            "text": assistant_text,
            "resolved_model": decision.resolved_model,
            "route_reason": decision.reason,
        }
    )


async def _try_handle_device_home_assistant(
    *,
    request: Request,
    settings,
    store,
    session,
    message: str,
    mode: str,
) -> JSONResponse | None:
    intent_result = await _try_handle_home_assistant_intent_stage(settings, message, session=session)
    if intent_result is not None:
        return await _build_device_home_assistant_response(
            request=request,
            store=store,
            session=session,
            message=message,
            mode=mode,
            result=intent_result,
            mark_backend_called=bool(intent_result.get("backend_called")),
        )

    action_result = await _try_handle_home_assistant_action(settings, message, session=session)
    if action_result is not None:
        return await _build_device_home_assistant_response(
            request=request,
            store=store,
            session=session,
            message=message,
            mode=mode,
            result=action_result,
            mark_backend_called=True,
        )

    lookup_result = await _try_handle_home_assistant_lookup(settings, message)
    if lookup_result is not None:
        return await _build_device_home_assistant_response(
            request=request,
            store=store,
            session=session,
            message=message,
            mode=mode,
            result=lookup_result,
            mark_backend_called=bool(lookup_result.get("backend_called")),
        )

    return None


async def _build_device_home_assistant_response(
    *,
    request: Request,
    store,
    session,
    message: str,
    mode: str,
    result: dict[str, str],
    mark_backend_called: bool,
) -> JSONResponse:
    raw_text = result["assistant_text"]
    spoken_text = _device_voice_text_for_home_assistant(raw_text, result["route_reason"])
    await store.add_message(session.id, "user", message)
    await store.update_route(session.id, "home_assistant", result["route_reason"], mode or session.mode)
    if mark_backend_called:
        request.state.backend_called = True
    await store.add_message(
        session.id,
        "assistant",
        raw_text,
        model_used="home_assistant",
    )
    return JSONResponse(
        {
            "session_id": session.id,
            "text": spoken_text,
            "raw_text": raw_text,
            "resolved_model": "home_assistant",
            "route_reason": result["route_reason"],
        }
    )


def _device_voice_text_for_home_assistant(raw_text: str, route_reason: str) -> str:
    text = (raw_text or "").strip()
    reason = (route_reason or "").strip().lower()
    if not text:
        return "Erledigt."

    # Voice devices need short, natural confirmation. Keep technical details
    # in `raw_text` for UI/debug, but speak concise feedback.
    if "lookup" in reason:
        return "Ich habe passende Home-Assistant-Entities gefunden."
    if "action" in reason:
        return "Okay, erledigt."
    if text.lower().startswith("home assistant ausgefuehrt"):
        return "Okay, erledigt."
    return text


@router.post("/api/device/ask", dependencies=[Depends(require_device_token)])
async def device_ask(payload: DeviceAskRequest, request: Request) -> JSONResponse:
    settings = get_settings()
    store = get_session_store(settings)

    if payload.session_id:
        session = await store.get_session(payload.session_id)
    else:
        session = await store.create_session(title="Pi Session", mode=payload.mode)

    if session is None:
        return error_response(
            request_id=request.state.request_id,
            status_code=status.HTTP_404_NOT_FOUND,
            message="Session not found.",
            error_type="not_found_error",
            code="session_not_found",
        )

    try:
        return await _run_device_message(
            request=request,
            session=session,
            message=payload.message,
            mode=payload.mode,
            max_tokens=payload.max_tokens,
            document_ids=payload.document_ids,
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


@router.post("/api/device/vision/event", dependencies=[Depends(require_device_token)])
async def device_vision_event(
    request: Request,
    CAMERA_NAME: str = Form(default=""),
    TRIGGER_TYPE: str = Form(default="snapshot"),
    MESSAGE: str = Form(default=""),
    MODE: str = Form(default="auto"),
    SESSION_ID: str = Form(default=""),
    MAX_TOKENS: int | None = Form(default=None),
    STORAGE_PROFILE_ID: str = Form(default=""),
    DOCUMENT_TITLE: str = Form(default=""),
    DOCUMENT_TAGS: str = Form(default=""),
    IMAGE_FILE: UploadFile = File(...),
) -> JSONResponse:
    # Snapshot events stay storage-first: the image is always persisted, and
    # only then optionally fed back into the normal device chat flow as context.
    settings = get_settings()
    store = get_session_store(settings)
    session = None
    if SESSION_ID:
        session = await store.get_session(SESSION_ID)
    if session is None:
        session_title = f"Vision Event {CAMERA_NAME.strip() or 'Pi'}"
        session = await store.create_session(title=session_title, mode=MODE or "auto")

    try:
        auto_tags = [item for item in [DOCUMENT_TAGS.strip(), "vision-event", f"trigger:{TRIGGER_TYPE.strip() or 'snapshot'}"] if item]
        if CAMERA_NAME.strip():
            auto_tags.append(f"camera:{CAMERA_NAME.strip()}")
        title = DOCUMENT_TITLE.strip() or f"{CAMERA_NAME.strip() or 'Kamera'} {TRIGGER_TYPE.strip() or 'snapshot'}"
        document = await upload_document(
            settings=settings,
            file=IMAGE_FILE,
            storage_profile_id=STORAGE_PROFILE_ID or None,
            title=title,
            tags=",".join(auto_tags),
        )
        response_payload = {
            "session_id": session.id,
            "document": document,
            "analysis_text": str(document.get("extracted_text") or document.get("text_excerpt") or "").strip(),
        }
        if not MESSAGE.strip():
            return JSONResponse(response_payload)

        chat_response = await _run_device_message(
            request=request,
            session=session,
            message=MESSAGE.strip(),
            mode=MODE or session.mode,
            max_tokens=MAX_TOKENS,
            document_ids=[str(document["id"])],
        )
        payload = dict(chat_response.body and json.loads(chat_response.body.decode("utf-8")) or {})
        payload["document"] = document
        payload["analysis_text"] = response_payload["analysis_text"]
        return JSONResponse(payload)
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
    except Exception as exc:
        return error_response(
            request_id=request.state.request_id,
            status_code=status.HTTP_400_BAD_REQUEST,
            message=str(exc),
            error_type="invalid_request_error",
            code="vision_event_failed",
        )
