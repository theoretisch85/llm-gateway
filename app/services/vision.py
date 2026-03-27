from __future__ import annotations

import base64
from pathlib import Path

from app.config import Settings
from app.services.llamacpp_client import LlamaCppClient


class VisionConfigError(RuntimeError):
    pass


def _image_media_type(file_name: str, media_type: str | None) -> str:
    if media_type and media_type.startswith("image/"):
        return media_type

    suffix = Path(file_name).suffix.lower()
    if suffix in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if suffix == ".png":
        return "image/png"
    if suffix == ".webp":
        return "image/webp"
    if suffix == ".gif":
        return "image/gif"
    return "application/octet-stream"


def vision_is_configured(settings: Settings) -> bool:
    return bool((settings.vision_model_name or "").strip())


async def analyze_image_bytes(
    settings: Settings,
    *,
    file_name: str,
    media_type: str | None,
    raw_bytes: bytes,
    prompt: str | None = None,
) -> str:
    # Vision stays a separate backend path on purpose so the main text model
    # can remain lightweight while image understanding is swapped independently.
    if not vision_is_configured(settings):
        raise VisionConfigError("VISION_MODEL_NAME ist nicht gesetzt.")

    mime = _image_media_type(file_name, media_type)
    image_b64 = base64.b64encode(raw_bytes).decode("ascii")
    data_url = f"data:{mime};base64,{image_b64}"
    payload = {
        "model": (settings.vision_model_name or "").strip(),
        "stream": False,
        "max_tokens": settings.vision_max_tokens,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": (prompt or settings.vision_prompt).strip()},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ],
    }
    client = LlamaCppClient(settings)
    response = await client.create_chat_completion(payload, base_url=settings.effective_vision_base_url)
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0] if isinstance(choices[0], dict) else {}
    message = first.get("message") if isinstance(first, dict) else {}
    content = message.get("content") if isinstance(message, dict) else ""
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text = str(item.get("text") or "").strip()
                if text:
                    parts.append(text)
        return "\n".join(parts).strip()
    return str(content or "").strip()
