import logging
import secrets

from fastapi import Header

from app.api_errors import unauthorized_error
from app.config import get_settings


logger = logging.getLogger(__name__)


async def require_bearer_token(authorization: str | None = Header(default=None)) -> None:
    settings = get_settings()

    if not authorization:
        logger.warning("auth failed reason=missing_authorization_header")
        raise unauthorized_error("Missing Authorization header.")

    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        logger.warning("auth failed reason=invalid_authorization_format")
        raise unauthorized_error("Authorization header must use Bearer token format.")

    if not secrets.compare_digest(token, settings.api_bearer_token):
        logger.warning("auth failed reason=invalid_token")
        raise unauthorized_error("Invalid bearer token.")
