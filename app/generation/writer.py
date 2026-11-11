"""The one place Theta talks to a model that writes questions.

Two providers, chosen by the model name the request carries (OnlineCampus sends the
institution's setting): "claude-*" → Anthropic, "gemini-*" → Google. Both are asked for the
SAME JSON schema and return the same ModelReply, so nothing downstream knows or cares which
one answered — the bank's own rules check every draft either way, and the teacher verifies it
before a student sees it.
"""

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from typing import Annotated, Protocol

import anthropic
from fastapi import Depends
from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types

from app.core.config import Settings, get_settings
from app.core.errors import BadGateway, ServiceUnavailable, Unprocessable

log = logging.getLogger(__name__)

# Room for ten questions with explanations plus Claude's thinking. Streaming keeps a
# request this large clear of HTTP timeouts.
MAX_TOKENS = 32000
# OnlineCampus waits a little over three minutes for Theta; stay inside that.
TIMEOUT_SECONDS = 150.0
# On a policy decline, Anthropic re-runs the request on its recommended fallback model.
FALLBACK_BETA = "server-side-fallback-2026-07-01"


@dataclass(frozen=True)
class ModelReply:
    questions: list[dict]
    model: str
    input_tokens: int
    output_tokens: int


class QuestionWriter(Protocol):
    async def write(self, system: str, prompt: str, schema: dict) -> ModelReply: ...


@lru_cache(maxsize=4)
def _client(api_key: str) -> anthropic.AsyncAnthropic:
    # One client per key for the life of the process, so connections are reused.
    return anthropic.AsyncAnthropic(api_key=api_key, timeout=TIMEOUT_SECONDS, max_retries=1)


@lru_cache(maxsize=4)
def _genai_client(api_key: str) -> genai.Client:
    return genai.Client(api_key=api_key, http_options=genai_types.HttpOptions(timeout=int(TIMEOUT_SECONDS * 1000)))


def allowed_models(settings: Settings) -> list[str]:
    """The models a school may be put on, in the order they are offered."""
    names = [name.strip() for name in settings.generation_models.split(",")]
    return [name for name in dict.fromkeys(names) if name]


def provider_of(model: str) -> str:
    """Which provider a model name belongs to. Unknown prefixes are refused."""
    if model.startswith("claude"):
        return "anthropic"
    if model.startswith("gemini"):
        return "google"
    raise Unprocessable(f"'{model}' is not a model Theta knows how to ask.")


class ClaudeWriter:
    def __init__(self, client: anthropic.AsyncAnthropic, model: str):
        self._client = client
        self._model = model

    async def write(self, system: str, prompt: str, schema: dict) -> ModelReply:
        try:
            async with self._client.beta.messages.stream(
                model=self._model,
                max_tokens=MAX_TOKENS,
                betas=[FALLBACK_BETA],
                fallbacks="default",
                system=system,
                messages=[{"role": "user", "content": prompt}],
                output_config={"format": {"type": "json_schema", "schema": schema}},
            ) as stream:
                message = await stream.get_final_message()
        except anthropic.RateLimitError as exc:
            log.warning("Claude rate limit (request %s)", exc.request_id)
            raise ServiceUnavailable("Claude is busy right now. Try again in a minute.") from None
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
            log.error("Claude refused the API key: %s (request %s)", exc.status_code, exc.request_id)
            raise ServiceUnavailable("The Anthropic API key on the Theta server was not accepted.") from None
        except anthropic.APIStatusError as exc:
            log.warning("Claude answered %s (request %s)", exc.status_code, exc.request_id)
            raise BadGateway(f"Claude could not write the questions (error {exc.status_code}). Try again.") from None
        except anthropic.APIConnectionError as exc:
            log.warning("Claude unreachable: %s", type(exc).__name__)
            raise BadGateway("Claude could not be reached. Try again.") from None

        # A declined request can come back with partial or no content: check first.
        if message.stop_reason == "refusal":
            category = message.stop_details.category if message.stop_details else None
            log.info("Claude declined (category %s, request %s)", category, getattr(message, "_request_id", None))
            raise Unprocessable("Claude declined to write these questions. Change the topic or the notes and try again.")
        if message.stop_reason == "max_tokens":
            raise BadGateway("The questions came back cut off. Ask for fewer at a time.")

        text = next((block.text for block in message.content if block.type == "text"), None)
        try:
            data = json.loads(text) if text else None
        except ValueError:
            data = None
        questions = data.get("questions") if isinstance(data, dict) else None
        if not isinstance(questions, list):
            log.warning("Claude's answer was not the expected JSON (request %s)", getattr(message, "_request_id", None))
            raise BadGateway("Claude's answer could not be read. Try again.")

        return ModelReply(
            questions=[q for q in questions if isinstance(q, dict)],
            model=message.model,
            input_tokens=message.usage.input_tokens,
            output_tokens=message.usage.output_tokens,
        )


