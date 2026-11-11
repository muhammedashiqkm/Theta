"""Question bank API against a real PostgreSQL (see the `bank` fixture)."""

CLASS, SUBJECT = 12, 5


def mcq(topic_id: int, *, correct: int = 1, difficulty: str = "medium", options: int = 4, stem: str = "What is 7 × 8?") -> dict:
    return {
        "class_id": CLASS,
        "subject_id": SUBJECT,
        "topic_id": topic_id,
        "type": "mcq_single",
        "stem": stem,
        "difficulty": difficulty,
        "bloom_level": "apply",
        "marks": 1,
        "options": [{"text": str(54 + i), "is_correct": i == correct} for i in range(options)],
    }


async def new_topic(bank, name: str = "Multiplication", class_id: int = CLASS, subject_id: int = SUBJECT) -> int:
    response = await bank.client.post(
        "/v1/topics", headers=bank.headers(), json={"class_id": class_id, "subject_id": subject_id, "name": name}
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def new_question(bank, topic_id: int, **overrides) -> dict:
    response = await bank.client.post("/v1/questions", headers=bank.headers(), json=mcq(topic_id, **overrides))
    assert response.status_code == 201, response.text
    return response.json()["question"]


# ----- access -----------------------------------------------------------------

async def test_scope_header_is_required(bank):
    response = await bank.client.get(
        "/v1/topics", headers=bank.headers(scope=None), params={"class_id": CLASS, "subject_id": SUBJECT}
    )
    assert response.status_code == 400


async def test_students_cannot_manage_the_bank(bank):
    response = await bank.client.get(
        "/v1/topics", headers=bank.headers(role="student"), params={"class_id": CLASS, "subject_id": SUBJECT}
    )
    assert response.status_code == 403


async def test_teaching_scope_limits_reads_and_writes(bank):
    topic_id = await new_topic(bank)
    question = await new_question(bank, topic_id)
    other_teacher = bank.headers(scope="12:6")

    listed = await bank.client.get(
        "/v1/questions", headers=other_teacher, params={"class_id": CLASS, "subject_id": SUBJECT}
    )
    assert listed.status_code == 403
    fetched = await bank.client.get(f"/v1/questions/{question['id']}", headers=other_teacher)
    assert fetched.status_code == 404  # out of scope reads as missing
    created = await bank.client.post("/v1/questions", headers=other_teacher, json=mcq(topic_id))
    assert created.status_code == 403

    own_teacher = bank.headers(scope="12:5")
    assert (await bank.client.get(f"/v1/questions/{question['id']}", headers=own_teacher)).status_code == 200


async def test_institutions_are_isolated(bank):
    question = await new_question(bank, await new_topic(bank))
    response = await bank.client.get(f"/v1/questions/{question['id']}", headers=bank.headers(institution=99))
    assert response.status_code == 404


# ----- topics -----------------------------------------------------------------

async def test_topic_lifecycle(bank):
    topic_id = await new_topic(bank, "Fractions")

    duplicate = await bank.client.post(
        "/v1/topics", headers=bank.headers(), json={"class_id": CLASS, "subject_id": SUBJECT, "name": "  fractions "}
    )
    assert duplicate.status_code == 409

    renamed = await bank.client.put(f"/v1/topics/{topic_id}", headers=bank.headers(), json={"name": "Fractions and decimals", "sort_order": 2})
    assert renamed.status_code == 200 and renamed.json()["name"] == "Fractions and decimals"

    await new_question(bank, topic_id)
    blocked = await bank.client.delete(f"/v1/topics/{topic_id}", headers=bank.headers())
    assert blocked.status_code == 409

    listed = await bank.client.get("/v1/topics", headers=bank.headers(), params={"class_id": CLASS, "subject_id": SUBJECT})
    assert [(t["name"], t["question_count"]) for t in listed.json()] == [("Fractions and decimals", 1)]


async def test_deleted_topic_name_can_be_reused(bank):
    topic_id = await new_topic(bank, "Ratios")
    assert (await bank.client.delete(f"/v1/topics/{topic_id}", headers=bank.headers())).status_code == 204
    await new_topic(bank, "Ratios")


# ----- questions --------------------------------------------------------------

async def test_create_validates_structure(bank):
    topic_id = await new_topic(bank)
    two_correct = mcq(topic_id)
    two_correct["options"][0]["is_correct"] = True
    response = await bank.client.post("/v1/questions", headers=bank.headers(), json=two_correct)
    assert response.status_code == 422
    assert "exactly one" in response.text

    numeric_without_value = {**mcq(topic_id), "type": "numeric", "options": [], "answer_key": {"tolerance": 0.5}}
    response = await bank.client.post("/v1/questions", headers=bank.headers(), json=numeric_without_value)
    assert response.status_code == 422

    numeric = {**mcq(topic_id), "type": "numeric", "options": [], "answer_key": {"value": 56, "tolerance": 0, "extra": 1}}
    response = await bank.client.post("/v1/questions", headers=bank.headers(), json=numeric)
    assert response.status_code == 201
    assert response.json()["question"]["answer_key"] == {"value": 56.0, "tolerance": 0.0, "unit": None}


async def test_topic_must_match_class_and_subject(bank):
    foreign_topic = await new_topic(bank, "Grammar", subject_id=9)
    response = await bank.client.post("/v1/questions", headers=bank.headers(), json=mcq(foreign_topic))
    assert response.status_code == 409


async def test_new_question_is_a_draft_without_parameters(bank):
    question = await new_question(bank, await new_topic(bank))
    assert question["status"] == "draft"
    assert question["version"] == 1
    assert question["params"] is None
    assert [o["position"] for o in question["options"]] == [1, 2, 3, 4]


async def test_approving_sets_a_prior_from_the_tag(bank):
    question = await new_question(bank, await new_topic(bank), difficulty="easy")
    response = await bank.client.post(f"/v1/questions/{question['id']}/status", headers=bank.headers(), json={"status": "pilot"})
    assert response.status_code == 200
    params = response.json()["params"]
    assert response.json()["status"] == "pilot"
    assert (params["model"], params["a"], params["b"], params["c"], params["is_prior"]) == ("1PL", 1.0, -1.0, 0.25, True)


async def test_editing_an_unanswered_pilot_updates_it_and_its_prior(bank):
    topic_id = await new_topic(bank)
    question = await new_question(bank, topic_id)
    await bank.client.post(f"/v1/questions/{question['id']}/status", headers=bank.headers(), json={"status": "pilot"})

    edit = mcq(topic_id, difficulty="hard", options=5, correct=3, stem="What is 8 × 9?")
    response = await bank.client.put(f"/v1/questions/{question['id']}", headers=bank.headers(), json=edit)
    body = response.json()
    assert response.status_code == 200
    assert body["new_version_created"] is False
    assert body["question"]["id"] == question["id"]
    assert body["question"]["stem"] == "What is 8 × 9?"
    assert len(body["question"]["options"]) == 5
    assert (body["question"]["params"]["b"], body["question"]["params"]["c"]) == (1.0, 0.2)


async def test_editing_an_answered_question_creates_a_new_version(bank):
    topic_id = await new_topic(bank)
    question = await new_question(bank, topic_id)
    await bank.client.post(f"/v1/questions/{question['id']}/status", headers=bank.headers(), json={"status": "pilot"})
    await bank.sql("UPDATE item_params SET n_responses = 12 WHERE question_id = :id", id=question["id"])

    response = await bank.client.put(
        f"/v1/questions/{question['id']}", headers=bank.headers(), json=mcq(topic_id, stem="What is 7 times 8?")
    )
    body = response.json()
    assert body["new_version_created"] is True
    assert body["question"]["id"] != question["id"]
    assert (body["question"]["status"], body["question"]["version"]) == ("draft", 2)
    assert body["question"]["previous_version_id"] == question["id"]

    original = (await bank.client.get(f"/v1/questions/{question['id']}", headers=bank.headers())).json()
    assert (original["stem"], original["status"]) == ("What is 7 × 8?", "pilot")

    back_to_draft = await bank.client.post(f"/v1/questions/{question['id']}/status", headers=bank.headers(), json={"status": "draft"})
    assert back_to_draft.status_code == 409


async def test_delete_and_retire_rules(bank):
    topic_id = await new_topic(bank)
    draft = await new_question(bank, topic_id)
    assert (await bank.client.delete(f"/v1/questions/{draft['id']}", headers=bank.headers())).status_code == 204

    pilot = await new_question(bank, topic_id)
    await bank.client.post(f"/v1/questions/{pilot['id']}/status", headers=bank.headers(), json={"status": "pilot"})
    assert (await bank.client.delete(f"/v1/questions/{pilot['id']}", headers=bank.headers())).status_code == 409

    retired = await bank.client.post(f"/v1/questions/{pilot['id']}/status", headers=bank.headers(), json={"status": "retired"})
    assert retired.json()["status"] == "retired"
    edit = await bank.client.put(f"/v1/questions/{pilot['id']}", headers=bank.headers(), json=mcq(topic_id))
    assert edit.status_code == 409


async def test_list_filters_search_and_pages(bank):
    topic_id = await new_topic(bank)
    for n in range(1, 8):
        await new_question(bank, topic_id, stem=f"Question {n}: what is {n} × 2?")
    first = await new_question(bank, topic_id, stem="100% of 40 is?")
    await bank.client.post(f"/v1/questions/{first['id']}/status", headers=bank.headers(), json={"status": "pilot"})

    params = {"class_id": CLASS, "subject_id": SUBJECT}
    page = (await bank.client.get("/v1/questions", headers=bank.headers(), params={**params, "page_size": 3, "page": 2})).json()
    assert (page["total"], page["page"], len(page["items"])) == (8, 2, 3)

    pilots = (await bank.client.get("/v1/questions", headers=bank.headers(), params={**params, "status": "pilot"})).json()
    assert [q["id"] for q in pilots["items"]] == [first["id"]]

    # "%" is matched literally, not as a wildcard.
    found = (await bank.client.get("/v1/questions", headers=bank.headers(), params={**params, "search": "100%"})).json()
    assert found["total"] == 1 and found["items"][0]["option_count"] == 4


async def test_changes_are_audited(bank):
    question = await new_question(bank, await new_topic(bank))
    await bank.client.post(f"/v1/questions/{question['id']}/status", headers=bank.headers(user=777), json={"status": "pilot"})
    async with bank.engine.connect() as conn:
        from sqlalchemy import text

        rows = (await conn.execute(text("SELECT action, actor_user_id FROM audit_log ORDER BY id"))).all()
    assert [tuple(r) for r in rows] == [("topic.create", 345), ("question.create", 345), ("question.status", 777)]
