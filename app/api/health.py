"""Liveness and readiness. Unauthenticated: they reveal no data, only whether it can work."""

from typing import Annotated

from fastapi import APIRouter, Depends, Response, status

from app.core.probes import Probes, get_probes

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict:
    """Is the process alive. Checks nothing else, so a database outage never triggers a restart loop."""
    return {"status": "ok"}


@router.get("/ready")
async def ready(response: Response, probes: Annotated[Probes, Depends(get_probes)]) -> dict:
    """Can the service do its work: 503 naming the broken store when it cannot."""
    database, redis = await probes.both()
    is_ready = database.ok and redis.ok
    if not is_ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {
        "ready": is_ready,
        "checks": {
            "database": {"ok": database.ok, "detail": database.detail},
            "redis": {"ok": redis.ok, "detail": redis.detail},
        },
    }
