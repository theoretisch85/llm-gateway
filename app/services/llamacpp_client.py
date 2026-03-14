import json
import logging
import time
from collections.abc import AsyncIterator, Iterable
from typing import Any

import httpx

from app.config import Settings
from app.metrics import metrics


logger = logging.getLogger(__name__)


class LlamaCppError(Exception):
    def __init__(self, message: str, status_code: int = 502, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


class LlamaCppTimeoutError(LlamaCppError):
    pass


class LlamaCppClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._settings.llamacpp_api_key:
            headers["Authorization"] = f"Bearer {self._settings.llamacpp_api_key}"
        return headers

    def _timeout(self) -> httpx.Timeout:
        return httpx.Timeout(self._settings.llamacpp_timeout_seconds)

    async def fetch_models(self) -> tuple[dict, float]:
        metrics.record_backend_call()
        logger.info("backend models request url=%s", self._settings.backend_models_url)
        started_at = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=self._timeout()) as client:
                response = await client.get(
                    self._settings.backend_models_url,
                    headers=self._headers(),
                )
        except httpx.TimeoutException as exc:
            metrics.record_backend_timeout()
            logger.warning("backend models timeout url=%s", self._settings.backend_models_url)
            raise LlamaCppTimeoutError("The upstream llama.cpp backend timed out.") from exc
        except httpx.HTTPError as exc:
            metrics.record_backend_error()
            logger.exception("backend models transport error url=%s", self._settings.backend_models_url)
            raise LlamaCppError("Could not reach llama.cpp backend.", status_code=502) from exc

        logger.info("backend models response status=%s", response.status_code)
        payload = self._parse_json_response(response)
        latency_ms = round((time.perf_counter() - started_at) * 1000, 2)
        return payload, latency_ms

    async def create_chat_completion(self, payload: dict) -> dict:
        metrics.record_backend_call()
        logger.info(
            "backend request url=%s model=%s stream=%s",
            self._settings.backend_chat_completions_url,
            payload.get("model"),
            payload.get("stream", False),
        )
        try:
            async with httpx.AsyncClient(timeout=self._timeout()) as client:
                response = await client.post(
                    self._settings.backend_chat_completions_url,
                    headers=self._headers(),
                    json=payload,
                )
        except httpx.TimeoutException as exc:
            metrics.record_backend_timeout()
            logger.warning("backend timeout url=%s", self._settings.backend_chat_completions_url)
            raise LlamaCppTimeoutError("The upstream llama.cpp backend timed out.") from exc
        except httpx.HTTPError as exc:
            metrics.record_backend_error()
            logger.exception("backend transport error url=%s", self._settings.backend_chat_completions_url)
            raise LlamaCppError("Could not reach llama.cpp backend.", status_code=502) from exc

        logger.info("backend response status=%s", response.status_code)
        return self._parse_json_response(response)

    async def stream_chat_completion(
        self,
        backend_payload: dict,
        public_model_name: str,
        backend_model_name: str,
        request_id: str,
    ) -> AsyncIterator[bytes]:
        metrics.record_backend_call()
        logger.info(
            "backend stream request url=%s model=%s",
            self._settings.backend_chat_completions_url,
            backend_payload.get("model"),
        )
        try:
            async with httpx.AsyncClient(timeout=self._timeout()) as client:
                async with client.stream(
                    "POST",
                    self._settings.backend_chat_completions_url,
                    headers=self._headers(),
                    json=backend_payload,
                ) as response:
                    logger.info(
                        "backend stream response status=%s content_type=%s",
                        response.status_code,
                        response.headers.get("content-type", ""),
                    )
                    if response.status_code >= 400:
                        error_body = await response.aread()
                        error = self._map_error_response(response.status_code, error_body)
                        yield self._encode_error_sse(error=error, request_id=request_id)
                        yield b"data: [DONE]\n\n"
                        return

                    content_type = response.headers.get("content-type", "")
                    if "text/event-stream" in content_type:
                        async for line in response.aiter_lines():
                            if not line:
                                continue
                            yield self._rewrite_sse_line(
                                line=line,
                                public_model_name=public_model_name,
                                backend_model_name=backend_model_name,
                                request_id=request_id,
                            )
                        return

                    logger.warning(
                        "backend stream fallback activated content_type=%s",
                        content_type or "missing",
                    )
                    fallback_response = self._parse_json_bytes(await response.aread())
                    async for chunk in self._fallback_stream_from_json(
                        completion=fallback_response,
                        public_model_name=public_model_name,
                        backend_model_name=backend_model_name,
                        request_id=request_id,
                    ):
                        yield chunk
        except httpx.TimeoutException as exc:
            metrics.record_backend_timeout()
            logger.warning("backend stream timeout url=%s", self._settings.backend_chat_completions_url)
            raise LlamaCppTimeoutError("The upstream llama.cpp backend timed out.") from exc
        except httpx.HTTPError as exc:
            metrics.record_backend_error()
            logger.exception("backend stream transport error url=%s", self._settings.backend_chat_completions_url)
            raise LlamaCppError("Could not reach llama.cpp backend.", status_code=502) from exc

    def _parse_json_response(self, response: httpx.Response) -> dict:
        if response.status_code >= 400:
            raise self._map_error_response(response.status_code, response.content)

        return self._parse_json_bytes(response.content)

    def _parse_json_bytes(self, content: bytes) -> dict:
        try:
            return json.loads(content.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise LlamaCppError(
                "llama.cpp backend returned invalid JSON.",
                status_code=502,
                code="invalid_upstream_json",
            ) from exc
        except UnicodeDecodeError as exc:
            raise LlamaCppError(
                "llama.cpp backend returned non-UTF8 JSON.",
                status_code=502,
                code="invalid_upstream_json_encoding",
            ) from exc

    def _map_error_response(self, status_code: int, content: bytes) -> LlamaCppError:
        metrics.record_backend_error()
        message = "llama.cpp backend returned an error."
        code = None

        try:
            data = json.loads(content.decode("utf-8"))
            if isinstance(data, dict):
                if isinstance(data.get("error"), dict):
                    message = data["error"].get("message", message)
                    code = data["error"].get("code")
                elif isinstance(data.get("message"), str):
                    message = data["message"]
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass

        lowered = message.lower()
        if "exceeds the available context size" in lowered or "context size" in lowered:
            code = code or "context_length_exceeded"

        return LlamaCppError(message=message, status_code=status_code, code=code)

    def _rewrite_sse_line(
        self,
        line: str,
        public_model_name: str,
        backend_model_name: str,
        request_id: str,
    ) -> bytes:
        if not line.startswith("data: "):
            return f"{line}\n\n".encode("utf-8")

        payload = line[6:]
        if payload == "[DONE]":
            return b"data: [DONE]\n\n"

        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            return f"{line}\n\n".encode("utf-8")

        if data.get("model") == backend_model_name:
            data["model"] = public_model_name
        data["request_id"] = request_id

        return f"data: {json.dumps(data, separators=(',', ':'))}\n\n".encode("utf-8")

    async def _fallback_stream_from_json(
        self,
        completion: dict[str, Any],
        public_model_name: str,
        backend_model_name: str,
        request_id: str,
    ) -> AsyncIterator[bytes]:
        model_name = completion.get("model", backend_model_name)
        if model_name == backend_model_name:
            model_name = public_model_name

        base_chunk = {
            "id": completion.get("id", "chatcmpl-fallback"),
            "object": "chat.completion.chunk",
            "created": completion.get("created", 0),
            "model": model_name,
            "request_id": request_id,
        }

        for index, choice in enumerate(self._iter_choices(completion.get("choices", []))):
            role_chunk = {
                **base_chunk,
                "choices": [{"index": index, "delta": {"role": "assistant"}, "finish_reason": None}],
            }
            yield self._encode_sse(role_chunk)

            content = choice.get("message", {}).get("content")
            if isinstance(content, str) and content:
                content_chunk = {
                    **base_chunk,
                    "choices": [{"index": index, "delta": {"content": content}, "finish_reason": None}],
                }
                yield self._encode_sse(content_chunk)

            finish_chunk = {
                **base_chunk,
                "choices": [
                    {
                        "index": index,
                        "delta": {},
                        "finish_reason": choice.get("finish_reason", "stop"),
                    }
                ],
            }
            yield self._encode_sse(finish_chunk)

        yield b"data: [DONE]\n\n"

    def _iter_choices(self, choices: Any) -> Iterable[dict[str, Any]]:
        if isinstance(choices, list):
            for choice in choices:
                if isinstance(choice, dict):
                    yield choice

    def _encode_sse(self, payload: dict[str, Any]) -> bytes:
        return f"data: {json.dumps(payload, separators=(',', ':'))}\n\n".encode("utf-8")

    def _encode_error_sse(self, error: LlamaCppError, request_id: str) -> bytes:
        payload = {
            "error": {
                "message": error.message,
                "type": "upstream_error",
                "code": error.code,
                "request_id": request_id,
            }
        }
        return f"data: {json.dumps(payload, separators=(',', ':'))}\n\n".encode("utf-8")
