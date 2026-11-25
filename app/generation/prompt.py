"""What Claude is asked, and the JSON shape its answer must take.

The system prompt and the schema never change between requests, so the schema is
compiled once and the prompt prefix is identical for every school.
"""

from app.bank.enums import BloomLevel, Difficulty, QuestionType
from app.generation.schemas import GenerateIn

# Longest existing stem quoted back to Claude; enough to recognise a question.
AVOID_STEM_CHARS = 300

TYPE_NAMES: dict[QuestionType, str] = {
    QuestionType.MCQ_SINGLE: "multiple choice with one correct answer",
    QuestionType.MCQ_MULTI: "multiple choice with more than one correct answer",
    QuestionType.TRUE_FALSE: "true/false",
    QuestionType.SHORT_ANSWER: "short answer, a word, phrase or number",
}

# The types Claude may write. Numeric questions are no longer used: a number is a
# short answer whose accepted answers are digits.
WRITABLE_TYPES = [t for t in QuestionType if t not in (QuestionType.NUMERIC, QuestionType.LONG_ANSWER)]

SYSTEM = """You write questions for a school question bank. A teacher reviews every question before students see it, and the bank later measures each question's real difficulty from students' answers. So each question should test one clear idea and have an answer key that is unambiguously right.

How to write them:
- Match the class level and the chapter, unit or sub-unit named in the request. Use only what a student at that level is expected to know from that part of the syllabus.
- Each question stands alone: no references to a passage, figure, diagram or another question.
- mcq_single: 3 to 5 options (usually 4), exactly one correct. Wrong options are plausible mistakes a student could make, similar in length and style to the right one. Avoid "all of the above", "none of the above" and wording that gives the answer away.
- mcq_multi: 4 to 6 options, at least one correct and at least one wrong. Say in the question that more than one answer may be correct.
- true_false: exactly two options, "True" and "False" (in Arabic "صح" and "خطأ"), with the right one marked correct. The statement must be clearly true or clearly false.
- short_answer: the answer is a word, a short phrase or one number. List every acceptable form and spelling in accepted_answers (at most 10). For a number, write it in digits (7, 2.5), add the form with its unit when there is one ("7 cm"), and when the answer needs rounding tell the student how to round.
- Fields that do not apply to a question's type take an empty value: options [], accepted_answers [].
- Write math in LaTeX between $...$ inside a sentence, or $$...$$ on its own line. Close every formula you open, and write \\$ for a money sign.
- difficulty: easy means most of the class answers correctly, medium about half, hard only the strongest students.
- bloom_level: the kind of thinking the question asks for (remember, understand, apply, analyze, evaluate, create).
- When lesson goals are given, write questions that check those goals, spread across them; stay inside the named chapter, unit or sub-unit.
- When textbook passages are given, write every question from them: the facts, words and examples the class's own book uses, and an answer the passages make plainly right. The question still stands alone (do not write "according to the passage"). Set source_page to the page the answer is on; with no passages, set it to 0.
- Do not repeat or closely paraphrase a question listed as already in the bank, and make the questions in one batch different from each other."""

_OPTION = {
    "type": "object",
    "properties": {"text": {"type": "string"}, "is_correct": {"type": "boolean"}},
    "required": ["text", "is_correct"],
    "additionalProperties": False,
}

_QUESTION = {
    "type": "object",
    "properties": {
        "type": {"type": "string", "enum": [t.value for t in WRITABLE_TYPES]},
        "stem": {"type": "string"},
        "options": {"type": "array", "items": _OPTION},
        "accepted_answers": {"type": "array", "items": {"type": "string"}},
        "difficulty": {"type": "string", "enum": [d.value for d in Difficulty]},
        "bloom_level": {"type": "string", "enum": [b.value for b in BloomLevel]},
        "source_page": {"type": "integer"},
    },
    "required": [
        "type", "stem", "options", "accepted_answers", "difficulty", "bloom_level", "source_page",
    ],
    "additionalProperties": False,
}

# Every type-specific field is always present (empty when it does not apply), so the
# schema needs no nullable fields and one fixed shape serves every request.
SCHEMA = {
    "type": "object",
    "properties": {"questions": {"type": "array", "items": _QUESTION}},
    "required": ["questions"],
    "additionalProperties": False,
}


def build(request: GenerateIn) -> str:
    """The user message: the request's facts, then the teacher's notes and the bank's
    existing questions in tags of their own."""
    lines = [
        f"Class: {request.class_name}",
        f"Subject: {request.subject_name}",
        f"Chapter: {request.chapter}",
        f"Unit: {request.unit}" if request.unit else "Unit: any part of the chapter",
    ]
    if request.unit and request.subunit:
        lines.append(f"Sub-unit: {request.subunit}. Every question is about this sub-unit.")
    if request.language == "ar":
        lines.append("Language: Arabic. Write every question, option and answer in Arabic; keep math in LaTeX.")
    else:
        lines.append("Language: English")
    lines.append(f"Number of questions: {request.count}")

    types = ", ".join(f"{t.value} ({TYPE_NAMES[t]})" for t in request.types)
    if len(request.types) == 1:
        lines.append(f"Question type: {types}. Every question uses this type.")
    else:
        lines.append(f"Question types: {types}. Use only these types, spread across the batch.")

    lines.append(f"Difficulty: {_mix(request)}. Label each question with its difficulty.")
    if request.bloom_levels:
        lines.append("Bloom levels: " + ", ".join(b.value for b in request.bloom_levels) + ".")

    parts = ["\n".join(lines)]
    if request.lesson_goals:
        goals = "\n".join(f"- {_one_line(goal)}" for goal in request.lesson_goals)
        parts.append(f"<lesson_goals>\n{goals}\n</lesson_goals>")
    if request.source_passages:
        passages = "\n".join(f'<passage page="{p.page}">\n{p.text}\n</passage>' for p in request.source_passages)
        parts.append(f"<textbook_passages>\n{passages}\n</textbook_passages>")
    if request.instructions:
        parts.append(f"<teacher_notes>\n{request.instructions}\n</teacher_notes>")
    if request.avoid:
        quoted = "\n".join(f"- {_one_line(stem)}" for stem in request.avoid)
        parts.append(f"<already_in_bank>\n{quoted}\n</already_in_bank>")
    return "\n\n".join(parts)


def _mix(request: GenerateIn) -> str:
    """'2 easy, 2 medium and 1 hard', leaving out the levels not asked for."""
    parts = [f"{request.difficulty_counts.of(d)} {d.value}" for d in Difficulty if request.difficulty_counts.of(d) > 0]
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def _one_line(stem: str) -> str:
    text = " ".join(stem.split())
    return text if len(text) <= AVOID_STEM_CHARS else text[: AVOID_STEM_CHARS - 1] + "…"
