"""Reachability checks for the two stores, shared by /ready and /v1/status."""

import asyncio
from dataclasses import dataclass

from fastapi import Request
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

# Well under the OnlineCampus API's Theta:TimeoutSeconds (10), so a store that is
# down is reported as down instead of making the whole status call time out.
PROBE_TIMEOUT_SECONDS = 3


@dataclass(frozen=True)
class ProbeResult:
    ok: bool
    detail: str


def _reason(exc: BaseException) -> str:
    if isinstance(exc, TimeoutError):
        return f"no answer within {PROBE_TIMEOUT_SECONDS}s"
    lines = str(exc).strip().splitlines()
    return (lines[0] if lines else type(exc).__name__)[:160]


class Probes:
    def __init__(self, engine: AsyncEngine, redis: Redis):
        self._engine = engine
        self._redis = redis

    async def database(self) -> ProbeResult:
        async def run() -> str:
            async with self._engine.connect() as conn:
                return (await conn.execute(text("select current_setting('server_version')"))).scalar_one()

        try:
            version = await asyncio.wait_for(run(), PROBE_TIMEOUT_SECONDS)
            return ProbeResult(True, f"PostgreSQL {version}")
        except Exception as exc:
            return ProbeResult(False, _reason(exc))

    async def redis(self) -> ProbeResult:
        async def run() -> str:
            await self._redis.ping()
            return (await self._redis.info("server")).get("redis_version", "?")

        try:
            version = await asyncio.wait_for(run(), PROBE_TIMEOUT_SECONDS)
            return ProbeResult(True, f"Redis {version}")
        except Exception as exc:
            return ProbeResult(False, _reason(exc))

    async def both(self) -> tuple[ProbeResult, ProbeResult]:
        """Database and Redis checked concurrently, so the worst case is one timeout, not two."""
        database, redis = await asyncio.gather(self.database(), self.redis())
        return database, redis


def get_probes(request: Request) -> Probes:
    return Probes(request.app.state.engine, request.app.state.redis)
