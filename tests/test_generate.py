"""Question writing: the rules each draft must pass, the endpoint's gates, and how
Claude's answers (declines, cut-offs, bad JSON) are turned into clear errors.
No test here calls the Anthropic API."""

from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from app.bank import rules
from app.core.config import Settings, get_settings
from app.core.errors import BadGateway, Unprocessable
from app.generation import prompt
from app.generation.writer import ClaudeWriter, ModelReply, get_writer_factory
from app.main import create_app
from tests.conftest import SERVICE_TOKEN


def mcq(stem="What is 2 + 3?", options=None, **extra) -> dict:
    return {
        "type": "mcq_single",
        "stem": stem,
        "options": options if options is not None else [
            {"text": "4", "is_correct": False},
            {"text": "5", "is_correct": True},
            {"text": "6", "is_correct": False},
            {"text": "23", "is_correct": False},
        ],
        "accepted_answers": [], "model_answer": "",
        "explanation": "Counting on 3 from 2 gives 5.",
        "difficulty": "easy", "bloom_level": "remember",
        **extra,
    }


def blank(qtype: str, stem: str, **fields) -> dict:
    return {**mcq(stem=stem, options=[]), "type": qtype, **fields}


class FakeWriter:
    def __init__(self, questions: list[dict]):
        self.questions = questions
        self.calls: list[tuple[str, str, dict]] = []

    async def write(self, system: str, prompt_text: str, schema: dict) -> ModelReply:
        self.calls.append((system, prompt_text, schema))
        return ModelReply(self.questions, "claude-opus-5", 1200, 3400)


def request(**overrides) -> dict:
    body = {
        "class_name": "Grade 8",
        "subject_name": "Mathematics",
        "chapter": "Algebra",
        "unit": "Linear equations",
        "count": 5,
        "types": ["mcq_single", "true_false", "short_answer", "long_answer"],
        "difficulty_counts": {"easy": 2, "medium": 2, "hard": 1},
    }
    body.update(overrides)
    return body


@pytest.fixture
def headers() -> dict[str, str]:
    return {
        "X-Theta-Token": SERVICE_TOKEN,
        "X-Theta-Institution-Id": "12",
        "X-Theta-User-Id": "345",
        "X-Theta-Role": "teacher",
    }


def client_for(writer: FakeWriter | None = None, api_key: str = "", google_key: str = "") -> AsyncClient:
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(
        service_token=SERVICE_TOKEN, anthropic_api_key=api_key, google_api_key=google_key, _env_file=None)
    if writer is not None:
        # The endpoint asks for a factory, because the model to write with arrives in the body.
        app.dependency_overrides[get_writer_factory] = lambda: (lambda model=None: writer)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://theta.test")


async def post(writer: FakeWriter, headers: dict, **overrides):
    async with client_for(writer) as client:
        return await client.post("/v1/generate/questions", json=request(**overrides), headers=headers)


# ----- the endpoint -------------------------------------------------------------

async def test_every_type_comes_back_in_the_bank_shape(headers):
    writer = FakeWriter([
        mcq(),
        blank("true_false", "Zero is an even number.", options=[
            {"text": "True", "is_correct": True}, {"text": "False", "is_correct": False}]),
        blank("short_answer", "A car travels 150 km in 2 hours. What is its speed?",
              accepted_answers=["75", "75 km/h"]),
        blank("short_answer", "What do we call a number with exactly two factors?",
              accepted_answers=["prime", " Prime ", "a prime number", ""]),
        blank("long_answer", "Explain why $x^2 \\geq 0$ for every real $x$.",
              model_answer="A square of a positive or negative number is positive; $0^2 = 0$."),
    ])
    response = await post(writer, headers)
    body = response.json()
    assert response.status_code == 200, body
    assert body["rejected"] == []
    assert body["model"] == "claude-opus-5"
    assert (body["input_tokens"], body["output_tokens"]) == (1200, 3400)
    single, tf, numeric, short, long = body["questions"]
    assert single["answer_key"] is None
    assert [o["is_correct"] for o in single["options"]] == [False, True, False, False]
    assert tf["options"][0] == {"text": "True", "is_correct": True}
    assert numeric["options"] == [] and numeric["type"] == "short_answer"
    assert numeric["answer_key"]["accepted"] == ["75", "75 km/h"]
    assert short["answer_key"]["accepted"] == ["prime", "a prime number"]
    assert short["answer_key"]["case_sensitive"] is False
    assert long["answer_key"]["model_answer"].startswith("A square")


