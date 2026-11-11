"""Chapter → unit topics and the syllabus import, against a real PostgreSQL."""

from tests.test_bank_api import CLASS, SUBJECT, mcq, new_question, new_topic

# Shaped like LMSSyllabusInClassDetail rows for Class 21 Biology in TestAzhar.
SYLLABUS = [
    {"source_id": 3516, "parent_source_id": 0, "level": 1, "name": "1.Protectors of biosphere", "order": 1},
    {"source_id": 3517, "parent_source_id": 0, "level": 1, "name": "2.Food through digestive tract", "order": 2},
    {"source_id": 3523, "parent_source_id": 3516, "level": 2, "name": "Pigments in Leaves", "order": 1},
    {"source_id": 3524, "parent_source_id": 3516, "level": 2, "name": "The Chemistry of Photosynthesis", "order": 2},
    {"source_id": 3600, "parent_source_id": 3517, "level": 2, "name": "Exercises", "order": 9},
    {"source_id": 3601, "parent_source_id": 3516, "level": 2, "name": "Exercises", "order": 9},
    {"source_id": 3700, "parent_source_id": 3523, "level": 3, "name": "Chlorophyll a and b", "order": 1},
    {"source_id": 3800, "parent_source_id": 9999, "level": 2, "name": "Unit with a missing chapter", "order": 1},
]


async def import_syllabus(bank, nodes=SYLLABUS):
    response = await bank.client.post(
        "/v1/topics/import", headers=bank.headers(), json={"class_id": CLASS, "subject_id": SUBJECT, "nodes": nodes}
    )
    assert response.status_code == 200, response.text
    return response.json()


async def test_import_builds_the_tree_in_syllabus_order(bank):
    result = await import_syllabus(bank)
    assert (result["created_chapters"], result["created_units"], result["matched"]) == (2, 4, 0)
    assert (result["skipped_subunits"], result["skipped_orphans"]) == (1, 1)

    layout = [(t["level"], t["name"]) for t in result["topics"]]
    assert layout == [
        ("chapter", "1.Protectors of biosphere"),
        ("unit", "Pigments in Leaves"),
        ("unit", "The Chemistry of Photosynthesis"),
        ("unit", "Exercises"),
        ("chapter", "2.Food through digestive tract"),
        ("unit", "Exercises"),
    ]
    chapter = result["topics"][0]
    assert chapter["parent_id"] is None and chapter["source_syllabus_detail_id"] == 3516
    assert all(t["parent_id"] == chapter["id"] for t in result["topics"][1:4])


async def test_reimport_with_new_year_ids_matches_by_name(bank):
    first = await import_syllabus(bank)
    unit_id = next(t["id"] for t in first["topics"] if t["name"] == "Pigments in Leaves")
    await new_question(bank, unit_id)

    next_year = [
        {**n, "source_id": n["source_id"] + 10000, "parent_source_id": n["parent_source_id"] and n["parent_source_id"] + 10000}
        for n in SYLLABUS
    ]
    next_year[0]["name"] = "1.PROTECTORS OF BIOSPHERE"  # casing drift is still the same chapter
    second = await import_syllabus(bank, next_year)

    assert (second["created_chapters"], second["created_units"], second["matched"]) == (0, 0, 6)
    same_unit = next(t for t in second["topics"] if t["name"] == "Pigments in Leaves")
    assert same_unit["id"] == unit_id
    assert same_unit["source_syllabus_detail_id"] == 13523
    assert same_unit["question_count"] == 1


async def test_units_are_added_under_chapters_only(bank):
    chapter = await new_topic(bank, "Algebra")
    unit = await bank.client.post(
        "/v1/topics", headers=bank.headers(),
        json={"class_id": CLASS, "subject_id": SUBJECT, "parent_id": chapter, "name": "Linear equations"},
    )
    assert unit.status_code == 201 and unit.json()["level"] == "unit"

    nested = await bank.client.post(
        "/v1/topics", headers=bank.headers(),
        json={"class_id": CLASS, "subject_id": SUBJECT, "parent_id": unit.json()["id"], "name": "Too deep"},
    )
    assert nested.status_code == 409

    blocked = await bank.client.delete(f"/v1/topics/{chapter}", headers=bank.headers())
    assert blocked.status_code == 409


async def test_chapter_filter_includes_its_units(bank):
    result = await import_syllabus(bank)
    chapter = result["topics"][0]
    unit = result["topics"][1]
    other_chapter = result["topics"][4]
    await new_question(bank, chapter["id"], stem="Directly in the chapter")
    await new_question(bank, unit["id"], stem="In a unit of the chapter")
    await new_question(bank, other_chapter["id"], stem="In another chapter")

    params = {"class_id": CLASS, "subject_id": SUBJECT}
    by_chapter = (await bank.client.get("/v1/questions", headers=bank.headers(), params={**params, "topic_id": chapter["id"]})).json()
    assert sorted(q["stem_preview"] for q in by_chapter["items"]) == ["Directly in the chapter", "In a unit of the chapter"]

    by_unit = (await bank.client.get("/v1/questions", headers=bank.headers(), params={**params, "topic_id": unit["id"]})).json()
    assert [q["topic_path"] for q in by_unit["items"]] == ["1.Protectors of biosphere › Pigments in Leaves"]


async def test_question_detail_carries_the_topic_path(bank):
    result = await import_syllabus(bank)
    unit = result["topics"][2]
    created = await bank.client.post("/v1/questions", headers=bank.headers(), json=mcq(unit["id"]))
    assert created.json()["question"]["topic_path"] == "1.Protectors of biosphere › The Chemistry of Photosynthesis"


async def test_import_respects_teaching_scope(bank):
    response = await bank.client.post(
        "/v1/topics/import", headers=bank.headers(scope="12:6"),
        json={"class_id": CLASS, "subject_id": SUBJECT, "nodes": SYLLABUS},
    )
    assert response.status_code == 403
