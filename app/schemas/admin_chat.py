from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


ChatMode = Literal["auto", "fast", "deep"]


class AdminSessionCreateRequest(BaseModel):
    title: str | None = None
    mode: ChatMode = "auto"


class AdminSessionRenameRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=160)


class AdminChatRequest(BaseModel):
    message: str = Field(..., min_length=1)
    mode: ChatMode | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    include_home_assistant: bool = False
    document_ids: list[str] = []


class AdminChatMessageResponse(BaseModel):
    id: str
    role: str
    content: str
    model_used: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    tokens_per_second: float | None = None
    created_at: datetime


class AdminSessionResponse(BaseModel):
    id: str
    title: str
    mode: ChatMode
    resolved_model: str | None = None
    route_reason: str | None = None
    summary: str | None = None
    token_estimate: int = 0
    message_count: int = 0
    created_at: datetime
    updated_at: datetime
    messages: list[AdminChatMessageResponse] = []


class AdminMemorySummaryResponse(BaseModel):
    id: str
    session_id: str
    session_title: str
    summary_kind: str
    content: str
    source_message_count: int
    resolved_model: str | None = None
    created_at: datetime


class AdminMemoryOverviewResponse(BaseModel):
    store_mode: str
    persistent: bool
    sessions_count: int
    messages_count: int
    summaries_count: int
    sessions: list[AdminSessionResponse] = []
    summaries: list[AdminMemorySummaryResponse] = []


class AdminChatResponse(BaseModel):
    session: AdminSessionResponse
    assistant_message: AdminChatMessageResponse
    resolved_model: str
    route_reason: str
