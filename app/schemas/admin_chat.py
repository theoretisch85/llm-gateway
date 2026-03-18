from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


ChatMode = Literal["auto", "fast", "deep"]


class AdminSessionCreateRequest(BaseModel):
    title: str | None = None
    mode: ChatMode = "auto"


class AdminChatRequest(BaseModel):
    message: str = Field(..., min_length=1)
    mode: ChatMode | None = None
    temperature: float | None = None
    max_tokens: int | None = None


class AdminChatMessageResponse(BaseModel):
    id: str
    role: str
    content: str
    model_used: str | None = None
    created_at: datetime


class AdminSessionResponse(BaseModel):
    id: str
    title: str
    mode: ChatMode
    resolved_model: str | None = None
    route_reason: str | None = None
    summary: str | None = None
    created_at: datetime
    updated_at: datetime
    messages: list[AdminChatMessageResponse] = []


class AdminChatResponse(BaseModel):
    session: AdminSessionResponse
    assistant_message: AdminChatMessageResponse
    resolved_model: str
    route_reason: str
