from fastapi import APIRouter, Depends

from app.auth import require_bearer_token
from app.config import get_settings
from app.schemas.models import ModelCard, ModelListResponse


router = APIRouter(tags=["models"])


@router.get("/v1/models", response_model=ModelListResponse, dependencies=[Depends(require_bearer_token)])
async def list_models() -> ModelListResponse:
    settings = get_settings()
    model = ModelCard(id=settings.public_model_name)
    return ModelListResponse(data=[model])
