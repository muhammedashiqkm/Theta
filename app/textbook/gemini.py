"""The one place Theta talks to Gemini about textbooks: holding the PDF, reading its pages,
and turning text into embeddings.

Gemini reads the PDF itself, so a scanned book (no text layer, common for printed school
books and for Arabic) reads the same way as a typed one, and Theta needs no PDF library.
"""

import io
import json
import logging
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, Protocol, TypeVar

from fastapi import Depends
from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types

from app.core.config import Settings, get_settings
from app.core.errors import BadGateway, Gone, ServiceUnavailable, Unprocessable
from app.textbook.schemas import UploadOut

log = logging.getLogger(__name__)

TIMEOUT_SECONDS = 150.0
# Room for ten dense pages of text.
READ_MAX_TOKENS = 32000
# Gemini's limit for one PDF.
MAX_PDF_BYTES = 50 * 1024 * 1024

READ_SYSTEM = """You transcribe pages of a school textbook so they can be searched. Copy the text exactly as printed, in the book's own language and spelling; do not translate, summarise, correct or add anything.

- Mark every heading (chapter, unit, lesson or section title) on a line of its own starting with "# ".
- Keep paragraphs apart with a blank line. Join words broken across lines.
- Leave out running headers, footers and the page numbers themselves.
- When a picture, diagram, table or map carries content a question could ask about, describe it in one line in square brackets, e.g. [Picture: the phases of the moon, new moon to full moon]. Write a table's rows as plain lines.
- Write math in LaTeX between $...$.
- printed_page is the page number printed on that page ("" when none is printed).
- A page with nothing to read gets text "".
- document_pages is how many pages the whole PDF has."""

