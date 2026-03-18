from fastapi import APIRouter, Depends

from app.auth import require_admin_api_auth
from app.metrics import metrics


router = APIRouter(tags=["metrics"])


@router.get("/internal/metrics", dependencies=[Depends(require_admin_api_auth)])
async def get_metrics() -> dict:
    return metrics.snapshot()
