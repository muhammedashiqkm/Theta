"""Theta: the adaptive assessment service behind OnlineCampus."""

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api import health
from app.api.v1 import assessments, generate, mark, questions, status, student, topics
from app.core.config import get_settings
from app.core.db import create_session_factory
from app.core.errors import ThetaError
from app.core.stores import create_engine, create_redis


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.engine = create_engine(settings.database_url)
    app.state.session_factory = create_session_factory(app.state.engine)
    app.state.redis = create_redis(settings.redis_url)
    try:
        yield
    finally:
        await app.state.redis.aclose()
        await app.state.engine.dispose()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        lifespan=lifespan,
        # Interactive docs only outside production; the API is internal either way.
        docs_url=None if settings.environment == "production" else "/docs",
        redoc_url=None,
    )

    @app.exception_handler(ThetaError)
    async def theta_error(_: Request, exc: ThetaError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=exc.status_code)

    app.include_router(health.router)
    app.include_router(status.router)
    app.include_router(topics.router)
    app.include_router(questions.router)
    app.include_router(assessments.router)
    app.include_router(student.router)
    app.include_router(generate.router)
    app.include_router(mark.router)
    return app


app = create_app()
