"""Textbooks: cutting pages into passages, the read / embed / upload / check endpoints, and
question writing from passages. Gemini and Claude are faked: no test calls a provider."""

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings, get_settings
from app.generation import prompt
from app.generation.writer import ModelReply, get_writer_factory
from app.main import create_app
from app.textbook import chunker
from app.textbook.gemini import PagesReply, ReadPage, get_textbook_ai, normalise
from app.textbook.schemas import UploadOut
from tests.conftest import SERVICE_TOKEN


class FakeAI:
    def __init__(self, pages: list[ReadPage] | None = None, document_pages: int = 40):
        self.pages = pages or []
        self.document_pages = document_pages
        self.embedded: list[tuple[list[str], str]] = []
        self.uploaded: list[tuple[int, str]] = []
        self.deleted: list[str] = []

    async def upload(self, data: bytes, display_name: str) -> UploadOut:
        self.uploaded.append((len(data), display_name))
        return UploadOut(file_name="files/abc", file_uri="https://gemini/files/abc", size_bytes=len(data), expires_at=None)

    async def read_pages(self, file_uri: str, from_page: int, to_page: int) -> PagesReply:
        pages = [p for p in self.pages if from_page <= p.page <= to_page]
        return PagesReply(pages, self.document_pages, "gemini-3.8-flash", 5000, 2000)

    async def embed(self, texts: list[str], kind: str) -> list[list[float]]:
        self.embedded.append((texts, kind))
        return [normalise([float(len(t)), 1.0, 0.0]) for t in texts]

    async def delete(self, file_name: str) -> None:
        self.deleted.append(file_name)


class FakeWriter:
    def __init__(self, questions: list[dict]):
        self.questions = questions
        self.calls: list[tuple[str, str, dict]] = []

    async def write(self, system: str, prompt_text: str, schema: dict) -> ModelReply:
        self.calls.append((system, prompt_text, schema))
        return ModelReply(self.questions, "claude-opus-5", 900, 300)


@pytest.fixture
def headers() -> dict[str, str]:
    return {
        "X-Theta-Token": SERVICE_TOKEN,
        "X-Theta-Institution-Id": "1",
        "X-Theta-User-Id": "431",
        "X-Theta-Role": "teacher",
    }


def client_for(ai: FakeAI | None = None, writer: FakeWriter | None = None, google_key: str = "") -> AsyncClient:
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(
        service_token=SERVICE_TOKEN, google_api_key=google_key, _env_file=None)
    if ai is not None:
        app.dependency_overrides[get_textbook_ai] = lambda: ai
    if writer is not None:
        app.dependency_overrides[get_writer_factory] = lambda: (lambda model=None: writer)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://theta.test")


# ----- cutting pages into passages ---------------------------------------------------------

def test_a_heading_starts_a_passage_and_names_the_ones_under_it():
    text = "# Earth is our home\nThe earth is round.\n\nIt goes round the sun.\n# Day and night\nThe earth spins."
    chunks = chunker.chunk_pages([(34, text)])
    assert [(c.page, c.order, c.heading) for c in chunks] == [(34, 1, "Earth is our home"), (34, 2, "Day and night")]
    assert chunks[0].text == "The earth is round.\nIt goes round the sun."
    assert chunks[1].for_embedding() == "Day and night\nThe earth spins."


def test_the_heading_carries_over_to_the_next_page_and_the_next_batch():
    first = chunker.chunk_pages([(1, "# Phases of the Moon\nThe moon has phases.")])
    assert chunker.heading_after([(1, "# Phases of the Moon\nThe moon has phases.")]) == "Phases of the Moon"
    later = chunker.chunk_pages([(2, "A full moon is round.")], heading="Phases of the Moon")
    assert first[0].heading == later[0].heading == "Phases of the Moon"


def test_long_text_is_cut_near_the_target_and_never_past_the_maximum():
    sentence = "Plants make their food from sunlight. "
    chunks = chunker.chunk_pages([(5, sentence * 200)])
    assert len(chunks) > 3
    assert all(len(c.text) <= chunker.MAX for c in chunks)
    assert [c.order for c in chunks] == list(range(1, len(chunks) + 1))


def test_a_tiny_tail_joins_the_passage_before_it():
    para = "Leaves are green. " * 60
    chunks = chunker.chunk_pages([(9, para + "\n\nThe end.")])
    assert chunks[-1].text.endswith("The end.")
    assert all(len(c.text) >= chunker.MIN_TAIL for c in chunks)


