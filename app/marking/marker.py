"""Where marking talks to Claude. One small structured request per check."""

import json
import logging
from dataclasses import dataclass
from typing import Annotated, Literal, Protocol

import anthropic
from fastapi import Depends

from app.core.config import Settings, get_settings
from app.core.errors import BadGateway, ServiceUnavailable
from app.generation.writer import FALLBACK_BETA, _client

log = logging.getLogger(__name__)

Effort = Literal["low", "medium", "high"]

# Room for Claude's thinking plus a small JSON answer; a marking reply is short.
MAX_TOKENS = 16000


class MarkRefused(Exception):
    """Claude declined to mark this text (a policy decline, not an outage)."""


@dataclass(frozen=True)
class MarkReply:
    data: dict
    model: str


class Marker(Protocol):
    async def ask(self, system: str, prompt: str, schema: dict, effort: Effort, timeout: float,
                  quick: bool = False) -> MarkReply: ...


class ClaudeMarker:
    def __init__(self, client: anthropic.AsyncAnthropic, model: str, quick_model: str = ""):
        self._client = client
        self._model = model
        self._quick_model = quick_model

    async def ask(self, system: str, prompt: str, schema: dict, effort: Effort, timeout: float,
                  quick: bool = False) -> MarkReply:
        """`quick`: the fast first check, on the quick model. It has no server-side refusal
        fallback, so it is sent without one; a decline there goes to the careful check."""
        client = self._client.with_options(timeout=timeout, max_retries=1)
        output_config = {"effort": effort, "format": {"type": "json_schema", "schema": schema}}
        try:
            if quick and self._quick_model:
                message = await client.messages.create(
                    model=self._quick_model,
                    max_tokens=MAX_TOKENS,
                    system=system,
                    messages=[{"role": "user", "content": prompt}],
                    output_config=output_config,
                )
            else:
                message = await client.beta.messages.create(
                    model=self._model,
                    max_tokens=MAX_TOKENS,
                    betas=[FALLBACK_BETA],
                    fallbacks="default",
                    system=system,
                    messages=[{"role": "user", "content": prompt}],
                    output_config=output_config,
                )
        except anthropic.RateLimitError as exc:
            log.warning("Claude rate limit while marking (request %s)", exc.request_id)
            raise ServiceUnavailable("Claude is busy right now; the answer will be marked shortly.") from None
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
            log.error("Claude refused the API key: %s (request %s)", exc.status_code, exc.request_id)
            raise ServiceUnavailable("The Anthropic API key on the Theta server was not accepted.") from None
        except anthropic.APIStatusError as exc:
            log.warning("Claude answered %s while marking (request %s)", exc.status_code, exc.request_id)
            raise BadGateway(f"Claude could not mark the answer (error {exc.status_code}).") from None
        except anthropic.APIConnectionError as exc:
            log.warning("Claude unreachable while marking: %s", type(exc).__name__)
            raise BadGateway("Claude could not be reached.") from None

        if message.stop_reason == "refusal":
            category = message.stop_details.category if message.stop_details else None
            log.info("Claude declined to mark (category %s, request %s)", category, getattr(message, "_request_id", None))
            raise MarkRefused()
        if message.stop_reason == "max_tokens":
            raise BadGateway("Claude's marking came back cut off.")

        text = next((block.text for block in message.content if block.type == "text"), None)
        try:
            data = json.loads(text) if text else None
        except ValueError:
            data = None
        if not isinstance(data, dict):
            log.warning("Claude's marking was not the expected JSON (request %s)", getattr(message, "_request_id", None))
            raise BadGateway("Claude's marking could not be read.")
        return MarkReply(data, message.model)


def get_marker(settings: Annotated[Settings, Depends(get_settings)]) -> Marker:
    if not settings.anthropic_api_key:
        raise ServiceUnavailable("Marking is not set up on this server: it has no Anthropic API key.")
    return ClaudeMarker(_client(settings.anthropic_api_key), settings.marking_model, settings.marking_quick_model)
