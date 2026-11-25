"""Asks Claude for questions and keeps only those that pass the bank's rules.

Every rule here matches what OnlineCampus checks again when a teacher saves the
question, so a draft that comes back from Theta is one the bank will accept.
"""

import logging

from app.bank import rules
from app.bank.enums import OPTION_TYPES, BloomLevel, Difficulty, QuestionType
from app.core.security import Caller
from app.generation import prompt
from app.generation.schemas import (
    GenerateIn,
    GenerateOut,
    GeneratedKey,
    GeneratedOption,
    GeneratedQuestion,
    RejectedQuestion,
)
from app.generation.writer import QuestionWriter

log = logging.getLogger(__name__)

STEM_MAX = 10000
OPTION_MAX = 2000
ACCEPTED_MAX = 10
ACCEPTED_ITEM_MAX = 500
REJECTED_STEM_CHARS = 200


async def generate(request: GenerateIn, writer: QuestionWriter, caller: Caller) -> GenerateOut:
    reply = await writer.write(prompt.SYSTEM, prompt.build(request), prompt.SCHEMA)

    accepted: list[GeneratedQuestion] = []
    rejected: list[RejectedQuestion] = []
    seen = {_fingerprint(stem) for stem in request.avoid}
    # Claude occasionally writes one more than asked; the extra is dropped, not rejected.
    for index, raw in enumerate(reply.questions[: request.count]):
        try:
            question = to_question(raw, request)
        except ValueError as exc:
            rejected.append(RejectedQuestion(index=index, stem=_short(raw.get("stem")), reason=str(exc)))
            continue
        fingerprint = _fingerprint(question.stem)
        if fingerprint in seen:
            rejected.append(RejectedQuestion(
                index=index, stem=_short(question.stem),
                reason="It repeats a question already in the bank or in this batch."))
            continue
        seen.add(fingerprint)
        accepted.append(question)

    log.info(
        "generated for institution %s user %s: asked %s, kept %s, rejected %s, tokens %s in / %s out, model %s",
        caller.institution_id, caller.user_id, request.count, len(accepted), len(rejected),
        reply.input_tokens, reply.output_tokens, reply.model,
    )
    return GenerateOut(
        model=reply.model,
        questions=accepted,
        rejected=rejected,
        input_tokens=reply.input_tokens,
        output_tokens=reply.output_tokens,
    )


def to_question(raw: dict, request: GenerateIn) -> GeneratedQuestion:
    """One question from Claude's JSON, checked; raises ValueError with the reason."""
    try:
        qtype = QuestionType(raw.get("type"))
    except ValueError:
        raise ValueError("It has no valid question type.") from None
    if qtype not in request.types:
        raise ValueError(f"It is a {qtype.value} question, which was not asked for.")

    stem = _text(raw.get("stem"))
    if not stem:
        raise ValueError("The question text is empty.")
    if len(stem) > STEM_MAX:
        raise ValueError(f"The question text is longer than {STEM_MAX} characters.")
    _closed(stem, "the question text")

    # No explanation is written: teachers add one themselves if they want it.
    explanation = None

    try:
        difficulty = Difficulty(raw.get("difficulty"))
    except ValueError:
        raise ValueError("It has no valid difficulty.") from None
    if request.difficulty_counts.of(difficulty) == 0:
        raise ValueError(f"It is {difficulty.value}, which was not asked for.")
    try:
        bloom = BloomLevel(raw.get("bloom_level"))
    except ValueError:
        raise ValueError("It has no valid Bloom level.") from None

    options: list[GeneratedOption] = []
    if qtype in OPTION_TYPES:
        raw_options = raw.get("options") if isinstance(raw.get("options"), list) else []
        pairs = [(_text(o.get("text")), o.get("is_correct") is True) for o in raw_options if isinstance(o, dict)]
        if len(pairs) > rules.MAX_OPTIONS:
            raise ValueError(f"It has more than {rules.MAX_OPTIONS} options.")
        if any(len(text) > OPTION_MAX for text, _ in pairs):
            raise ValueError(f"An option is longer than {OPTION_MAX} characters.")
        for text, _ in pairs:
            _closed(text, "an option")
        rules.check_options(qtype, pairs)
        options = [GeneratedOption(text=text, is_correct=correct) for text, correct in pairs]

    pages = {p.page for p in request.source_passages}
    page = raw.get("source_page")
    return GeneratedQuestion(
        source_page=page if isinstance(page, int) and page in pages else None,
        type=qtype,
        stem=stem,
        explanation=explanation,
        difficulty=difficulty,
        bloom_level=bloom,
        options=options,
        answer_key=_answer_key(qtype, raw),
    )


def _answer_key(qtype: QuestionType, raw: dict) -> GeneratedKey | None:
    if qtype == QuestionType.SHORT_ANSWER:
        raw_accepted = raw.get("accepted_answers") if isinstance(raw.get("accepted_answers"), list) else []
        accepted: list[str] = []
        for answer in (_text(a) for a in raw_accepted):
            if answer and answer.casefold() not in {a.casefold() for a in accepted}:
                accepted.append(answer)
        if not accepted:
            raise ValueError("A short-answer question needs at least one accepted answer.")
        if len(accepted) > ACCEPTED_MAX:
            raise ValueError(f"A short-answer question can have at most {ACCEPTED_MAX} accepted answers.")
        if any(len(a) > ACCEPTED_ITEM_MAX for a in accepted):
            raise ValueError(f"An accepted answer is longer than {ACCEPTED_ITEM_MAX} characters.")
        return GeneratedKey(accepted=accepted, case_sensitive=False)

    return None  # option types are keyed by their options


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _closed(text: str | None, where: str) -> None:
    if not rules.math_is_closed(text):
        raise ValueError(f"A formula in {where} is not closed.")


def _fingerprint(stem: str) -> str:
    """Two stems that differ only in case, spacing or end punctuation count as the same."""
    return " ".join(stem.casefold().split()).rstrip(" .?!؟")


def _short(stem: object) -> str:
    text = " ".join(stem.split()) if isinstance(stem, str) else ""
    return text if len(text) <= REJECTED_STEM_CHARS else text[: REJECTED_STEM_CHARS - 1] + "…"
