import logging
import time
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api import all_routers
from app.api_errors import error_response, normalize_http_exception, normalize_validation_exception
from app.config import get_settings
from app.metrics import metrics
from app.request_context import RequestIdFilter, request_id_var


settings = get_settings()
logger = logging.getLogger("llm_gateway")

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s request_id=%(request_id)s %(message)s",
)

for handler in logging.getLogger().handlers:
    handler.addFilter(RequestIdFilter())

app = FastAPI(
    title="llm-gateway",
    version="1.31",
    description="OpenAI-compatible local orchestrator for llama.cpp backends.",
)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    started_at = time.perf_counter()
    request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
    request.state.request_id = request_id
    token = request_id_var.set(request_id)
    backend_called = False
    request.state.backend_called = backend_called
    metrics.record_request(request.url.path)
    logger.info("incoming request method=%s path=%s", request.method, request.url.path)

    try:
        response = await call_next(request)
    except Exception:
        logger.exception("unhandled request error method=%s path=%s", request.method, request.url.path)
        response = error_response(
            request_id=request_id,
            status_code=500,
            message="Internal server error.",
            error_type="internal_server_error",
            code="internal_error",
        )

    duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
    metrics.record_response(response.status_code, duration_ms)
    response.headers["X-Request-ID"] = request_id
    logger.info(
        "completed request method=%s path=%s status=%s backend_called=%s duration_ms=%s",
        request.method,
        request.url.path,
        response.status_code,
        getattr(request.state, "backend_called", False),
        duration_ms,
    )
    request_id_var.reset(token)
    return response


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return normalize_http_exception(request, exc)


@app.exception_handler(StarletteHTTPException)
async def starlette_http_exception_handler(request: Request, exc: StarletteHTTPException):
    return normalize_http_exception(request, exc)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return normalize_validation_exception(request, exc)


for router in all_routers():
    app.include_router(router)
