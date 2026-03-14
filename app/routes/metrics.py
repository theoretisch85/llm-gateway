from fastapi import APIRouter, Depends

from app.auth import require_bearer_token
from app.metrics import metrics


router = APIRouter(tags=["metrics"])


@router.get("/internal/metrics", dependencies=[Depends(require_bearer_token)])
async def get_metrics() -> dict:
    return metrics.snapshot()
