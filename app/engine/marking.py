"""Deciding whether a submitted answer is right. Every type here is marked automatically;
long answers need a teacher and are not delivered in adaptive tests."""

import re
from dataclasses import dataclass

AUTO_MARKED_TYPES = frozenset({"mcq_single", "mcq_multi", "true_false", "numeric", "short_answer"})


@dataclass(frozen=True)
class OptionKey:
    position: int
    is_correct: bool


class InvalidAnswer(ValueError):
    """The answer does not fit the question (for example, a text answer to an MCQ)."""


def is_correct(question_type: str, options: list[OptionKey], answer_key: dict | None, answer: dict) -> bool:
    if question_type in ("mcq_single", "true_false"):
        positions = _positions(answer, options)
        if len(positions) != 1:
            raise InvalidAnswer("Choose exactly one option.")
        return positions == {o.position for o in options if o.is_correct}

    if question_type == "mcq_multi":
        positions = _positions(answer, options)
        if not positions:
            raise InvalidAnswer("Choose at least one option.")
        return positions == {o.position for o in options if o.is_correct}

    if question_type == "numeric":
        key = answer_key or {}
        try:
            value = float(answer.get("value"))
        except (TypeError, ValueError):
            raise InvalidAnswer("Enter a number.") from None
        return abs(value - float(key["value"])) <= float(key.get("tolerance") or 0) + 1e-9

    if question_type == "short_answer":
        key = answer_key or {}
        text = answer.get("text")
        if not isinstance(text, str) or not text.strip():
            raise InvalidAnswer("Enter an answer.")
        case_sensitive = bool(key.get("case_sensitive"))
        given = _normalise(text, case_sensitive)
        return any(given == _normalise(accepted, case_sensitive) for accepted in key.get("accepted", []))

    raise InvalidAnswer(f"A {question_type} question cannot be marked automatically.")


def _positions(answer: dict, options: list[OptionKey]) -> set[int]:
    raw = answer.get("positions")
    if not isinstance(raw, list) or not all(isinstance(p, int) for p in raw):
        raise InvalidAnswer("Choose from the options.")
    valid = {o.position for o in options}
    chosen = set(raw)
    if not chosen <= valid:
        raise InvalidAnswer("Choose from the options.")
    return chosen


def _normalise(text: str, case_sensitive: bool) -> str:
    collapsed = re.sub(r"\s+", " ", text.strip())
    return collapsed if case_sensitive else collapsed.casefold()
