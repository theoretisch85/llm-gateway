from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


PipelineVerdict = Literal[
    "ok",
    "needs_revision",
    "unsafe",
    "review_timeout",
    "review_parse_error",
    "review_error",
]
RiskLevel = Literal["low", "medium", "high", "unknown"]


class ReviewedChatRequest(BaseModel):
    message: str = Field(..., min_length=1)
    worker_model: str = "fast"
    reviewer_model: str = "reviewer"
    include_review_meta: bool = True


class ReviewMeta(BaseModel):
    verdict: PipelineVerdict
    issues: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    risk_level: RiskLevel = "unknown"


class ReviewedChatResponse(BaseModel):
    answer: str
    review_meta: ReviewMeta | None = None


class ReviewerJson(BaseModel):
    verdict: Literal["ok", "needs_revision", "unsafe"]
    issues: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    risk_level: Literal["low", "medium", "high"]
    final_answer: str