READ_SCHEMA = {
    "type": "object",
    "properties": {
        "document_pages": {"type": "integer"},
        "pages": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "page": {"type": "integer"},
                    "printed_page": {"type": "string"},
                    "text": {"type": "string"},
                },
                "required": ["page", "printed_page", "text"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["document_pages", "pages"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class ReadPage:
    page: int
    printed_page: str | None
    text: str


@dataclass(frozen=True)
class PagesReply:
    pages: list[ReadPage]
    document_pages: int
    model: str
    input_tokens: int
    output_tokens: int


class TextbookAI(Protocol):
    async def upload(self, data: bytes, display_name: str) -> UploadOut: ...
    async def read_pages(self, file_uri: str, from_page: int, to_page: int) -> PagesReply: ...
    async def embed(self, texts: list[str], kind: str) -> list[list[float]]: ...
    async def delete(self, file_name: str) -> None: ...


T = TypeVar("T")


async def _call(what: str, run: Callable[[], Awaitable[T]]) -> T:
    """One Gemini call, its failures turned into answers a teacher can act on."""
    try:
        return await run()
    except genai_errors.ClientError as exc:
        status = getattr(exc, "code", None)
        if status == 429:
            log.warning("Gemini rate limit (%s)", what)
            raise ServiceUnavailable("Gemini is busy right now. Try again in a minute.") from None
        if status == 401 or (status == 403 and "file" not in str(exc).lower()):
            log.error("Gemini refused the API key (%s): %s", what, status)
            raise ServiceUnavailable("The Google API key on the Theta server was not accepted.") from None
        if status in (403, 404) and "file" in str(exc).lower():
            raise Gone("The textbook file is no longer held by Gemini. Upload the book again.") from None
        log.warning("Gemini answered %s (%s)", status, what)
        raise BadGateway(f"Gemini could not {what} (error {status}). Try again.") from None
    except genai_errors.ServerError as exc:
        log.warning("Gemini server error %s (%s)", getattr(exc, "code", None), what)
        raise BadGateway(f"Gemini could not {what}. Try again.") from None
    except genai_errors.APIError as exc:
        log.warning("Gemini call failed (%s): %s", what, type(exc).__name__)
        raise BadGateway("Gemini could not be reached. Try again.") from None


def normalise(vector: list[float]) -> list[float]:
    """Unit length, so a dot product is the cosine similarity."""
    norm = math.sqrt(sum(v * v for v in vector))
    return [v / norm for v in vector] if norm > 0 else vector


class GeminiTextbookAI:
    def __init__(self, client: genai.Client, settings: Settings):
        self._client = client
        self._read_model = settings.textbook_read_model
        self._embed_model = settings.embedding_model
        self._dimensions = settings.embedding_dimensions

    async def upload(self, data: bytes, display_name: str) -> UploadOut:
        config = genai_types.UploadFileConfig(mime_type="application/pdf", display_name=display_name[:120] or "textbook")
        file = await _call("take the textbook", lambda: self._client.aio.files.upload(file=io.BytesIO(data), config=config))
        if not file.uri or not file.name:
            raise BadGateway("Gemini took the textbook but gave no reference to it. Try again.")
        return UploadOut(
            file_name=file.name,
            file_uri=file.uri,
            size_bytes=len(data),
            expires_at=file.expiration_time.isoformat() if file.expiration_time else None,
        )

    async def read_pages(self, file_uri: str, from_page: int, to_page: int) -> PagesReply:
        config = genai_types.GenerateContentConfig(
            system_instruction=READ_SYSTEM,
            max_output_tokens=READ_MAX_TOKENS,
            response_mime_type="application/json",
            response_json_schema=READ_SCHEMA,
            automatic_function_calling=genai_types.AutomaticFunctionCallingConfig(disable=True),
        )
        ask = (f"Transcribe pages {from_page} to {to_page} of this PDF (page 1 is the first page of the file). "
               "If the PDF ends before that, transcribe the pages it has.")
        contents = [genai_types.Part.from_uri(file_uri=file_uri, mime_type="application/pdf"), ask]
        response = await _call("read the textbook", lambda: self._client.aio.models.generate_content(
            model=self._read_model, contents=contents, config=config))

        candidate = (response.candidates or [None])[0]
        reason = getattr(candidate, "finish_reason", None)
        reason_name = getattr(reason, "name", str(reason) if reason else "")
        if candidate is None or reason_name in ("SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION"):
            log.info("Gemini declined to read pages %s-%s (%s)", from_page, to_page, reason_name or "no candidate")
            raise Unprocessable("Gemini declined to read these pages. Try fewer pages at a time.")
        if reason_name == "MAX_TOKENS":
            raise BadGateway("These pages came back cut off. Read fewer pages at a time.")
        try:
            data = json.loads(response.text) if response.text else None
        except ValueError:
            data = None
        if not isinstance(data, dict) or not isinstance(data.get("pages"), list):
            raise BadGateway("Gemini's reading of the pages could not be understood. Try again.")

        pages: dict[int, ReadPage] = {}
        for raw in data["pages"]:
            if not isinstance(raw, dict):
                continue
            page = raw.get("page")
            if not isinstance(page, int) or not from_page <= page <= to_page or page in pages:
                continue
            printed = raw.get("printed_page")
            text = raw.get("text")
            pages[page] = ReadPage(
                page=page,
                printed_page=printed.strip() or None if isinstance(printed, str) else None,
                text=text.strip() if isinstance(text, str) else "",
            )
        usage = response.usage_metadata
        total = data.get("document_pages")
        return PagesReply(
            pages=[pages[p] for p in sorted(pages)],
            document_pages=total if isinstance(total, int) and total > 0 else 0,
            model=response.model_version or self._read_model,
            input_tokens=getattr(usage, "prompt_token_count", 0) or 0,
            output_tokens=(getattr(usage, "candidates_token_count", 0) or 0) + (getattr(usage, "thoughts_token_count", 0) or 0),
        )

    async def embed(self, texts: list[str], kind: str) -> list[list[float]]:
        config = genai_types.EmbedContentConfig(
            task_type="RETRIEVAL_QUERY" if kind == "query" else "RETRIEVAL_DOCUMENT",
            output_dimensionality=self._dimensions,
        )
        response = await _call("read the text for searching", lambda: self._client.aio.models.embed_content(
            model=self._embed_model, contents=texts, config=config))
        vectors = [list(e.values or []) for e in (response.embeddings or [])]
        if len(vectors) != len(texts) or any(len(v) != self._dimensions for v in vectors):
            raise BadGateway("Gemini's search vectors came back incomplete. Try again.")
        return [normalise(v) for v in vectors]

    async def delete(self, file_name: str) -> None:
        await _call("remove the textbook file", lambda: self._client.aio.files.delete(name=file_name))

    @property
    def embed_model(self) -> str:
        return self._embed_model


_clients: dict[str, genai.Client] = {}


def _client(api_key: str) -> genai.Client:
    if api_key not in _clients:
        _clients[api_key] = genai.Client(
            api_key=api_key, http_options=genai_types.HttpOptions(timeout=int(TIMEOUT_SECONDS * 1000)))
    return _clients[api_key]


def get_textbook_ai(settings: Annotated[Settings, Depends(get_settings)]) -> TextbookAI:
    """Injected into the endpoints; a test replaces it with a fake."""
    if not settings.google_api_key:
        raise ServiceUnavailable("Textbooks are not set up on this server: it has no Google API key.")
    return GeminiTextbookAI(_client(settings.google_api_key), settings)
