import asyncio
import os
from dataclasses import dataclass
from pathlib import Path

import asyncpg
import pytest
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import Settings, get_settings
from app.core.db import create_session_factory, get_session
from app.core.probes import ProbeResult, get_probes
from app.main import create_app

SERVICE_TOKEN = "test-service-token"
ROOT = Path(__file__).resolve().parents[1]

# Integration tests run against a throwaway database next to the dev one, created
# and migrated fresh for every test session. Override for CI with THETA_TEST_*.
TEST_DB = os.environ.get("THETA_TEST_DB", "theta_test")
ADMIN_DSN = os.environ.get("THETA_TEST_ADMIN_DSN", "postgresql://theta:theta_dev_password@127.0.0.1:5433/theta")
TEST_URL = os.environ.get(
    "THETA_TEST_DATABASE_URL", f"postgresql+asyncpg://theta:theta_dev_password@127.0.0.1:5433/{TEST_DB}"
)


class FakeProbes:
    def __init__(self, database_ok: bool, redis_ok: bool):
        self._database_ok = database_ok
        self._redis_ok = redis_ok

    async def database(self) -> ProbeResult:
        return ProbeResult(True, "PostgreSQL 16.4") if self._database_ok else ProbeResult(False, "connection refused")

    async def redis(self) -> ProbeResult:
        return ProbeResult(True, "Redis 7.4.0") if self._redis_ok else ProbeResult(False, "connection refused")

    async def both(self) -> tuple[ProbeResult, ProbeResult]:
        return await self.database(), await self.redis()


def _test_settings(token: str) -> Settings:
    return Settings(service_token=token, _env_file=None)


@pytest.fixture
def make_client():
    """A client against the app with stores faked and the token chosen per test."""

    def factory(configured_token: str = SERVICE_TOKEN, database_ok: bool = True, redis_ok: bool = True):
        app = create_app()
        app.dependency_overrides[get_settings] = lambda: _test_settings(configured_token)
        app.dependency_overrides[get_probes] = lambda: FakeProbes(database_ok, redis_ok)
        return AsyncClient(transport=ASGITransport(app=app), base_url="http://theta.test")

    return factory


@pytest.fixture
def caller_headers() -> dict[str, str]:
    return {
        "X-Theta-Token": SERVICE_TOKEN,
        "X-Theta-Institution-Id": "12",
        "X-Theta-User-Id": "345",
        "X-Theta-Role": "teacher",
    }


# ----- integration ------------------------------------------------------------

@pytest.fixture(scope="session")
def migrated_database() -> str:
    async def recreate() -> None:
        conn = await asyncpg.connect(ADMIN_DSN, timeout=3)
        try:
            await conn.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}" WITH (FORCE)')
            await conn.execute(f'CREATE DATABASE "{TEST_DB}"')
        finally:
            await conn.close()

    try:
        asyncio.run(recreate())
    except (OSError, TimeoutError, asyncpg.PostgresError) as exc:
        pytest.skip(f"PostgreSQL for integration tests is unreachable ({exc}). Run `docker compose up -d`.")

    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", TEST_URL)
    command.upgrade(cfg, "head")
    return TEST_URL


@dataclass
class Bank:
    client: AsyncClient
    engine: AsyncEngine

    def headers(self, scope: str | None = "all", role: str = "teacher", institution: int = 12, user: int = 345) -> dict:
        headers = {
            "X-Theta-Token": SERVICE_TOKEN,
            "X-Theta-Institution-Id": str(institution),
            "X-Theta-User-Id": str(user),
            "X-Theta-Role": role,
        }
        if scope is not None:
            headers["X-Theta-Scope"] = scope
        return headers

    async def sql(self, statement: str, **params) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(text(statement), params)


@pytest.fixture
async def bank(migrated_database: str):
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.execute(
            text("TRUNCATE responses, sessions, assessment_candidates, assessment_sections, assessment_chapters, assessments, audit_log, item_params, question_options, questions, topics RESTART IDENTITY CASCADE")
        )
    factory = create_session_factory(engine)

    async def session_override():
        async with factory() as session:
            yield session

    app = create_app()
    app.dependency_overrides[get_settings] = lambda: _test_settings(SERVICE_TOKEN)
    app.dependency_overrides[get_session] = session_override
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://theta.test") as client:
        yield Bank(client, engine)
    await engine.dispose()
