"""Which model writes the questions. The choice belongs to the SCHOOL: OnlineCampus keeps it
per institution and sends it with the request, so Theta must route by the name it is given,
refuse anything outside its allow-list, and say which providers it can actually reach.
"""

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings, get_settings
from app.core.errors import ServiceUnavailable, Unprocessable
from app.generation.writer import (
    ClaudeWriter,
    GeminiWriter,
    allowed_models,
    provider_of,
    writer_for,
)
from app.main import create_app
from tests.conftest import SERVICE_TOKEN

pytestmark = pytest.mark.anyio

BOTH_KEYS = dict(anthropic_api_key="sk-test", google_api_key="AIza-test")


@pytest.fixture
def headers() -> dict[str, str]:
    return {
        "X-Theta-Token": SERVICE_TOKEN,
        "X-Theta-Institution-Id": "12",
        "X-Theta-User-Id": "345",
        "X-Theta-Role": "teacher",
    }


def settings(**overrides) -> Settings:
    return Settings(service_token=SERVICE_TOKEN, _env_file=None, **overrides)


def client(**overrides) -> AsyncClient:
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings(**overrides)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://theta.test")


# ----- routing ------------------------------------------------------------------

def test_the_name_decides_the_provider():
    assert provider_of("claude-opus-5") == "anthropic"
    assert provider_of("gemini-3.8-flash") == "google"


def test_a_name_from_neither_provider_is_refused():
    with pytest.raises(Unprocessable):
        provider_of("gpt-9")


def test_each_allowed_model_builds_its_own_writer():
    both = settings(**BOTH_KEYS)
    assert isinstance(writer_for(both, "claude-opus-5"), ClaudeWriter)
    assert isinstance(writer_for(both, "claude-sonnet-5"), ClaudeWriter)
    assert isinstance(writer_for(both, "gemini-pro-latest"), GeminiWriter)
    assert isinstance(writer_for(both, "gemini-3.8-flash"), GeminiWriter)


def test_no_model_named_uses_this_server_default():
    writer = writer_for(settings(**BOTH_KEYS, generation_model="gemini-3.8-flash"), None)
    assert isinstance(writer, GeminiWriter)


def test_a_model_outside_the_allow_list_is_refused_before_any_provider_is_called():
    # Even a real model name: the school's setting cannot reach past what this server offers.
    with pytest.raises(Unprocessable) as exc:
        writer_for(settings(**BOTH_KEYS), "claude-opus-4-1")
    assert "claude-opus-5" in str(exc.value)


def test_the_allow_list_is_ordered_and_deduplicated():
    listed = allowed_models(settings(generation_models="claude-opus-5, gemini-3.8-flash ,claude-opus-5"))
    assert listed == ["claude-opus-5", "gemini-3.8-flash"]


# ----- a missing key is a clear refusal, per provider ----------------------------

def test_without_the_anthropic_key_only_gemini_works():
    only_google = settings(google_api_key="AIza-test")
    assert isinstance(writer_for(only_google, "gemini-3.8-flash"), GeminiWriter)
    with pytest.raises(ServiceUnavailable) as exc:
        writer_for(only_google, "claude-opus-5")
    assert "Anthropic" in str(exc.value)


def test_without_the_google_key_only_claude_works():
    only_anthropic = settings(anthropic_api_key="sk-test")
    assert isinstance(writer_for(only_anthropic, "claude-opus-5"), ClaudeWriter)
    with pytest.raises(ServiceUnavailable) as exc:
        writer_for(only_anthropic, "gemini-pro-latest")
    assert "Google" in str(exc.value)


# ----- what OnlineCampus builds its picker from ----------------------------------

async def test_the_models_endpoint_lists_every_choice_with_its_provider(headers):
    async with client(**BOTH_KEYS) as api:
        response = await api.get("/v1/generate/models", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["default"] == "claude-opus-5"
    assert [m["name"] for m in body["models"]] == [
        "claude-opus-5", "claude-sonnet-5", "gemini-pro-latest", "gemini-3.8-flash",
    ]
    assert [m["provider"] for m in body["models"]] == ["anthropic", "anthropic", "google", "google"]
    assert all(m["ready"] for m in body["models"])
    assert [m["is_default"] for m in body["models"]] == [True, False, False, False]


async def test_a_model_whose_key_is_missing_is_listed_but_not_ready(headers):
    async with client(anthropic_api_key="sk-test") as api:
        body = (await api.get("/v1/generate/models", headers=headers)).json()
    ready = {m["name"]: m["ready"] for m in body["models"]}
    assert ready["claude-opus-5"] is True
    assert ready["gemini-3.8-flash"] is False


async def test_the_models_endpoint_needs_the_service_token():
    async with client(**BOTH_KEYS) as api:
        assert (await api.get("/v1/generate/models")).status_code == 401


async def test_a_mistyped_model_in_config_does_not_break_the_list(headers):
    async with client(**BOTH_KEYS, generation_models="claude-opus-5,gpt-9,gemini-3.8-flash") as api:
        body = (await api.get("/v1/generate/models", headers=headers)).json()
    assert [m["name"] for m in body["models"]] == ["claude-opus-5", "gemini-3.8-flash"]
