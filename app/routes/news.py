from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from app.api_errors import error_response
from app.auth import require_bearer_token
from app.config import get_settings
from app.services.calm_news import CalmNewsClient, CalmNewsConfigError, CalmNewsRequestError


router = APIRouter(tags=["news"])


@router.get("/api/news/latest", dependencies=[Depends(require_bearer_token)], response_model=None)
async def news_get_latest(
    request: Request,
    limit: int | None = Query(default=None, ge=1),
    tone: str | None = Query(default=None),
    source: str | None = Query(default=None),
    min_relevance: int | None = Query(default=None),
    max_stress: int | None = Query(default=None),
) -> JSONResponse:
    params = _news_query_params(
        limit=limit,
        tone=tone,
        source=source,
        min_relevance=min_relevance,
        max_stress=max_stress,
    )
    return await _run_news_call(request, lambda client: client.get_latest(params=params))


@router.get("/api/news/calm", dependencies=[Depends(require_bearer_token)], response_model=None)
async def news_get_calm(
    request: Request,
    limit: int | None = Query(default=None, ge=1),
    tone: str | None = Query(default=None),
    source: str | None = Query(default=None),
    min_relevance: int | None = Query(default=None),
    max_stress: int | None = Query(default=None),
) -> JSONResponse:
    params = _news_query_params(
        limit=limit,
        tone=tone,
        source=source,
        min_relevance=min_relevance,
        max_stress=max_stress,
    )
    return await _run_news_call(request, lambda client: client.get_calm(params=params))


@router.get("/api/news/positive", dependencies=[Depends(require_bearer_token)], response_model=None)
async def news_get_positive(
    request: Request,
    limit: int | None = Query(default=None, ge=1),
    tone: str | None = Query(default=None),
    source: str | None = Query(default=None),
    min_relevance: int | None = Query(default=None),
    max_stress: int | None = Query(default=None),
) -> JSONResponse:
    params = _news_query_params(
        limit=limit,
        tone=tone,
        source=source,
        min_relevance=min_relevance,
        max_stress=max_stress,
    )
    return await _run_news_call(request, lambda client: client.get_positive(params=params))


@router.get("/api/news/relevant", dependencies=[Depends(require_bearer_token)], response_model=None)
async def news_get_relevant(
    request: Request,
    limit: int | None = Query(default=None, ge=1),
    tone: str | None = Query(default=None),
    source: str | None = Query(default=None),
    min_relevance: int | None = Query(default=None),
    max_stress: int | None = Query(default=None),
) -> JSONResponse:
    params = _news_query_params(
        limit=limit,
        tone=tone,
        source=source,
        min_relevance=min_relevance,
        max_stress=max_stress,
    )
    return await _run_news_call(request, lambda client: client.get_relevant(params=params))


@router.get("/api/news/system/status", dependencies=[Depends(require_bearer_token)], response_model=None)
async def news_get_system_status(request: Request) -> JSONResponse:
    return await _run_news_call(request, lambda client: client.get_system_status())


@router.get("/api/news/sources", dependencies=[Depends(require_bearer_token)], response_model=None)
async def news_get_sources(
    request: Request,
    source: str | None = Query(default=None),
) -> JSONResponse:
    params = _news_query_params(source=source, include_filters=False)
    return await _run_news_call(request, lambda client: client.get_sources(params=params))


@router.post("/api/news/ingest/run", dependencies=[Depends(require_bearer_token)], response_model=None)
async def news_trigger_ingest(
    request: Request,
    source: str | None = Query(default=None),
) -> JSONResponse:
    params = _news_query_params(source=source, include_filters=False)
    return await _run_news_call(request, lambda client: client.trigger_ingest(params=params))


@router.get("/api/news/ingest/last", dependencies=[Depends(require_bearer_token)], response_model=None)
async def news_get_last_ingest(
    request: Request,
    source: str | None = Query(default=None),
) -> JSONResponse:
    params = _news_query_params(source=source, include_filters=False)
    return await _run_news_call(request, lambda client: client.get_last_ingest(params=params))


@router.patch("/api/news/sources/{source_id}", dependencies=[Depends(require_bearer_token)], response_model=None)
async def news_set_source_status(
    source_id: str,
    payload: dict[str, Any],
    request: Request,
) -> JSONResponse:
    return await _run_news_call(request, lambda client: client.set_source_status(source_id, payload=payload))


@router.get("/api/news/{article_id}", dependencies=[Depends(require_bearer_token)], response_model=None)
async def news_get_article(
    article_id: str,
    request: Request,
    tone: str | None = Query(default=None),
    source: str | None = Query(default=None),
    min_relevance: int | None = Query(default=None),
    max_stress: int | None = Query(default=None),
) -> JSONResponse:
    params = _news_query_params(
        tone=tone,
        source=source,
        min_relevance=min_relevance,
        max_stress=max_stress,
    )
    return await _run_news_call(request, lambda client: client.get_article(article_id, params=params))


async def _run_news_call(
    request: Request,
    operation: Callable[[CalmNewsClient], Awaitable[dict[str, Any]]],
) -> JSONResponse:
    settings = get_settings()
    client = CalmNewsClient(settings)

    try:
        request.state.backend_called = True
        result = await operation(client)
        return JSONResponse(content=result)
    except CalmNewsConfigError as exc:
        return error_response(
            request_id=request.state.request_id,
            status_code=exc.status_code,
            message=exc.message,
            error_type="service_unavailable",
            code=exc.code,
            headers={"X-Upstream-Service": CalmNewsClient.upstream_name},
        )
    except CalmNewsRequestError as exc:
        headers = {"X-Upstream-Service": CalmNewsClient.upstream_name}
        if exc.upstream_request_id:
            headers["X-Upstream-Request-ID"] = exc.upstream_request_id
        return error_response(
            request_id=request.state.request_id,
            status_code=exc.status_code,
            message=exc.message,
            error_type="upstream_error",
            code=exc.code,
            headers=headers,
        )
    except ValueError as exc:
        return error_response(
            request_id=request.state.request_id,
            status_code=400,
            message=str(exc),
            error_type="invalid_request_error",
            code="news_invalid_arguments",
            headers={"X-Upstream-Service": CalmNewsClient.upstream_name},
        )


def _news_query_params(
    *,
    limit: int | None = None,
    tone: str | None = None,
    source: str | None = None,
    min_relevance: int | None = None,
    max_stress: int | None = None,
    include_filters: bool = True,
) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if limit is not None:
        params["limit"] = limit
    if tone:
        params["tone"] = tone.strip()
    if source:
        params["source"] = source.strip()
    if include_filters and min_relevance is not None:
        params["min_relevance"] = min_relevance
    if include_filters and max_stress is not None:
        params["max_stress"] = max_stress
    return {key: value for key, value in params.items() if value != ""}