async def test_the_prompt_carries_the_request(headers):
    writer = FakeWriter([mcq()])
    await post(writer, headers, language="ar", instructions="Word problems only",
               avoid=["Solve 2x + 3 = 7."], bloom_levels=["apply", "apply"], count=1,
               difficulty_counts={"easy": 1})
    system, text, schema = writer.calls[0]
    assert system == prompt.SYSTEM and schema == prompt.SCHEMA
    assert "Chapter: Algebra" in text and "Unit: Linear equations" in text
    assert "Language: Arabic" in text
    assert "<teacher_notes>\nWord problems only\n</teacher_notes>" in text
    assert "- Solve 2x + 3 = 7." in text
    assert "Bloom levels: apply." in text
    assert "Difficulty: 1 easy. Label each question with its difficulty." in text


async def test_the_prompt_names_the_difficulty_mix(headers):
    writer = FakeWriter([mcq()])
    await post(writer, headers)
    _, text, _ = writer.calls[0]
    assert "Difficulty: 2 easy, 2 medium and 1 hard. Label each question with its difficulty." in text


async def test_a_subunit_narrows_the_prompt(headers):
    writer = FakeWriter([mcq()])
    await post(writer, headers, subunit="Word problems", count=1, difficulty_counts={"medium": 1})
    _, text, _ = writer.calls[0]
    assert "Unit: Linear equations" in text
    assert "Sub-unit: Word problems. Every question is about this sub-unit." in text


async def test_questions_that_break_the_rules_are_rejected_with_the_reason(headers):
    writer = FakeWriter([
        mcq(stem="Pick the even number.", options=[
            {"text": "2", "is_correct": True}, {"text": "2", "is_correct": False}]),
        mcq(stem="Pick the primes.", options=[
            {"text": "2", "is_correct": True}, {"text": "3", "is_correct": True}, {"text": "4", "is_correct": False}]),
        mcq(stem="What is $x + 1?"),
        blank("mcq_multi", "Which are odd?"),
        blank("numeric", "How many sides has a square?", accepted_answers=["4"]),
    ])
    body = (await post(writer, headers)).json()
    assert body["questions"] == []
    assert [r["reason"] for r in body["rejected"]] == [
        "Two options have the same text.",
        "Mark exactly one option as correct.",
        "A formula in the question text is not closed.",
        "It is a mcq_multi question, which was not asked for.",
        "It is a numeric question, which was not asked for.",
    ]
    assert [r["index"] for r in body["rejected"]] == [0, 1, 2, 3, 4]
    assert body["rejected"][0]["stem"] == "Pick the even number."


async def test_repeats_of_the_bank_or_the_batch_are_rejected(headers):
    writer = FakeWriter([
        mcq(stem="Solve 2x + 3 = 7"),       # the bank has it, with other spacing and a full stop
        mcq(stem="What is 2 + 3?"),
        mcq(stem="what is 2 + 3 ?"),        # same as the one before
    ])
    body = (await post(writer, headers, avoid=["Solve  2x + 3 = 7."])).json()
    assert [q["stem"] for q in body["questions"]] == ["What is 2 + 3?"]
    assert [r["index"] for r in body["rejected"]] == [0, 2]
    assert all(r["reason"] == "It repeats a question already in the bank or in this batch." for r in body["rejected"])


async def test_a_level_not_asked_for_is_rejected_and_extras_are_dropped(headers):
    writer = FakeWriter([mcq(stem="Q0?", difficulty="medium"), mcq(stem="Q1?", difficulty="hard"),
                         mcq(stem="Q2?", difficulty="medium"), mcq(stem="Q3?", difficulty="medium")])
    body = (await post(writer, headers, count=3, difficulty_counts={"medium": 3})).json()
    assert [q["stem"] for q in body["questions"]] == ["Q0?", "Q2?"]
    assert [(r["index"], r["reason"]) for r in body["rejected"]] == [(1, "It is hard, which was not asked for.")]


