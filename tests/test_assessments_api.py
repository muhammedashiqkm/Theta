"""Adaptive tests end to end against a real PostgreSQL: teacher publishes, students sit."""

from datetime import UTC, datetime, timedelta

from tests.test_bank_api import CLASS, SUBJECT, mcq

STUDENT, OTHER_STUDENT = 9001, 9002
SECTION = {"class_group_division_id": 501, "group_id": 1, "division_id": 1, "label": "Grade 8 · A"}


def iso(delta: timedelta) -> str:
    return (datetime.now(UTC) + delta).isoformat()


def student_headers(bank, student_id: int = STUDENT, role: str = "student") -> dict:
    headers = bank.headers(scope=None, role=role, user=77)
    headers["X-Theta-Student-Id"] = str(student_id)
    return headers


async def build_bank(bank, per_chapter: int = 6, question_type: str = "mcq_single") -> list[int]:
    """Two chapters (one with a unit) holding approved questions of mixed difficulty."""
    imported = await bank.client.post(
        "/v1/topics/import", headers=bank.headers(),
        json={"class_id": CLASS, "subject_id": SUBJECT, "nodes": [
            {"source_id": 1, "parent_source_id": 0, "level": 1, "name": "Fractions", "order": 1},
            {"source_id": 2, "parent_source_id": 0, "level": 1, "name": "Decimals", "order": 2},
            {"source_id": 3, "parent_source_id": 1, "level": 2, "name": "Adding fractions", "order": 1},
        ]},
    )
    topics = imported.json()["topics"]
    chapters = [t["id"] for t in topics if t["level"] == "chapter"]
    unit = next(t["id"] for t in topics if t["level"] == "unit")
    for chapter_index, chapter in enumerate(chapters):
        for i in range(per_chapter):
            topic = unit if chapter_index == 0 and i % 2 else chapter
            body = mcq(topic, difficulty=("easy", "medium", "hard")[i % 3], stem=f"Chapter {chapter_index} question {i}")
            if question_type == "long_answer":
                body = {**body, "type": "long_answer", "options": []}
            created = await bank.client.post("/v1/questions", headers=bank.headers(), json=body)
            qid = created.json()["question"]["id"]
            await bank.client.post(f"/v1/questions/{qid}/status", headers=bank.headers(), json={"status": "pilot"})
    return chapters


def draft_payload(chapter_ids: list[int], **overrides) -> dict:
    return {
        "class_id": CLASS,
        "subject_id": SUBJECT,
        "title": "Unit 3 check",
        "instructions": "Answer carefully.",
        "starts_at": iso(timedelta(hours=-1)),
        "ends_at": iso(timedelta(hours=2)),
        "duration_minutes": 30,
        "min_questions": 3,
        "max_questions": 5,
        "target_se": 0.2,
        "result_policy": "immediate",
        "chapters": [{"topic_id": chapter_ids[0], "share": 60}, {"topic_id": chapter_ids[1], "share": 40}],
        "sections": [SECTION],
        **overrides,
    }


ROSTER = {"candidates": [
    {"student_id": STUDENT, "name": "Aisha M.", "roll_no": "1", "class_group_division_id": 501},
    {"student_id": OTHER_STUDENT, "name": "Omar K.", "roll_no": "2", "class_group_division_id": 501},
]}


async def published_test(bank, **overrides) -> dict:
    chapters = await build_bank(bank)
    created = await bank.client.post("/v1/assessments", headers=bank.headers(), json=draft_payload(chapters, **overrides))
    assert created.status_code == 201, created.text
    published = await bank.client.post(f"/v1/assessments/{created.json()['id']}/publish", headers=bank.headers(), json=ROSTER)
    assert published.status_code == 200, published.text
    return published.json()


async def correct_answer(bank, question_id: int) -> dict:
    detail = (await bank.client.get(f"/v1/questions/{question_id}", headers=bank.headers())).json()
    return {"positions": [o["position"] for o in detail["options"] if o["is_correct"]]}


# ----- teacher ----------------------------------------------------------------

async def test_payload_validation(bank):
    chapters = await build_bank(bank, per_chapter=1)
    bad_shares = draft_payload(chapters, chapters=[{"topic_id": chapters[0], "share": 50}, {"topic_id": chapters[1], "share": 30}])
    assert (await bank.client.post("/v1/assessments", headers=bank.headers(), json=bad_shares)).status_code == 422
    backwards = draft_payload(chapters, starts_at=iso(timedelta(hours=3)), ends_at=iso(timedelta(hours=1)))
    assert (await bank.client.post("/v1/assessments", headers=bank.headers(), json=backwards)).status_code == 422
    unit_as_chapter = await bank.client.get("/v1/topics", headers=bank.headers(), params={"class_id": CLASS, "subject_id": SUBJECT})
    unit = next(t["id"] for t in unit_as_chapter.json() if t["level"] == "unit")
    not_chapter = draft_payload(chapters, chapters=[{"topic_id": unit, "share": 100}])
    assert (await bank.client.post("/v1/assessments", headers=bank.headers(), json=not_chapter)).status_code == 409


