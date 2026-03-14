from fastapi import APIRouter

from app.config import get_settings


router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, str]:
    settings = get_settings()
    return {
        "status": "ok",
        "service": "llm-gateway",
        "public_model": settings.public_model_name,
    }
