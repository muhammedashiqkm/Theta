"""Textbooks: a school's own book, read into searchable passages (staff only).

The flow OnlineCampus runs for a new book:
  1. POST /v1/textbook/upload   the PDF as the raw body → Gemini holds it for ~2 days.
  2. POST /v1/textbook/read     ten pages at a time until `at_end` → pages + passages + vectors,
                                which OnlineCampus stores. Pass `last_heading` on to the next call.
  3. DELETE /v1/textbook/file   when done (or let it expire).
Then, any time:
  POST /v1/textbook/embed       a topic or question as a search vector.
  POST /v1/textbook/check       uploaded questions + their closest passages → is the key right?
Writing questions from the book goes through /v1/generate/questions with `source_passages`.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status

from app.core.config import Settings, get_settings
from app.core.errors import Unprocessable
from app.core.security import Caller, require_staff
from app.generation.writer import WriterFactory, get_writer_factory
from app.textbook import service
from app.textbook.gemini import MAX_PDF_BYTES, TextbookAI, get_textbook_ai
from app.textbook.schemas import CheckIn, CheckOut, EmbedIn, EmbedOut, ReadIn, ReadOut, UploadOut

router = APIRouter(prefix="/v1/textbook", tags=["textbook"])


@router.post("/upload", response_model=UploadOut, status_code=status.HTTP_201_CREATED)
async def upload(
    request: Request,
    caller: Annotated[Caller, Depends(require_staff)],
    ai: Annotated[TextbookAI, Depends(get_textbook_ai)],
    name: Annotated[str, Query(max_length=200)] = "textbook",
) -> UploadOut:
    # The PDF is the raw body (Content-Type: application/pdf), so no multipart parser is needed.
    data = await request.body()
    if not data:
        raise Unprocessable("No file was sent.")
    if len(data) > MAX_PDF_BYTES:
        raise Unprocessable("The textbook is larger than 50 MB. Split it into parts.")
    if not data.startswith(b"%PDF-"):
        raise Unprocessable("The file is not a PDF.")
    return await ai.upload(data, name.strip() or "textbook")


@router.post("/read", response_model=ReadOut)
async def read(
    body: ReadIn,
    caller: Annotated[Caller, Depends(require_staff)],
    ai: Annotated[TextbookAI, Depends(get_textbook_ai)],
) -> ReadOut:
    return await service.read(body, ai, caller)


@router.delete("/file", status_code=status.HTTP_204_NO_CONTENT)
async def delete_file(
    caller: Annotated[Caller, Depends(require_staff)],
    ai: Annotated[TextbookAI, Depends(get_textbook_ai)],
    name: Annotated[str, Query(min_length=1, max_length=200)],
) -> None:
    await ai.delete(name)


@router.post("/embed", response_model=EmbedOut)
async def embed(
    body: EmbedIn,
    caller: Annotated[Caller, Depends(require_staff)],
    ai: Annotated[TextbookAI, Depends(get_textbook_ai)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> EmbedOut:
    return await service.embed(body, ai, settings.embedding_model, settings.embedding_dimensions)


@router.post("/check", response_model=CheckOut)
async def check(
    body: CheckIn,
    caller: Annotated[Caller, Depends(require_staff)],
    make_writer: Annotated[WriterFactory, Depends(get_writer_factory)],
) -> CheckOut:
    # The school's question-writing model checks too (Claude or Gemini).
    return await service.check(body, make_writer(body.model), caller)
