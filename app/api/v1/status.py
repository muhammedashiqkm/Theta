"""The integration handshake: proves token, caller context and both stores in one call."""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.config import Settings, get_settings
from app.core.probes import Probes, get_probes
from app.core.security import Caller, Role, get_caller

router = APIRouter(prefix="/v1", tags=["status"])


class CheckOut(BaseModel):
    ok: bool
    detail: str


class CallerOut(BaseModel):
    institution_id: int
    user_id: int
    role: Role


class StatusOut(BaseModel):
    service: str
    version: str
    environment: str
    server_time: datetime
    database: CheckOut
    redis: CheckOut
    caller: CallerOut


@router.get("/status", response_model=StatusOut)
async def service_status(
    caller: Annotated[Caller, Depends(get_caller)],
    probes: Annotated[Probes, Depends(get_probes)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> StatusOut:
    database, redis = await probes.both()
    return StatusOut(
        service=settings.app_name,
        version=settings.app_version,
        environment=settings.environment,
        server_time=datetime.now(UTC),
        database=CheckOut(ok=database.ok, detail=database.detail),
        redis=CheckOut(ok=redis.ok, detail=redis.detail),
        caller=CallerOut(institution_id=caller.institution_id, user_id=caller.user_id, role=caller.role),
    )