def test_arabic_text_is_kept_as_written():
    chunks = chunker.chunk_pages([(3, "# الأرض بيتنا\nالأرض كروية الشكل.")])
    assert chunks[0].heading == "الأرض بيتنا" and chunks[0].text == "الأرض كروية الشكل."


def test_vectors_are_unit_length():
    v = normalise([3.0, 4.0])
    assert v == [0.6, 0.8]
    assert normalise([0.0, 0.0]) == [0.0, 0.0]


# ----- the endpoints ---------------------------------------------------------------------

async def test_read_returns_pages_passages_and_one_vector_each(headers):
    ai = FakeAI([
        ReadPage(1, None, ""),
        ReadPage(2, "34", "# Earth is our home\nThe earth is round."),
        ReadPage(3, "35", "It spins once a day.\n# Day and night\nHalf the earth faces the sun."),
    ], document_pages=3)
    async with client_for(ai) as client:
        response = await client.post("/v1/textbook/read", headers=headers,
                                     json={"file_uri": "https://gemini/files/abc", "from_page": 1, "to_page": 3})
    body = response.json()
    assert response.status_code == 200, body
    assert [p["printed_page"] for p in body["pages"]] == [None, "34", "35"]
    assert [(c["page"], c["heading"]) for c in body["chunks"]] == [
        (2, "Earth is our home"), (3, "Earth is our home"), (3, "Day and night")]
    assert all(len(c["embedding"]) == 3 for c in body["chunks"])
    assert ai.embedded[0][1] == "document"
    assert ai.embedded[0][0][0] == "Earth is our home\nThe earth is round."
    assert body["at_end"] is True and body["last_heading"] == "Day and night"


async def test_read_refuses_more_than_ten_pages_or_a_backwards_range(headers):
    async with client_for(FakeAI()) as client:
        many = await client.post("/v1/textbook/read", headers=headers,
                                 json={"file_uri": "x", "from_page": 1, "to_page": 11})
        back = await client.post("/v1/textbook/read", headers=headers,
                                 json={"file_uri": "x", "from_page": 5, "to_page": 4})
    assert many.status_code == 422 and back.status_code == 422


async def test_reading_past_the_end_says_so(headers):
    async with client_for(FakeAI([], document_pages=0)) as client:
        response = await client.post("/v1/textbook/read", headers=headers,
                                     json={"file_uri": "x", "from_page": 41, "to_page": 50})
    assert response.json()["at_end"] is True and response.json()["chunks"] == []


async def test_upload_takes_a_pdf_body_only(headers):
    ai = FakeAI()
    async with client_for(ai) as client:
        ok = await client.post("/v1/textbook/upload?name=EVS%20Class%201", headers=headers, content=b"%PDF-1.7 ...")
        not_pdf = await client.post("/v1/textbook/upload", headers=headers, content=b"hello")
        empty = await client.post("/v1/textbook/upload", headers=headers, content=b"")
    assert ok.status_code == 201 and ok.json()["file_uri"] == "https://gemini/files/abc"
    assert ai.uploaded == [(12, "EVS Class 1")]
    assert not_pdf.status_code == 422 and empty.status_code == 422


async def test_embed_marks_search_text_as_a_query(headers):
    ai = FakeAI()
    async with client_for(ai) as client:
        response = await client.post("/v1/textbook/embed", headers=headers, json={"texts": ["Phases of the Moon"]})
    assert response.status_code == 200
    assert response.json()["model"] == "gemini-embedding-001" and response.json()["dimensions"] == 768
    assert ai.embedded == [(["Phases of the Moon"], "query")]


async def test_without_a_google_key_textbooks_are_off(headers):
    async with client_for() as client:
        response = await client.post("/v1/textbook/embed", headers=headers, json={"texts": ["x"]})
    assert response.status_code == 503


async def test_students_cannot_use_textbooks(headers):
    async with client_for(FakeAI()) as client:
        response = await client.post("/v1/textbook/embed", headers={**headers, "X-Theta-Role": "student"},
                                     json={"texts": ["x"]})
    assert response.status_code == 403


async def test_delete_removes_the_held_file(headers):
    ai = FakeAI()
    async with client_for(ai) as client:
        response = await client.delete("/v1/textbook/file?name=files/abc", headers=headers)
    assert response.status_code == 204 and ai.deleted == ["files/abc"]


# ----- checking uploaded questions ---------------------------------------------------------

