"""AI marking: the short-answer verdict (quick, then careful when unsure), the mark
scheme fitted to the question's marks, and the double (or triple) marking of a
written answer. No test here calls the Anthropic API."""

from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings, get_settings
from app.core.errors import BadGateway
from app.main import create_app
from app.marking import prompt
from app.marking.marker import ClaudeMarker, MarkRefused, MarkReply, get_marker
from tests.conftest import SERVICE_TOKEN


class FakeMarker:
    """Answers each call with the next queued reply (a dict, or MarkRefused to decline)."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls: list[dict] = []

    async def ask(self, system, prompt_text, schema, effort, timeout):
        self.calls.append({"system": system, "prompt": prompt_text, "schema": schema, "effort": effort})
        reply = self.replies.pop(0)
        if reply is MarkRefused:
            raise MarkRefused()
        return MarkReply(reply, "claude-opus-5")


@pytest.fixture
def headers() -> dict[str, str]:
    return {
        "X-Theta-Token": SERVICE_TOKEN,
        "X-Theta-Institution-Id": "12",
        "X-Theta-User-Id": "345",
        "X-Theta-Role": "student",
    }


def client_for(marker: FakeMarker | None = None, api_key: str = "") -> AsyncClient:
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(
        service_token=SERVICE_TOKEN, anthropic_api_key=api_key, _env_file=None)
    if marker is not None:
        app.dependency_overrides[get_marker] = lambda: marker
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://theta.test")


async def post(path: str, body: dict, headers: dict, marker: FakeMarker | None = None):
    async with client_for(marker) as client:
        return await client.post(path, json=body, headers=headers)


def short_body(**overrides) -> dict:
    body = {"question": "What is the longest side of a right-angled triangle called?",
            "accepted": ["hypotenuse", "the hypotenuse"], "answer": "the side opposite the right angle"}
    body.update(overrides)
    return body


SCHEME = [{"text": "Names the parallel line through a vertex.", "marks": 1},
          {"text": "Uses alternate angles.", "marks": 2},
          {"text": "Concludes the angles add up to 180°.", "marks": 1}]


def long_body(**overrides) -> dict:
    body = {"question": "Explain why the angles of a triangle add up to 180°.",
            "model_answer": "Draw a line through one vertex parallel to the opposite side...",
            "points": SCHEME,
            "answer": "Draw a parallel line through the top. The alternate angles are equal. So they make a straight line, 180."}
    body.update(overrides)
    return body


def marking(awarded: list[float], feedback: str = "Good.", evidence: list[str] | None = None) -> dict:
    evidence = evidence or [""] * len(awarded)
    return {"points": [{"index": i, "awarded": a, "evidence": e} for i, (a, e) in enumerate(zip(awarded, evidence))],
            "feedback": feedback}


# ----- short answers -------------------------------------------------------------------

async def test_a_sure_quick_check_decides(headers):
    marker = FakeMarker({"verdict": "correct", "reason": "Same meaning as 'hypotenuse'."})
    response = await post("/v1/mark/short-answer", short_body(), headers, marker)
    assert response.status_code == 200, response.json()
    assert response.json() == {"correct": True, "reason": "Same meaning as 'hypotenuse'.", "checked_twice": False,
                               "model": "claude-opus-5"}
    call = marker.calls[0]
    assert call["effort"] == "low" and call["schema"] == prompt.SHORT_FIRST_SCHEMA
    assert "<student_answer>\nthe side opposite the right angle\n</student_answer>" in call["prompt"]
    assert "- hypotenuse\n- the hypotenuse" in call["prompt"]


async def test_an_unsure_quick_check_is_decided_by_a_careful_one(headers):
    marker = FakeMarker({"verdict": "unsure", "reason": "?"}, {"verdict": "incorrect", "reason": "Different side."})
    body = (await post("/v1/mark/short-answer", short_body(tolerance=0.1, case_sensitive=True, language="ar"),
                       headers, marker)).json()
    assert body["correct"] is False and body["checked_twice"] is True and body["reason"] == "Different side."
    careful = marker.calls[1]
    assert careful["effort"] == "high" and careful["schema"] == prompt.SHORT_FINAL_SCHEMA
    assert "A first check could not decide" in careful["prompt"]
    assert "A number within 0.1 of an accepted number is right." in careful["prompt"]
    assert "Capital letters must match." in careful["prompt"]
    assert "Write the reason in Arabic." in careful["prompt"]


async def test_a_declined_short_answer_is_wrong_with_a_plain_reason(headers):
    body = (await post("/v1/mark/short-answer", short_body(language="ar"), headers, FakeMarker(MarkRefused))).json()
    assert body["correct"] is False and body["reason"] == "تعذّر التحقق من الإجابة تلقائياً."


@pytest.mark.parametrize("overrides", [
    {"accepted": []}, {"accepted": ["x"] * 11}, {"answer": " "}, {"answer": "x" * 2001},
    {"tolerance": -1}, {"language": "fr"}, {"question": ""},
])
async def test_bad_short_requests_are_422(headers, overrides):
    response = await post("/v1/mark/short-answer", short_body(**overrides), headers, FakeMarker())
    assert response.status_code == 422


async def test_marking_without_a_key_is_503(headers):
    async with client_for() as client:
        response = await client.post("/v1/mark/short-answer", json=short_body(), headers=headers)
    assert response.status_code == 503


async def test_marking_needs_the_service_token(headers):
    headers["X-Theta-Token"] = "wrong"
    response = await post("/v1/mark/short-answer", short_body(), headers, FakeMarker())
    assert response.status_code == 401


# ----- mark schemes ---------------------------------------------------------------------

async def test_the_scheme_is_fitted_to_the_question_marks(headers):
    marker = FakeMarker({"points": [{"text": "A", "marks": 1}, {"text": "B", "marks": 1}, {"text": "C", "marks": 2},
                                    {"text": " ", "marks": 1}, {"text": "D", "marks": -1}]})
    body = (await post("/v1/mark/rubric", {"question": "Explain.", "model_answer": "Because.", "marks": 5},
                       headers, marker)).json()
    assert [(p["text"], p["marks"]) for p in body["points"]] == [("A", 1.5), ("B", 1.5), ("C", 2.0)]
    assert body["total"] == 5 and sum(p["marks"] for p in body["points"]) == 5
    assert "The question is worth 5 marks." in marker.calls[0]["prompt"]
    assert marker.calls[0]["effort"] == "medium"


async def test_an_unusable_scheme_becomes_one_point(headers):
    body = (await post("/v1/mark/rubric", {"question": "Explain.", "marks": 2, "language": "ar"},
                       headers, FakeMarker({"points": []}))).json()
    assert body["points"] == [{"text": "إجابة كاملة وصحيحة.", "marks": 2.0}]


# ----- long answers ---------------------------------------------------------------------

async def test_two_markings_that_agree_are_averaged(headers):
    marker = FakeMarker(
        marking([1, 2, 1], "Clear and complete.", ["Draw a parallel line through the top.", "The alternate angles are equal.", ""]),
        marking([1, 1, 1], "Good, but explain the angles.", ["", "", "they make a straight line"]),
    )
    body = (await post("/v1/mark/long-answer", long_body(), headers, marker)).json()
    assert [p["awarded"] for p in body["points"]] == [1, 1.5, 1]
    assert body["total"] == 3.5 and body["max"] == 4 and body["markings"] == 2
    assert [p["evidence"] for p in body["points"]] == [
        "Draw a parallel line through the top.", "The alternate angles are equal.", "they make a straight line"]
    assert len(marker.calls) == 2 and all(c["effort"] == "high" for c in marker.calls)
    assert "<mark_scheme>\n0. 1: Names the parallel line through a vertex.\n1. 2: Uses alternate angles." in marker.calls[0]["prompt"]


async def test_markings_that_disagree_get_a_third_and_the_middle_one_counts(headers):
    marker = FakeMarker(marking([1, 2, 1], "High."), marking([0, 0, 0], "Low."), marking([1, 1, 0], "Middle."))
    body = (await post("/v1/mark/long-answer", long_body(), headers, marker)).json()
    assert body["markings"] == 3 and body["total"] == 2 and body["feedback"] == "Middle."
    assert [p["awarded"] for p in body["points"]] == [1, 1, 0]


async def test_marks_are_kept_inside_each_point_and_evidence_must_be_the_students(headers):
    marker = FakeMarker(
        {"points": [{"index": 0, "awarded": 5, "evidence": "made up words"}, {"index": 1, "awarded": 0.3, "evidence": ""},
                    {"index": 1, "awarded": 2, "evidence": ""}, {"index": 7, "awarded": 1, "evidence": ""}], "feedback": "x"},
        {"points": [{"index": 0, "awarded": 1, "evidence": ""}, {"index": 1, "awarded": 0.5, "evidence": ""}], "feedback": "y"},
    )
    body = (await post("/v1/mark/long-answer", long_body(), headers, marker)).json()
    # First: point 0 capped at 1, point 1 0.3 → 0.5 (first entry wins), point 2 missing → 0.
    assert [p["awarded"] for p in body["points"]] == [1, 0.5, 0]
    assert body["points"][0]["evidence"] == ""


async def test_a_declined_long_answer_gets_no_marks(headers):
    body = (await post("/v1/mark/long-answer", long_body(), headers, FakeMarker(MarkRefused, MarkRefused))).json()
    assert body["total"] == 0 and body["markings"] == 0 and body["feedback"] == "This answer could not be marked automatically."


# ----- the Claude call ------------------------------------------------------------------

class FakeMessages:
    def __init__(self, message):
        self.message = message
        self.kwargs = None

    async def create(self, **kwargs):
        self.kwargs = kwargs
        return self.message


def fake_client(message):
    messages = FakeMessages(message)
    client = SimpleNamespace(beta=SimpleNamespace(messages=messages))
    client.with_options = lambda **_: client
    return client, messages


def message(text: str | None, stop_reason: str = "end_turn"):
    content = [] if text is None else [SimpleNamespace(type="text", text=text)]
    return SimpleNamespace(content=content, stop_reason=stop_reason, stop_details=None, model="claude-opus-5")


async def test_claude_marker_sends_effort_schema_and_fallbacks():
    client, messages = fake_client(message('{"verdict": "correct", "reason": "ok"}'))
    reply = await ClaudeMarker(client, "claude-opus-5").ask("sys", "user", prompt.SHORT_FIRST_SCHEMA, "low", 5)
    assert reply.data == {"verdict": "correct", "reason": "ok"}
    assert messages.kwargs["output_config"] == {"effort": "low", "format": {"type": "json_schema", "schema": prompt.SHORT_FIRST_SCHEMA}}
    assert messages.kwargs["fallbacks"] == "default" and messages.kwargs["model"] == "claude-opus-5"


async def test_claude_marker_reports_a_decline():
    client, _ = fake_client(message(None, stop_reason="refusal"))
    with pytest.raises(MarkRefused):
        await ClaudeMarker(client, "claude-opus-5").ask("sys", "user", {}, "low", 5)


@pytest.mark.parametrize("reply", [message("{", stop_reason="max_tokens"), message("nope"), message(None)])
async def test_claude_marker_turns_unusable_answers_into_502(reply):
    client, _ = fake_client(reply)
    with pytest.raises(BadGateway):
        await ClaudeMarker(client, "claude-opus-5").ask("sys", "user", {}, "low", 5)