async def test_publish_needs_questions_in_every_chapter(bank):
    chapters = await build_bank(bank, per_chapter=2)
    too_demanding = (await bank.client.post("/v1/assessments", headers=bank.headers(), json=draft_payload(chapters, min_questions=10, max_questions=10))).json()
    refused = await bank.client.post(f"/v1/assessments/{too_demanding['id']}/publish", headers=bank.headers(), json=ROSTER)
    assert refused.status_code == 409 and "at least 10" in refused.text

    empty = await bank.client.post(
        "/v1/topics", headers=bank.headers(), json={"class_id": CLASS, "subject_id": SUBJECT, "name": "Empty chapter"}
    )
    with_empty = draft_payload(chapters, min_questions=1, chapters=[{"topic_id": chapters[0], "share": 50}, {"topic_id": empty.json()["id"], "share": 50}])
    draft = (await bank.client.post("/v1/assessments", headers=bank.headers(), json=with_empty)).json()
    refused = await bank.client.post(f"/v1/assessments/{draft['id']}/publish", headers=bank.headers(), json=ROSTER)
    assert refused.status_code == 409 and "Empty chapter" in refused.text


async def test_only_drafts_change_and_detail_reports_the_pool(bank):
    chapters = await build_bank(bank)
    draft = (await bank.client.post("/v1/assessments", headers=bank.headers(), json=draft_payload(chapters))).json()
    assert draft["status"] == "draft"
    assert draft["pool"] == {"total": 12, "easy": 4, "medium": 4, "hard": 4}
    assert [c["eligible_questions"] for c in draft["chapters"]] == [6, 6]

    renamed = await bank.client.put(f"/v1/assessments/{draft['id']}", headers=bank.headers(), json=draft_payload(chapters, title="Renamed"))
    assert renamed.json()["title"] == "Renamed"

    await bank.client.post(f"/v1/assessments/{draft['id']}/publish", headers=bank.headers(), json=ROSTER)
    assert (await bank.client.put(f"/v1/assessments/{draft['id']}", headers=bank.headers(), json=draft_payload(chapters))).status_code == 409
    assert (await bank.client.delete(f"/v1/assessments/{draft['id']}", headers=bank.headers())).status_code == 409

    listed = (await bank.client.get("/v1/assessments", headers=bank.headers(), params={"class_id": CLASS, "subject_id": SUBJECT})).json()
    assert [(a["status"], a["is_open"], a["candidates"], a["sections"]) for a in listed] == [("published", True, 2, ["Grade 8 · A"])]


# ----- student ----------------------------------------------------------------

async def test_a_student_sits_an_adaptive_test(bank):
    test = await published_test(bank)

    mine = (await bank.client.get("/v1/student/tests", headers=student_headers(bank))).json()
    assert [(t["availability"], t["session_status"]) for t in mine] == [("open", "not_started")]

    view = await bank.client.post(f"/v1/student/tests/{test['id']}/start", headers=student_headers(bank))
    assert view.status_code == 200
    assert "is_correct" not in view.text and "answer_key" not in view.text
    state = view.json()
    seen = []
    while state["status"] == "in_progress":
        q = state["question"]
        seen.append(q["question_id"])
        state = (await bank.client.post(
            f"/v1/student/sessions/{state['session_id']}/answer", headers=student_headers(bank),
            json={"seq": q["seq"], "question_id": q["question_id"], "answer": await correct_answer(bank, q["question_id"])},
        )).json()

    assert len(set(seen)) == len(seen) == 5  # no repeats; stopped at the maximum
    assert (state["finish_reason"], state["answered"], state["result_available"]) == ("max_questions", 5, True)

    result = (await bank.client.get(f"/v1/student/tests/{test['id']}/result", headers=student_headers(bank))).json()
    assert result["scaled_score"] > 500 and result["provisional"] is True

    rows = (await bank.client.get(f"/v1/assessments/{test['id']}/results", headers=bank.headers())).json()["rows"]
    aisha = next(r for r in rows if r["student_id"] == STUDENT)
    assert (aisha["status"], aisha["answered"], aisha["correct"], aisha["scaled_score"]) == ("completed", 5, 5, result["scaled_score"])
    assert next(r for r in rows if r["student_id"] == OTHER_STUDENT)["status"] == "not_started"

    # Answered questions now carry responses, so editing one makes a new version.
    edited = await bank.client.put(f"/v1/questions/{seen[0]}", headers=bank.headers(), json=(await bank.client.get(
        f"/v1/questions/{seen[0]}", headers=bank.headers())).json() | {"options": [
            {"text": "One", "is_correct": True}, {"text": "Two", "is_correct": False}]})
    assert edited.json()["new_version_created"] is True