def check_body(**overrides) -> dict:
    body = {
        "class_name": "Class 1",
        "subject_name": "Environmental Studies",
        "items": [
            {"key": "r1", "type": "mcq_single", "stem": "How many phases does the moon show in a month?",
             "options": [{"text": "4", "is_correct": True}, {"text": "8"}, {"text": "2"}],
             "passages": [{"page": 37, "text": "The moon shows eight phases in about a month."}]},
            {"key": "r2", "type": "short_answer", "stem": "What gives us day and night?",
             "passages": [{"page": 35, "text": "The earth spins, so we get day and night."}]},
            {"key": "r3", "type": "true_false", "stem": "Bees make honey.",
             "options": [{"text": "True", "is_correct": True}, {"text": "False"}]},
        ],
    }
    body.update(overrides)
    return body


async def test_check_reports_a_wrong_key_a_missing_key_and_what_the_book_does_not_cover(headers):
    writer = FakeWriter([
        {"key": "r1", "verdict": "agrees", "correct_options": [1], "accepted_answers": [], "page": 37,
         "note": "The book says  eight phases."},
        {"key": "r2", "verdict": "agrees", "correct_options": [], "accepted_answers": ["The earth spinning", "spinning"],
         "page": 35, "note": "Page 35."},
        {"key": "r3", "verdict": "agrees", "correct_options": [0], "accepted_answers": [], "page": 99, "note": ""},
        {"key": "zz", "verdict": "agrees", "correct_options": [0], "accepted_answers": [], "page": 1, "note": ""},
    ])
    async with client_for(writer=writer) as client:
        response = await client.post("/v1/textbook/check", headers=headers, json=check_body())
    body = response.json()
    assert response.status_code == 200, body
    r1, r2, r3 = body["items"]
    # it said "agrees" but backs another option than the one marked: that is a disagreement
    assert (r1["verdict"], r1["correct_options"], r1["page"], r1["note"]) == ("disagrees", [1], 37, "The book says eight phases.")
    # no accepted answers were given, so the book's answer is a suggestion
    assert (r2["verdict"], r2["accepted_answers"]) == ("no_key", ["The earth spinning", "spinning"])
    # a page not among the passages is dropped
    assert r3["page"] == 0
    system, text, schema = writer.calls[0]
    assert '<question key="r1" type="mcq_single">' in text and "Option 0: 4 (marked correct)" in text
    assert '<passage page="37">' in text and "(no textbook passage was found for this question)" in text


async def test_a_skipped_question_is_reported_unchecked_not_guessed(headers):
    async with client_for(writer=FakeWriter([])) as client:
        response = await client.post("/v1/textbook/check", headers=headers, json=check_body())
    assert [i["verdict"] for i in response.json()["items"]] == ["not_in_book"] * 3


async def test_check_needs_distinct_keys(headers):
    body = check_body()
    body["items"][1]["key"] = "r1"
    async with client_for(writer=FakeWriter([])) as client:
        response = await client.post("/v1/textbook/check", headers=headers, json=body)
    assert response.status_code == 422


# ----- writing questions from the book ------------------------------------------------------

async def test_generation_writes_from_the_passages_and_names_the_page(headers):
    writer = FakeWriter([
        {"type": "mcq_single", "stem": "How many phases does the moon show?",
         "options": [{"text": "8", "is_correct": True}, {"text": "4", "is_correct": False},
                     {"text": "2", "is_correct": False}],
         "accepted_answers": [], "difficulty": "easy", "bloom_level": "remember", "source_page": 37},
        {"type": "mcq_single", "stem": "What makes day and night?",
         "options": [{"text": "The earth spinning", "is_correct": True}, {"text": "The moon", "is_correct": False},
                     {"text": "Clouds", "is_correct": False}],
         "accepted_answers": [], "difficulty": "easy", "bloom_level": "remember", "source_page": 12},
    ])
    body = {
        "class_name": "Class 1", "subject_name": "Environmental Studies", "chapter": "Earth is our home",
        "count": 2, "types": ["mcq_single"], "difficulty_counts": {"easy": 2},
        "source_passages": [{"page": 37, "text": "The moon shows eight phases."},
                            {"page": 35, "text": "The earth spins, so we get day and night."}],
    }
    async with client_for(writer=writer) as client:
        response = await client.post("/v1/generate/questions", headers=headers, json=body)
    out = response.json()
    assert response.status_code == 200, out
    assert [q["source_page"] for q in out["questions"]] == [37, None]
    _, text, schema = writer.calls[0]
    assert '<textbook_passages>\n<passage page="37">\nThe moon shows eight phases.\n</passage>' in text
    assert "source_page" in schema["properties"]["questions"]["items"]["required"]
    assert "textbook passages are given" in prompt.SYSTEM
