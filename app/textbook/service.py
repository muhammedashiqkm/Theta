"""Textbooks, stateless: read pages into passages with embeddings, embed search text, and
check uploaded questions against the passages OnlineCampus found for them.

OnlineCampus keeps the book, its passages and their vectors (campus SQL Server), finds the
closest passages itself, and decides who may do what; Theta only reads, embeds and checks.
"""

import logging

from app.bank.enums import OPTION_TYPES, QuestionType
from app.core.errors import Unprocessable
from app.core.security import Caller
from app.generation.writer import QuestionWriter
from app.textbook.chunker import chunk_pages, heading_after
from app.textbook.gemini import TextbookAI
from app.textbook.schemas import (
    MAX_EMBED_TEXTS,
    MAX_PAGES_PER_READ,
    CheckIn,
    CheckItem,
    CheckOut,
    CheckResult,
    ChunkOut,
    EmbedIn,
    EmbedOut,
    PageOut,
    ReadIn,
    ReadOut,
)

log = logging.getLogger(__name__)

NOTE_MAX = 300


async def read(request: ReadIn, ai: TextbookAI, caller: Caller) -> ReadOut:
    if request.to_page < request.from_page:
        raise Unprocessable("The last page comes before the first page.")
    if request.to_page - request.from_page + 1 > MAX_PAGES_PER_READ:
        raise Unprocessable(f"Read at most {MAX_PAGES_PER_READ} pages at a time.")

    reply = await ai.read_pages(request.file_uri, request.from_page, request.to_page)
    texts_by_page = [(p.page, p.text) for p in reply.pages if p.text]
    chunks = chunk_pages(texts_by_page, request.heading)
    vectors: list[list[float]] = []
    texts = [c.for_embedding() for c in chunks]
    for start in range(0, len(texts), MAX_EMBED_TEXTS):
        vectors.extend(await ai.embed(texts[start : start + MAX_EMBED_TEXTS], "document"))

    at_end = reply.document_pages > 0 and request.to_page >= reply.document_pages
    if not reply.pages:
        # Nothing came back for pages that should exist: past the end of the book.
        at_end = True
    log.info(
        "textbook pages %s-%s read for institution %s user %s: %s pages, %s passages, tokens %s in / %s out",
        request.from_page, request.to_page, caller.institution_id, caller.user_id,
        len(reply.pages), len(chunks), reply.input_tokens, reply.output_tokens,
    )
    return ReadOut(
        pages=[PageOut(page=p.page, printed_page=p.printed_page, text=p.text) for p in reply.pages],
        chunks=[ChunkOut(page=c.page, order=c.order, heading=c.heading, text=c.text, embedding=v)
                for c, v in zip(chunks, vectors, strict=True)],
        document_pages=reply.document_pages,
        at_end=at_end,
        last_heading=heading_after(texts_by_page, request.heading),
        model=reply.model,
        input_tokens=reply.input_tokens,
        output_tokens=reply.output_tokens,
    )


async def embed(request: EmbedIn, ai: TextbookAI, model: str, dimensions: int) -> EmbedOut:
    return EmbedOut(vectors=await ai.embed(request.texts, request.kind), model=model, dimensions=dimensions)


# ----- checking uploaded questions against the book -------------------------------------------

CHECK_SYSTEM = """You check questions a teacher uploaded to a school question bank against pages of the class's own textbook. For each question you get the question, its options or accepted answers with the ones marked correct, and the textbook passages closest to it (each with its page number).

Judge only from the passages and from what a student at that class level is plainly expected to know alongside them:
- agrees: the answer marked correct is right according to the passages.
- disagrees: the passages show a different answer is right. Give the right one.
- no_key: no answer is marked; give the one the passages support.
- not_in_book: the passages do not cover what the question asks. Give no answer.

For option questions, correct_options lists the 0-based positions of every option the passages support (empty for not_in_book). For short answers, accepted_answers lists the answer the passages support in the forms a student might write (empty for not_in_book). page is the page the answer is on (0 when none). note is one short, plain sentence for the teacher, in the language of the question, quoting the book briefly when it helps. Return one result per question, with the question's key."""