class GeminiWriter:
    """Gemini, asked for the same JSON schema. `response_json_schema` takes the schema the
    Anthropic call uses, so the two providers cannot drift apart."""

    def __init__(self, client: genai.Client, model: str):
        self._client = client
        self._model = model

    async def write(self, system: str, prompt: str, schema: dict) -> ModelReply:
        config = genai_types.GenerateContentConfig(
            system_instruction=system,
            max_output_tokens=MAX_TOKENS,
            response_mime_type="application/json",
            response_json_schema=schema,
            # No tools are offered, and the SDK warns when its function-calling loop is left on.
            automatic_function_calling=genai_types.AutomaticFunctionCallingConfig(disable=True),
        )
        try:
            response = await self._client.aio.models.generate_content(
                model=self._model, contents=prompt, config=config
            )
        except genai_errors.ClientError as exc:
            status = getattr(exc, "code", None)
            if status == 429:
                log.warning("Gemini rate limit")
                raise ServiceUnavailable("Gemini is busy right now. Try again in a minute.") from None
            if status in (401, 403):
                log.error("Gemini refused the API key: %s", status)
                raise ServiceUnavailable("The Google API key on the Theta server was not accepted.") from None
            log.warning("Gemini answered %s", status)
            raise BadGateway(f"Gemini could not write the questions (error {status}). Try again.") from None
        except genai_errors.ServerError as exc:
            log.warning("Gemini server error: %s", getattr(exc, "code", None))
            raise BadGateway("Gemini could not write the questions. Try again.") from None
        except genai_errors.APIError as exc:
            log.warning("Gemini call failed: %s", type(exc).__name__)
            raise BadGateway("Gemini could not be reached. Try again.") from None

        # A blocked prompt comes back with no candidate; a truncated one with MAX_TOKENS.
        candidate = (response.candidates or [None])[0]
        reason = getattr(candidate, "finish_reason", None)
        reason_name = getattr(reason, "name", str(reason) if reason else "")
        if candidate is None or reason_name in ("SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII"):
            log.info("Gemini declined (%s)", reason_name or "no candidate")
            raise Unprocessable("Gemini declined to write these questions. Change the topic or the notes and try again.")
        if reason_name == "MAX_TOKENS":
            raise BadGateway("The questions came back cut off. Ask for fewer at a time.")

        try:
            data = json.loads(response.text) if response.text else None
        except ValueError:
            data = None
        questions = data.get("questions") if isinstance(data, dict) else None
        if not isinstance(questions, list):
            log.warning("Gemini's answer was not the expected JSON")
            raise BadGateway("Gemini's answer could not be read. Try again.")

        usage = response.usage_metadata
        return ModelReply(
            questions=[q for q in questions if isinstance(q, dict)],
            model=response.model_version or self._model,
            input_tokens=getattr(usage, "prompt_token_count", 0) or 0,
            # Thinking tokens are billed as output, so count them.
            output_tokens=(getattr(usage, "candidates_token_count", 0) or 0)
            + (getattr(usage, "thoughts_token_count", 0) or 0),
        )


def writer_for(settings: Settings, model: str | None) -> QuestionWriter:
    """The writer for the model a request asked for (or this server's default).

    The name must be one of `generation_models`: a school's setting travels from
    OnlineCampus in the request, and an unknown or unlisted name is refused here rather
    than passed to a provider.
    """
    wanted = (model or settings.generation_model).strip()
    allowed = allowed_models(settings)
    if wanted not in allowed:
        raise Unprocessable(f"'{wanted}' is not one of the models this server offers: {', '.join(allowed)}.")

    provider = provider_of(wanted)
    if provider == "anthropic":
        if not settings.anthropic_api_key:
            raise ServiceUnavailable("Question writing with Claude is not set up on this server: it has no Anthropic API key.")
        return ClaudeWriter(_client(settings.anthropic_api_key), wanted)

    if not settings.google_api_key:
        raise ServiceUnavailable("Question writing with Gemini is not set up on this server: it has no Google API key.")
    return GeminiWriter(_genai_client(settings.google_api_key), wanted)


WriterFactory = Callable[[str | None], QuestionWriter]


def get_writer_factory(settings: Annotated[Settings, Depends(get_settings)]) -> WriterFactory:
    """Injected into the endpoint: the writer can only be chosen once the body is read,
    because the body carries the school's model. One seam, so a test can replace the model
    with a fake."""

    def factory(model: str | None = None) -> QuestionWriter:
        return writer_for(settings, model)

    return factory
