from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse

from app.api_errors import error_response
from app.auth import require_admin_api_auth
from app.config import get_settings
from app.services.gmail import GmailClient, GmailConfigError, GmailRequestError, classify_gmail_messages
from app.services.llamacpp_client import LlamaCppError, LlamaCppTimeoutError


router = APIRouter(tags=["mail"])


@router.get("/api/admin/mail/status", dependencies=[Depends(require_admin_api_auth)], response_model=None)
async def mail_status(request: Request) -> JSONResponse:
    settings = get_settings()
    client = GmailClient(settings)
    try:
        payload = await client.get_status(fallback_redirect_uri=str(request.url_for("gmail_oauth_callback")))
        return JSONResponse(payload)
    except GmailConfigError as exc:
        return _gmail_error(request, exc)


@router.get("/api/admin/mail/oauth/start", dependencies=[Depends(require_admin_api_auth)], response_model=None)
async def mail_oauth_start(
    request: Request,
    next: str | None = Query(default="/internal/admin?tab=settings"),
) -> RedirectResponse | JSONResponse:
    settings = get_settings()
    client = GmailClient(settings)
    try:
        payload = await client.start_oauth(
            fallback_redirect_uri=str(request.url_for("gmail_oauth_callback")),
            next_url=next,
        )
        return RedirectResponse(url=str(payload["authorization_url"]), status_code=303)
    except GmailConfigError as exc:
        return _gmail_error(request, exc)


@router.get("/api/admin/mail/oauth/callback", name="gmail_oauth_callback", response_model=None)
async def mail_oauth_callback(
    request: Request,
    auth_subject: str = Depends(require_admin_api_auth),
) -> RedirectResponse | JSONResponse:
    _ = auth_subject
    settings = get_settings()
    client = GmailClient(settings)
    try:
        payload = await client.finish_oauth(authorization_response=str(request.url))
        return _settings_redirect(str(payload.get("message") or "Gmail verbunden."), error=False, next_url=str(payload.get("next_url") or ""))
    except GmailConfigError as exc:
        return _settings_redirect(exc.message, error=True)
    except GmailRequestError as exc:
        return _settings_redirect(exc.message, error=True)


@router.post("/api/admin/mail/disconnect", dependencies=[Depends(require_admin_api_auth)], response_model=None)
async def mail_disconnect(request: Request) -> JSONResponse | RedirectResponse:
    settings = get_settings()
    client = GmailClient(settings)
    try:
        return JSONResponse(await client.disconnect())
    except GmailConfigError as exc:
        return _gmail_error(request, exc)


@router.post("/internal/admin/mail/credentials/upload", response_model=None)
async def mail_upload_credentials(
    request: Request,
    credentials_file: UploadFile = File(...),
    next_url: str = Form(default="/internal/admin?tab=mail"),
    auth_subject: str = Depends(require_admin_api_auth),
) -> RedirectResponse:
    _ = auth_subject
    settings = get_settings()
    target_path = settings.gmail_client_secret_file
    try:
        raw = await credentials_file.read()
        parsed = json.loads(raw.decode("utf-8"))
        if not isinstance(parsed, dict) or not any(key in parsed for key in {"web", "installed"}):
            raise ValueError("Google OAuth JSON muss einen 'web' oder 'installed' Abschnitt enthalten.")
        destination = Path(target_path).expanduser()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(parsed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return _settings_redirect(f"Gmail OAuth Client gespeichert: {destination}", error=False, next_url=next_url)
    except Exception as exc:
        return _settings_redirect(f"Gmail OAuth Upload fehlgeschlagen: {exc}", error=True, next_url=next_url)


@router.get("/api/admin/mail/messages", dependencies=[Depends(require_admin_api_auth)], response_model=None)
async def mail_list_messages(
    request: Request,
    limit: int = Query(default=20, ge=1, le=50),
    q: str | None = Query(default=None),
    label_ids: str | None = Query(default=None),
    include_spam_trash: bool = Query(default=False),
    classify: bool = Query(default=False),
) -> JSONResponse:
    settings = get_settings()
    client = GmailClient(settings)
    parsed_labels = [item.strip() for item in (label_ids or "").split(",") if item.strip()]

    try:
        payload = await client.list_recent(
            limit=limit,
            query=q,
            label_ids=parsed_labels,
            include_spam_trash=include_spam_trash,
        )
        if classify:
            request.state.backend_called = True
            classified = await classify_gmail_messages(settings, payload.get("messages") or [])
            payload["messages"] = classified.get("messages") or []
            payload["classification_model"] = classified.get("model") or settings.effective_fast_model.public_name
        return JSONResponse(payload)
    except (GmailConfigError, GmailRequestError, LlamaCppError, LlamaCppTimeoutError, ValueError) as exc:
        return _gmail_error(request, exc)


@router.get("/api/admin/mail/messages/{message_id}", dependencies=[Depends(require_admin_api_auth)], response_model=None)
async def mail_get_message(message_id: str, request: Request) -> JSONResponse:
    settings = get_settings()
    client = GmailClient(settings)
    try:
        return JSONResponse(await client.get_message(message_id))
    except (GmailConfigError, GmailRequestError, ValueError) as exc:
        return _gmail_error(request, exc)


def _gmail_error(request: Request, exc: Exception) -> JSONResponse:
    if isinstance(exc, GmailConfigError):
        return error_response(
            request_id=request.state.request_id,
            status_code=exc.status_code,
            message=exc.message,
            error_type="service_unavailable",
            code=exc.code,
            headers={"X-Upstream-Service": GmailClient.upstream_name},
        )

    if isinstance(exc, GmailRequestError):
        return error_response(
            request_id=request.state.request_id,
            status_code=exc.status_code,
            message=exc.message,
            error_type="upstream_error",
            code=exc.code,
            headers={"X-Upstream-Service": GmailClient.upstream_name},
        )

    if isinstance(exc, LlamaCppTimeoutError):
        return error_response(
            request_id=request.state.request_id,
            status_code=504,
            message=exc.message,
            error_type="gateway_timeout",
            code=exc.code or "gmail_classification_timeout",
            headers={"X-Upstream-Service": GmailClient.upstream_name},
        )

    if isinstance(exc, LlamaCppError):
        return error_response(
            request_id=request.state.request_id,
            status_code=exc.status_code,
            message=exc.message,
            error_type="upstream_error",
            code=exc.code or "gmail_classification_failed",
            headers={"X-Upstream-Service": GmailClient.upstream_name},
        )

    return error_response(
        request_id=request.state.request_id,
        status_code=400,
        message=str(exc),
        error_type="invalid_request_error",
        code="gmail_invalid_request",
        headers={"X-Upstream-Service": GmailClient.upstream_name},
    )


def _settings_redirect(message: str, *, error: bool, next_url: str = "") -> RedirectResponse:
    target = next_url.strip() or "/internal/admin?tab=settings"
    joiner = "&" if "?" in target else "?"
    params = {"settings_message": message}
    if error:
        params["settings_error"] = "1"
    return RedirectResponse(url=f"{target}{joiner}{urlencode(params)}", status_code=303)
