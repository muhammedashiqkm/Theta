"""Connections to PostgreSQL and Redis, created once per process."""

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


def create_engine(database_url: str) -> AsyncEngine:
    return create_async_engine(
        database_url,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
        connect_args={"timeout": 5},
    )


def create_redis(redis_url: str) -> Redis:
    return Redis.from_url(redis_url, socket_connect_timeout=3, socket_timeout=3, decode_responses=True)
