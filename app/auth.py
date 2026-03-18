import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
from typing import Any

from fastapi import Cookie, Header, HTTPException, Request
from fastapi.responses import RedirectResponse

from app.api_errors import unauthorized_error
from app.config import Settings, get_settings


logger = logging.getLogger(__name__)

ADMIN_SESSION_COOKIE = "llm_gateway_admin"


def _verify_token(candidate: str, expected: str) -> bool:
    return secrets.compare_digest(candidate, expected)


def _effective_admin_password(settings: Settings) -> str:
    return settings.admin_password or settings.api_bearer_token


def _effective_admin_session_secret(settings: Settings) -> str:
    return settings.admin_session_secret or settings.api_bearer_token


def create_admin_session_token(settings: Settings, username: str) -> str:
    payload = {
        "username": username,
        "exp": int(time.time()) + settings.admin_session_ttl_hours * 3600,
    }
    payload_raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    payload_b64 = base64.urlsafe_b64encode(payload_raw).decode("utf-8").rstrip("=")
    signature = hmac.new(
        _effective_admin_session_secret(settings).encode("utf-8"),
        payload_b64.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{payload_b64}.{signature}"


def parse_admin_session_token(settings: Settings, token: str | None) -> str | None:
    if not token or "." not in token:
        return None

    payload_b64, signature = token.rsplit(".", 1)
    expected_signature = hmac.new(
        _effective_admin_session_secret(settings).encode("utf-8"),
        payload_b64.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if not secrets.compare_digest(signature, expected_signature):
        return None

    padding = "=" * (-len(payload_b64) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode((payload_b64 + padding).encode("utf-8")))
    except (ValueError, json.JSONDecodeError):
        return None

    if not isinstance(payload, dict):
        return None
    if payload.get("exp", 0) < int(time.time()):
        return None
    username = payload.get("username")
    if not isinstance(username, str) or not username:
        return None
    return username


def attach_admin_session_cookie(response: RedirectResponse, settings: Settings, username: str) -> None:
    response.set_cookie(
        key=ADMIN_SESSION_COOKIE,
        value=create_admin_session_token(settings, username),
        max_age=settings.admin_session_ttl_hours * 3600,
        httponly=True,
        samesite="lax",
        secure=settings.admin_cookie_secure,
        path="/",
    )


def clear_admin_session_cookie(response: RedirectResponse) -> None:
    response.delete_cookie(key=ADMIN_SESSION_COOKIE, path="/")


def get_admin_session_username(request: Request) -> str | None:
    settings = get_settings()
    cookie_value = request.cookies.get(ADMIN_SESSION_COOKIE)
    return parse_admin_session_token(settings, cookie_value)


def require_admin_login_or_redirect(request: Request) -> str:
    username = get_admin_session_username(request)
    if username:
        return username
    return_url = request.url.path
    if request.url.query:
        return_url = f"{return_url}?{request.url.query}"
    raise HTTPException(
        status_code=303,
        detail="Login required.",
        headers={"Location": f"/admin/login?next={return_url}"},
    )


async def require_bearer_token(authorization: str | None = Header(default=None)) -> None:
    settings = get_settings()

    if not authorization:
        logger.warning("auth failed reason=missing_authorization_header")
        raise unauthorized_error("Missing Authorization header.")

    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        logger.warning("auth failed reason=invalid_authorization_format")
        raise unauthorized_error("Authorization header must use Bearer token format.")

    if not _verify_token(token, settings.api_bearer_token):
        logger.warning("auth failed reason=invalid_token")
        raise unauthorized_error("Invalid bearer token.")


async def require_admin_api_auth(
    request: Request,
    authorization: str | None = Header(default=None),
    admin_session: str | None = Cookie(default=None, alias=ADMIN_SESSION_COOKIE),
) -> str:
    settings = get_settings()

    username = parse_admin_session_token(settings, admin_session)
    if username:
        return username

    if authorization:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() == "bearer" and token and _verify_token(token, settings.api_bearer_token):
            return "bearer"

    logger.warning("admin auth failed reason=missing_or_invalid_credentials path=%s", request.url.path)
    raise unauthorized_error("Admin authentication required.")


async def require_device_token(
    authorization: str | None = Header(default=None),
    x_device_token: str | None = Header(default=None),
) -> None:
    settings = get_settings()
    expected = settings.device_shared_token or settings.api_bearer_token

    if x_device_token and _verify_token(x_device_token, expected):
        return

    if authorization:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() == "bearer" and token and _verify_token(token, expected):
            return

    raise unauthorized_error("Invalid device token.")


def validate_admin_credentials(username: str, password: str, settings: Settings | None = None) -> bool:
    active_settings = settings or get_settings()
    if username != active_settings.admin_username:
        return False
    return _verify_token(password, _effective_admin_password(active_settings))
