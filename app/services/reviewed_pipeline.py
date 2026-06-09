from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import ValidationError

from app.config import Settings
from app.schemas.reviewed_pipeline import ReviewMeta, ReviewedChatRequest, ReviewedChatResponse, ReviewerJson
from app.services.llamacpp_client import LlamaCppClient, LlamaCppError, LlamaCppTimeoutError


logger = logging.getLogger(__name__)

WORKER_SYSTEM_PROMPT = (
    "Du bist der Worker. Antworte kurz, technisch und umsetzbar. "
    "Gib maximal 5 Schritte. Keine langen Erklärungen. "
    "Keine Shell-Befehle ausführen, nur beschreiben. "
    "Wenn Risiko besteht, nenne es knapp."
)

REVIEWER_SYSTEM_PROMPT = (
    "Du bist der Reviewer. Prüfe die Worker-Antwort kritisch, aber knapp. "
    "Führe nichts aus. Erfinde keine Fakten. "
    "Bewerte Sicherheit, fehlende Punkte und technische Korrektheit. "
    "Füge keine destruktiven Befehle wie kill, rm oder systemctl stop hinzu, wenn der Nutzer nur prüfen oder planen will. "
    "Antworte ausschließlich als JSON. final_answer ist die kurze, verbesserte "
    "Antwort an den Nutzer in derselben Sprache wie die Nutzeraufgabe."
)

MAX_WORKER_REVIEW_CHARS = 3000


class ReviewedPipeline:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = LlamaCppClient(settings)

    async def run(self, request: ReviewedChatRequest) -> ReviewedChatResponse:
        worker_answer = await self._worker_call(request)
        trimmed_worker_answer = self._trim_for_review(worker_answer)

        try:
            reviewer_raw = await self._reviewer_call(
                request=request,
                worker_answer=trimmed_worker_answer,
            )
        except LlamaCppTimeoutError:
            logger.warning("reviewed pipeline reviewer timeout model=%s", request.reviewer_model)
            return self._fallback_response(
                answer=worker_answer,
                include_review_meta=request.include_review_meta,
                meta=ReviewMeta(verdict="review_timeout", risk_level="unknown"),
            )
        except LlamaCppError as exc:
            logger.warning("reviewed pipeline reviewer error model=%s error=%s", request.reviewer_model, exc.message)
            return self._fallback_response(
                answer=worker_answer,
                include_review_meta=request.include_review_meta,
                meta=ReviewMeta(verdict="review_error", issues=[exc.message], risk_level="unknown"),
            )

        reviewer_json = self._parse_reviewer_json(reviewer_raw)
        if reviewer_json is None:
            logger.warning("reviewed pipeline reviewer returned invalid json")
            return self._fallback_response(
                answer=worker_answer,
                include_review_meta=request.include_review_meta,
                meta=ReviewMeta(verdict="review_parse_error", risk_level="unknown"),
            )

        meta = ReviewMeta(
            verdict=reviewer_json.verdict,
            issues=reviewer_json.issues,
            missing=reviewer_json.missing,
            risk_level=reviewer_json.risk_level,
        )
        return ReviewedChatResponse(
            answer=reviewer_json.final_answer or worker_answer,
            review_meta=meta if request.include_review_meta else None,
        )

    async def _worker_call(self, request: ReviewedChatRequest) -> str:
        target = self._settings.resolve_target_for_public_model(request.worker_model)
        payload = {
            "model": target.backend_name,
            "messages": [
                {"role": "system", "content": WORKER_SYSTEM_PROMPT},
                {"role": "user", "content": request.message},
            ],
            "max_tokens": 400,
            "temperature": 0.2,
            "stream": False,
        }
        logger.info(
            "reviewed pipeline worker call public_model=%s backend_model=%s base_url=%s",
            request.worker_model,
            target.backend_name,
            target.base_url,
        )
        response = await self._client.create_chat_completion(payload, base_url=target.base_url)
        return self._extract_message_content(response)

    async def _reviewer_call(self, request: ReviewedChatRequest, worker_answer: str) -> str:
        target = self._settings.resolve_target_for_public_model(request.reviewer_model)
        payload = {
            "model": target.backend_name,
            "messages": [
                {"role": "system", "content": REVIEWER_SYSTEM_PROMPT},
                {"role": "user", "content": self._build_reviewer_user_payload(request.message, worker_answer)},
            ],
            "max_tokens": 500,
            "temperature": 0.1,
            "stream": False,
        }
        logger.info(
            "reviewed pipeline reviewer call public_model=%s backend_model=%s base_url=%s",
            request.reviewer_model,
            target.backend_name,
            target.base_url,
        )
        response = await self._client.create_chat_completion(payload, base_url=target.base_url)
        return self._extract_message_content(response)

    def _build_reviewer_user_payload(self, message: str, worker_answer: str) -> str:
        return (
            "Original user task:\n"
            f"{message}\n\n"
            "Worker answer, possibly trimmed:\n"
            f"{worker_answer}\n\n"
            "Return only JSON:\n"
            "{\n"
            '  "verdict": "ok" | "needs_revision" | "unsafe",\n'
            '  "issues": [],\n'
            '  "missing": [],\n'
            '  "risk_level": "low" | "medium" | "high",\n'
            '  "final_answer": "short improved answer to the original user task, same language as user task"\n'
            "}"
        )

    def _trim_for_review(self, worker_answer: str) -> str:
        if len(worker_answer) <= MAX_WORKER_REVIEW_CHARS:
            return worker_answer
        return worker_answer[:MAX_WORKER_REVIEW_CHARS].rstrip() + "\n[trimmed]"

    def _parse_reviewer_json(self, content: str) -> ReviewerJson | None:
        parsed = self._load_json_object(content)
        if parsed is None:
            return None
        try:
            return ReviewerJson.model_validate(parsed)
        except ValidationError:
            return None

    def _load_json_object(self, content: str) -> dict[str, Any] | None:
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            start = content.find("{")
            end = content.rfind("}")
            if start < 0 or end <= start:
                return None
            try:
                parsed = json.loads(content[start : end + 1])
            except json.JSONDecodeError:
                return None
        return parsed if isinstance(parsed, dict) else None

    def _fallback_response(
        self,
        answer: str,
        include_review_meta: bool,
        meta: ReviewMeta,
    ) -> ReviewedChatResponse:
        return ReviewedChatResponse(
            answer=answer,
            review_meta=meta if include_review_meta else None,
        )

    def _extract_message_content(self, response: dict[str, Any]) -> str:
        choices = response.get("choices")
        if not isinstance(choices, list) or not choices:
            raise LlamaCppError("llama.cpp backend returned no choices.", status_code=502)
        first = choices[0]
        if not isinstance(first, dict):
            raise LlamaCppError("llama.cpp backend returned an invalid choice.", status_code=502)
        message = first.get("message")
        if not isinstance(message, dict):
            raise LlamaCppError("llama.cpp backend returned no message.", status_code=502)
        content = message.get("content")
        if not isinstance(content, str):
            raise LlamaCppError("llama.cpp backend returned no text content.", status_code=502)
        return content