async def test_retries_are_idempotent_and_stale_answers_refused(bank):
    test = await published_test(bank)
    state = (await bank.client.post(f"/v1/student/tests/{test['id']}/start", headers=student_headers(bank))).json()
    q = state["question"]
    body = {"seq": q["seq"], "question_id": q["question_id"], "answer": {"positions": [1]}}
    url = f"/v1/student/sessions/{state['session_id']}/answer"

    first = await bank.client.post(url, headers=student_headers(bank), json=body)
    retry = await bank.client.post(url, headers=student_headers(bank), json=body)
    assert first.json()["answered"] == retry.json()["answered"] == 1

    stale = await bank.client.post(url, headers=student_headers(bank), json={**body, "seq": 5})
    assert stale.status_code == 409

    resumed = (await bank.client.post(f"/v1/student/tests/{test['id']}/start", headers=student_headers(bank))).json()
    assert resumed["session_id"] == state["session_id"] and resumed["answered"] == 1


async def test_invalid_answers_are_rejected_without_recording(bank):
    test = await published_test(bank)
    state = (await bank.client.post(f"/v1/student/tests/{test['id']}/start", headers=student_headers(bank))).json()
    q = state["question"]
    bad = await bank.client.post(
        f"/v1/student/sessions/{state['session_id']}/answer", headers=student_headers(bank),
        json={"seq": 1, "question_id": q["question_id"], "answer": {"positions": [1, 2]}},
    )
    assert bad.status_code == 422
    again = (await bank.client.get(f"/v1/student/sessions/{state['session_id']}", headers=student_headers(bank))).json()
    assert again["answered"] == 0 and again["question"]["question_id"] == q["question_id"]


async def test_only_rostered_students_in_the_window(bank):
    test = await published_test(bank)
    outsider = await bank.client.post(f"/v1/student/tests/{test['id']}/start", headers=student_headers(bank, 4242))
    assert outsider.status_code == 404
    teacher = await bank.client.post(f"/v1/student/tests/{test['id']}/start", headers=student_headers(bank, role="teacher"))
    assert teacher.status_code == 403
    other_session = (await bank.client.post(f"/v1/student/tests/{test['id']}/start", headers=student_headers(bank, OTHER_STUDENT))).json()
    peek = await bank.client.get(f"/v1/student/sessions/{other_session['session_id']}", headers=student_headers(bank))
    assert peek.status_code == 404

    await bank.sql("UPDATE assessments SET starts_at = now() + interval '1 day', ends_at = now() + interval '2 days'")
    early = await bank.client.post(f"/v1/student/tests/{test['id']}/start", headers=student_headers(bank))
    assert early.status_code == 409


async def test_time_up_and_closing_finish_attempts(bank):
    test = await published_test(bank)
    aisha = (await bank.client.post(f"/v1/student/tests/{test['id']}/start", headers=student_headers(bank))).json()
    omar = (await bank.client.post(f"/v1/student/tests/{test['id']}/start", headers=student_headers(bank, OTHER_STUDENT))).json()

    await bank.sql("UPDATE sessions SET deadline_at = now() - interval '5 minutes' WHERE id = :id", id=aisha["session_id"])
    q = aisha["question"]
    late = (await bank.client.post(
        f"/v1/student/sessions/{aisha['session_id']}/answer", headers=student_headers(bank),
        json={"seq": 1, "question_id": q["question_id"], "answer": {"positions": [1]}},
    )).json()
    assert (late["status"], late["finish_reason"], late["answered"]) == ("completed", "time_up", 0)

    closed = await bank.client.post(f"/v1/assessments/{test['id']}/close", headers=bank.headers())
    assert closed.json()["status"] == "closed"
    omar_now = (await bank.client.get(f"/v1/student/sessions/{omar['session_id']}", headers=student_headers(bank, OTHER_STUDENT))).json()
    assert (omar_now["status"], omar_now["finish_reason"]) == ("completed", "closed")


async def test_results_wait_for_close_when_the_teacher_says_so(bank):
    test = await published_test(bank, result_policy="after_close", max_questions=3)
    state = (await bank.client.post(f"/v1/student/tests/{test['id']}/start", headers=student_headers(bank))).json()
    finished = (await bank.client.post(f"/v1/student/sessions/{state['session_id']}/finish", headers=student_headers(bank))).json()
    assert (finished["finish_reason"], finished["result_available"]) == ("student", False)

    hidden = await bank.client.get(f"/v1/student/tests/{test['id']}/result", headers=student_headers(bank))
    assert hidden.status_code == 403
    await bank.client.post(f"/v1/assessments/{test['id']}/close", headers=bank.headers())
    shown = await bank.client.get(f"/v1/student/tests/{test['id']}/result", headers=student_headers(bank))
    assert shown.status_code == 200


async def test_long_answers_are_never_delivered(bank):
    chapters = await build_bank(bank, question_type="long_answer")
    draft = (await bank.client.post("/v1/assessments", headers=bank.headers(), json=draft_payload(chapters))).json()
    assert draft["pool"]["total"] == 0
    refused = await bank.client.post(f"/v1/assessments/{draft['id']}/publish", headers=bank.headers(), json=ROSTER)
    assert refused.status_code == 409