CHECK_SCHEMA = {
    "type": "object",
    "properties": {
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "verdict": {"type": "string", "enum": ["agrees", "disagrees", "no_key", "not_in_book"]},
                    "correct_options": {"type": "array", "items": {"type": "integer"}},
                    "accepted_answers": {"type": "array", "items": {"type": "string"}},
                    "page": {"type": "integer"},
                    "note": {"type": "string"},
                },
                "required": ["key", "verdict", "correct_options", "accepted_answers", "page", "note"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["questions"],
    "additionalProperties": False,
}


def build_check(request: CheckIn) -> str:
    head = [f"Class: {request.class_name}", f"Subject: {request.subject_name}",
            "Language: Arabic" if request.language == "ar" else "Language: English"]
    parts = ["\n".join(head)]
    for item in request.items:
        lines = [f'<question key="{item.key}" type="{item.type.value}">', item.stem]
        if item.type in OPTION_TYPES:
            for i, option in enumerate(item.options):
                mark = " (marked correct)" if option.is_correct else ""
                lines.append(f"Option {i}: {option.text}{mark}")
        elif item.accepted_answers:
            lines.append("Accepted answers: " + " | ".join(item.accepted_answers))
        else:
            lines.append("Accepted answers: (none given)")
        if item.passages:
            for passage in item.passages:
                lines.append(f'<passage page="{passage.page}">\n{passage.text}\n</passage>')
        else:
            lines.append("(no textbook passage was found for this question)")
        lines.append("</question>")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


async def check(request: CheckIn, writer: QuestionWriter, caller: Caller) -> CheckOut:
    keys = [item.key for item in request.items]
    if len(set(keys)) != len(keys):
        raise Unprocessable("Each question needs its own key.")
    reply = await writer.write(CHECK_SYSTEM, build_check(request), CHECK_SCHEMA)

    by_key = {item.key: item for item in request.items}
    results: dict[str, CheckResult] = {}
    for raw in reply.questions:
        key = raw.get("key")
        item = by_key.get(key) if isinstance(key, str) else None
        if item is None or key in results:
            continue
        results[key] = _result(raw, item)
    # A question the model skipped is reported as not checked, never guessed.
    items = [results.get(item.key) or CheckResult(
        key=item.key, verdict="not_in_book", correct_options=[], accepted_answers=[], page=0,
        note="This question could not be checked.") for item in request.items]

    log.info(
        "checked %s uploaded questions for institution %s user %s: tokens %s in / %s out, model %s",
        len(items), caller.institution_id, caller.user_id, reply.input_tokens, reply.output_tokens, reply.model,
    )
    return CheckOut(items=items, model=reply.model, input_tokens=reply.input_tokens, output_tokens=reply.output_tokens)


def _result(raw: dict, item: CheckItem) -> CheckResult:
    verdict = raw.get("verdict")
    if verdict not in ("agrees", "disagrees", "no_key", "not_in_book"):
        verdict = "not_in_book"
    pages = {p.page for p in item.passages}
    page = raw.get("page")
    page = page if isinstance(page, int) and page in pages else 0

    correct: list[int] = []
    accepted: list[str] = []
    if verdict != "not_in_book":
        if item.type in OPTION_TYPES:
            raw_correct = raw.get("correct_options") if isinstance(raw.get("correct_options"), list) else []
            correct = sorted({i for i in raw_correct if isinstance(i, int) and 0 <= i < len(item.options)})
            if item.type in (QuestionType.MCQ_SINGLE, QuestionType.TRUE_FALSE) and len(correct) != 1:
                correct = []
        else:
            raw_accepted = raw.get("accepted_answers") if isinstance(raw.get("accepted_answers"), list) else []
            for answer in raw_accepted:
                text = answer.strip() if isinstance(answer, str) else ""
                if text and text.casefold() not in {a.casefold() for a in accepted}:
                    accepted.append(text[:500])
            accepted = accepted[:10]
        # A verdict with no answer to back it is not one.
        if not correct and not accepted:
            verdict = "not_in_book"
    has_key = any(o.is_correct for o in item.options) if item.type in OPTION_TYPES else bool(item.accepted_answers)
    if verdict in ("agrees", "disagrees") and not has_key:
        verdict = "no_key"
    elif verdict == "no_key" and has_key:
        verdict = "agrees"
    if verdict == "agrees" and item.type in OPTION_TYPES:
        marked = sorted(i for i, o in enumerate(item.options) if o.is_correct)
        if marked != correct:
            verdict = "disagrees"

    note = raw.get("note") if isinstance(raw.get("note"), str) else ""
    note = " ".join(note.split())
    return CheckResult(
        key=item.key, verdict=verdict, correct_options=correct, accepted_answers=accepted, page=page,
        note=note if len(note) <= NOTE_MAX else note[: NOTE_MAX - 1] + "…",
    )
