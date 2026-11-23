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

    async def ask(self, system, prompt_text, schema, effort, timeout, quick=False):
        self.calls.append({"system": system, "prompt": prompt_text, "schema": schema, "effort": effort, "quick": quick})
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
    body = (await post("/v1/mark/short-answer", short_body(language="ar"), headers, FakeMarker(MarkRefused, MarkRefused))).json()
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
    client = SimpleNamespace(beta=SimpleNamespace(messages=messages), messages=messages)
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


async def test_the_quick_check_uses_the_quick_model_without_fallbacks():
    client, messages = fake_client(message('{"verdict": "correct", "reason": "ok"}'))
    await ClaudeMarker(client, "claude-opus-5", "claude-haiku-5-5").ask("sys", "user", prompt.SHORT_FIRST_SCHEMA, "low", 5, quick=True)
    assert messages.kwargs["model"] == "claude-haiku-5-5"
    assert "fallbacks" not in messages.kwargs and "betas" not in messages.kwargs


async def test_a_declined_quick_check_goes_to_the_careful_check(headers):
    marker = FakeMarker(MarkRefused, {"verdict": "correct", "reason": "Same meaning."})
    body = (await post("/v1/mark/short-answer", short_body(), headers, marker)).json()
    assert body["correct"] is True and body["checked_twice"] is True
    assert [c["quick"] for c in marker.calls] == [True, False]


async def test_claude_marker_reports_a_decline():
    client, _ = fake_client(message(None, stop_reason="refusal"))
    with pytest.raises(MarkRefused):
        await ClaudeMarker(client, "claude-opus-5").ask("sys", "user", {}, "low", 5)


@pytest.mark.parametrize("reply", [message("{", stop_reason="max_tokens"), message("nope"), message(None)])
async def test_claude_marker_turns_unusable_answers_into_502(reply):
    client, _ = fake_client(reply)
    with pytest.raises(BadGateway):
        await ClaudeMarker(client, "claude-opus-5").ask("sys", "user", {}, "low", 5)


# ----- a test's results in plain words -------------------------------------------------------

def summary_body(**overrides) -> dict:
    body = {
        "test_title": "Environmental Studies - Subject test 2",
        "subject_name": "Environmental Studies",
        "students": 119, "sat": 114, "absent": 5,
        "average": 518, "previous_average": 509, "lowest": 324, "highest": 713,
        "beginning": 17, "developing": 40, "proficient": 45, "advanced": 12,
        "chapters": [{"name": "Not enough time to have eyes", "percent_right": 46, "answers": 210}],
        "divisions": [{"name": "1C", "average": 470, "weak_chapters": ["Not enough time to have eyes"]}],
        "odd_questions": 2,
        "language": "en",
    }
    body.update(overrides)
    return body


async def test_a_test_summary_is_written_from_the_figures(headers):
    marker = FakeMarker({"headline": "Most of the class is **on track**.",
                         "went_well": ["Strong in forests.", "", "B is ahead.", "Third point."],
                         "needs_attention": ["Chapter 3 needs another look."],
                         "tip": "Revise chapter 3 with group C."})
    response = await post("/v1/mark/test-summary", summary_body(), headers, marker)
    assert response.status_code == 200
    data = response.json()
    assert data["headline"] == "Most of the class is on track."
    assert data["went_well"] == ["Strong in forests.", "B is ahead."]
    assert data["needs_attention"] == ["Chapter 3 needs another look."]
    assert data["tip"] == "Revise chapter 3 with group C."
    assert data["summary"].startswith("Most of the class is on track. Strong in forests.")
    assert data["model"] == "claude-opus-5"
    call = marker.calls[0]
    assert call["effort"] == "low"
    assert "Previous test's average: 509" in call["prompt"]
    assert "1C: average 470; weak: Not enough time to have eyes" in call["prompt"]
    assert "Write the summary in English." in call["prompt"]


async def test_a_declined_test_summary_comes_back_empty(headers):
    response = await post("/v1/mark/test-summary", summary_body(language="ar"), headers, FakeMarker(MarkRefused))
    assert response.status_code == 200
    assert response.json()["summary"] == ""


@pytest.mark.parametrize("overrides", [{"average": 900}, {"students": -1}, {"language": "fr"}, {"test_title": ""}])
async def test_bad_test_summary_requests_are_422(headers, overrides):
    response = await post("/v1/mark/test-summary", summary_body(**overrides), headers, FakeMarker())
    assert response.status_code == 422


# ----- one student's progress in a teacher's words ---------------------------------------------

def comment_body(**overrides) -> dict:
    body = {"subject_name": "Environmental Studies",
            "tests": [{"score": 495, "level": "proficient"}, {"score": 535, "level": "proficient"}],
            "latest_level": "proficient", "strengths": ["In the house & in the forest"],
            "focus": ["Not enough time to have eyes"], "language": "en"}
    body.update(overrides)
    return body


async def test_a_student_comment_names_no_one_and_keeps_case(headers):
    marker = FakeMarker({"comment": "[NAME] has made **steady** progress this term."})
    response = await post("/v1/mark/student-comment", comment_body(), headers, marker)
    assert response.status_code == 200
    assert response.json()["comment"] == "[NAME] has made steady progress this term."
    assert "Scores, oldest first: 495 (proficient), 535 (proficient)" in marker.calls[0]["prompt"]


@pytest.mark.parametrize("overrides", [{"tests": []}, {"tests": [{"score": 100}]}, {"latest_level": "expert"}])
async def test_bad_student_comment_requests_are_422(headers, overrides):
    response = await post("/v1/mark/student-comment", comment_body(**overrides), headers, FakeMarker())
    assert response.status_code == 422