async def test_not_configured_is_503_but_only_after_the_token(headers):
    async with client_for(api_key="") as client:
        response = await client.post("/v1/generate/questions", json=request(), headers=headers)
        assert response.status_code == 503
        assert "no Anthropic API key" in response.json()["detail"]
        headers["X-Theta-Token"] = "wrong"
        response = await client.post("/v1/generate/questions", json=request(), headers=headers)
        assert response.status_code == 401


async def test_only_staff_may_generate(headers):
    headers["X-Theta-Role"] = "student"
    response = await post(FakeWriter([mcq()]), headers)
    assert response.status_code == 403


@pytest.mark.parametrize("overrides", [
    {"count": 0}, {"count": 11}, {"types": []}, {"types": ["essay"]}, {"types": ["numeric"]},
    {"types": ["mcq_single", "numeric"]},
    {"difficulty_counts": {"easy": 6}}, {"difficulty_counts": {"easy": 6, "hard": -1}},
    {"difficulty_counts": None}, {"language": "fr"}, {"chapter": " "},
    {"instructions": "x" * 1001},
])
async def test_bad_requests_are_422(headers, overrides):
    response = await post(FakeWriter([mcq()]), headers, **overrides)
    assert response.status_code == 422


# ----- the Claude call ------------------------------------------------------------

class FakeStream:
    def __init__(self, message):
        self.message = message
        self.kwargs = None

    def stream(self, **kwargs):
        self.kwargs = kwargs
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get_final_message(self):
        return self.message


def fake_client(message) -> tuple[SimpleNamespace, FakeStream]:
    stream = FakeStream(message)
    return SimpleNamespace(beta=SimpleNamespace(messages=stream)), stream


def message(text: str | None, stop_reason: str = "end_turn"):
    content = [] if text is None else [SimpleNamespace(type="text", text=text)]
    return SimpleNamespace(
        content=content, stop_reason=stop_reason, stop_details=None, model="claude-opus-5",
        usage=SimpleNamespace(input_tokens=10, output_tokens=20))


async def test_claude_writer_reads_the_json_and_sends_the_schema():
    client, stream = fake_client(message('{"questions": [{"type": "mcq_single"}, "junk"]}'))
    reply = await ClaudeWriter(client, "claude-opus-5").write("sys", "user", prompt.SCHEMA)
    assert reply.questions == [{"type": "mcq_single"}]
    assert (reply.model, reply.input_tokens, reply.output_tokens) == ("claude-opus-5", 10, 20)
    assert stream.kwargs["output_config"] == {"format": {"type": "json_schema", "schema": prompt.SCHEMA}}
    assert stream.kwargs["fallbacks"] == "default"
    assert stream.kwargs["messages"] == [{"role": "user", "content": "user"}]


async def test_claude_writer_turns_a_decline_into_422():
    client, _ = fake_client(message(None, stop_reason="refusal"))
    with pytest.raises(Unprocessable):
        await ClaudeWriter(client, "claude-opus-5").write("sys", "user", prompt.SCHEMA)


@pytest.mark.parametrize("reply", [
    message('{"questions": [', stop_reason="max_tokens"),
    message("not json"),
    message('{"items": []}'),
    message(None),
])
async def test_claude_writer_turns_unusable_answers_into_502(reply):
    client, _ = fake_client(reply)
    with pytest.raises(BadGateway):
        await ClaudeWriter(client, "claude-opus-5").write("sys", "user", prompt.SCHEMA)


# ----- rules ------------------------------------------------------------------------

@pytest.mark.parametrize(("text", "closed"), [
    (None, True), ("", True), ("plain", True), ("$x$ and $y$", True), ("$$x^2$$", True),
    ("costs \\$5", True), ("$x", False), ("$$x$", False), ("$a$ and $b", False),
])
def test_math_is_closed(text, closed):
    assert rules.math_is_closed(text) is closed
