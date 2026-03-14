from typing import Any

from fastapi import HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


def error_payload(
    request_id: str,
    message: str,
    error_type: str,
    code: str | None = None,
) -> dict[str, dict[str, str | None]]:
    return {
        "error": {
            "message": message,
            "type": error_type,
            "code": code,
            "request_id": request_id,
        }
    }


def error_response(
    request_id: str,
    status_code: int,
    message: str,
    error_type: str,
    code: str | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    response = JSONResponse(
        status_code=status_code,
        content=error_payload(
            request_id=request_id,
            message=message,
            error_type=error_type,
            code=code,
        ),
    )
    if headers:
        for key, value in headers.items():
            response.headers[key] = value
    response.headers["X-Request-ID"] = request_id
    return response


def get_request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "-")


def unauthorized_error(message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={
            "message": message,
            "type": "authentication_error",
            "code": "invalid_api_key",
        },
        headers={"WWW-Authenticate": "Bearer"},
    )


def normalize_http_exception(request: Request, exc: HTTPException) -> JSONResponse:
    request_id = get_request_id(request)
    detail: Any = exc.detail

    if isinstance(detail, dict) and {"message", "type", "code"} <= set(detail.keys()):
        return error_response(
            request_id=request_id,
            status_code=exc.status_code,
            message=str(detail["message"]),
            error_type=str(detail["type"]),
            code=str(detail["code"]) if detail["code"] is not None else None,
            headers=exc.headers,
        )

    if exc.status_code == status.HTTP_404_NOT_FOUND:
        return error_response(
            request_id=request_id,
            status_code=exc.status_code,
            message="Route not found.",
            error_type="not_found_error",
            code="route_not_found",
            headers=exc.headers,
        )

    return error_response(
        request_id=request_id,
        status_code=exc.status_code,
        message=str(detail),
        error_type="http_error",
        code=f"http_{exc.status_code}",
        headers=exc.headers,
    )


def normalize_validation_exception(request: Request, exc: RequestValidationError) -> JSONResponse:
    request_id = get_request_id(request)
    first_error = exc.errors()[0] if exc.errors() else {}
    location = ".".join(str(part) for part in first_error.get("loc", []))
    message = first_error.get("msg", "Request validation failed.")
    if location:
        message = f"{location}: {message}"

    return error_response(
        request_id=request_id,
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        message=message,
        error_type="validation_error",
        code="invalid_request",
    )
