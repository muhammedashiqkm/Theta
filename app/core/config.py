"""Settings, read once from the environment (or .env) with the THETA_ prefix."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="THETA_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    app_name: str = "Theta"
    app_version: str = "0.1.0"
    environment: str = "development"

    # Shared secret the OnlineCampus API sends as X-Theta-Token. Theta is never
    # called by browsers, so this token plus a private network is the whole gate.
    # Left empty, every protected endpoint refuses every call.
    service_token: str = ""

    # Host ports 5433 / 6380, not the defaults: this machine already runs a local
    # PostgreSQL on 5432, and other containers commonly hold 6379.
    database_url: str = "postgresql+asyncpg://theta:theta_dev_password@127.0.0.1:5433/theta"
    redis_url: str = "redis://127.0.0.1:6380/0"

    # Question writing (POST /v1/generate/questions) calls Claude through the Anthropic
    # API. Left empty, that endpoint answers 503 and nothing else is affected.
    anthropic_api_key: str = ""
    # Google AI Studio key, for the Gemini models in `generation_models`.
    google_api_key: str = ""
    # The model used when a request names none (and the default a school starts on).
    generation_model: str = "claude-opus-5"
    # Every model a school may be put on, newest-capable first. The provider is read from
    # the name: "claude-*" goes to Anthropic, "gemini-*" to Google. A name outside this list
    # is refused, so nothing upstream can send Theta at an arbitrary (or costly) model.
    generation_models: str = "claude-opus-5,claude-sonnet-5,gemini-pro-latest,gemini-3.8-flash"
    # AI marking of adaptive tests (POST /v1/mark/*): short answers during the test,
    # the written part's mark schemes and marks after it.
    marking_model: str = "claude-opus-5"


@lru_cache
def get_settings() -> Settings:
    return Settings()
