from dataclasses import dataclass
from typing import Any

from app.schemas.chat import ChatMessage


@dataclass
class ContextGuardResult:
    messages: list[dict[str, Any]]
    estimated_prompt_tokens: int
    dropped_messages: int
    truncated: bool


class ContextGuardError(Exception):
    def __init__(self, message: str, code: str = "context_length_exceeded") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


def fit_messages_to_budget(
    messages: list[ChatMessage],
    max_context_tokens: int,
    response_reserve_tokens: int,
    chars_per_token: float,
) -> ContextGuardResult:
    input_budget = max_context_tokens - response_reserve_tokens
    if input_budget <= 0:
        raise ContextGuardError("Configured context budget is invalid.")

    serialized = [message.model_dump(exclude_none=True) for message in messages]
    system_messages = [message for message in serialized if message.get("role") == "system"]
    non_system_messages = [message for message in serialized if message.get("role") != "system"]

    kept_messages: list[dict[str, Any]] = []
    truncated = False

    for message in system_messages:
        if _estimate_messages_tokens(kept_messages + [message], chars_per_token) <= input_budget:
            kept_messages.append(message)
        else:
            truncated = True

    kept_non_system: list[dict[str, Any]] = []
    for message in reversed(non_system_messages):
        candidate = kept_messages + [message] + list(reversed(kept_non_system))
        if _estimate_messages_tokens(candidate, chars_per_token) <= input_budget:
            kept_non_system.append(message)
        else:
            truncated = True

    kept_messages.extend(reversed(kept_non_system))

    if not kept_messages and serialized:
        kept_messages = [serialized[-1]]
        truncated = True

    estimated_prompt_tokens = _estimate_messages_tokens(kept_messages, chars_per_token)
    if estimated_prompt_tokens <= input_budget:
        return ContextGuardResult(
            messages=kept_messages,
            estimated_prompt_tokens=estimated_prompt_tokens,
            dropped_messages=max(0, len(serialized) - len(kept_messages)),
            truncated=truncated,
        )

    truncated_message = _truncate_last_message_to_budget(
        kept_messages,
        input_budget=input_budget,
        chars_per_token=chars_per_token,
    )
    if truncated_message is None:
        raise ContextGuardError(
            "Request is too large for the configured backend context window, even after dropping old messages."
        )

    estimated_prompt_tokens = _estimate_messages_tokens(truncated_message, chars_per_token)
    return ContextGuardResult(
        messages=truncated_message,
        estimated_prompt_tokens=estimated_prompt_tokens,
        dropped_messages=max(0, len(serialized) - len(truncated_message)),
        truncated=True,
    )


def _truncate_last_message_to_budget(
    messages: list[dict[str, Any]],
    input_budget: int,
    chars_per_token: float,
) -> list[dict[str, Any]] | None:
    result = [dict(message) for message in messages]

    for index in range(len(result) - 1, -1, -1):
        content = result[index].get("content")
        if not isinstance(content, str):
            continue

        while len(content) > 1 and _estimate_messages_tokens(result, chars_per_token) > input_budget:
            overflow_tokens = _estimate_messages_tokens(result, chars_per_token) - input_budget
            overflow_chars = max(16, int(overflow_tokens * chars_per_token))
            keep_chars = max(1, len(content) - overflow_chars)
            content = content[:keep_chars]
            result[index]["content"] = content

        if _estimate_messages_tokens(result, chars_per_token) <= input_budget:
            return result

    return None


def _estimate_messages_tokens(messages: list[dict[str, Any]], chars_per_token: float) -> int:
    total = 0
    for message in messages:
        total += 6
        total += _estimate_content_tokens(message.get("content"), chars_per_token)
        name = message.get("name")
        if isinstance(name, str):
            total += max(1, int(len(name) / chars_per_token))
    return total + 8


def _estimate_content_tokens(content: Any, chars_per_token: float) -> int:
    if isinstance(content, str):
        return max(1, int(len(content) / chars_per_token))

    if isinstance(content, list):
        joined = []
        for item in content:
            if isinstance(item, dict):
                joined.append(str(item))
            else:
                joined.append(str(item))
        return max(1, int(len(" ".join(joined)) / chars_per_token))

    return 1
