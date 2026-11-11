"""Question bank rules with no database in sight, so each one is unit-testable on its own."""

from typing import Literal

from app.bank.enums import OPTION_TYPES, Difficulty, QuestionStatus, QuestionType
from app.core.errors import Conflict

MIN_OPTIONS = 2
MAX_OPTIONS = 6

# Starting difficulty on the θ scale until real answers calibrate the question.
PRIOR_DIFFICULTY: dict[Difficulty, float] = {
    Difficulty.EASY: -1.0,
    Difficulty.MEDIUM: 0.0,
    Difficulty.HARD: 1.0,
}

# Where each status may go by hand. ACTIVE is reached only through calibration.
ALLOWED_TRANSITIONS: dict[QuestionStatus, frozenset[QuestionStatus]] = {
    QuestionStatus.DRAFT: frozenset({QuestionStatus.PILOT}),
    QuestionStatus.PILOT: frozenset({QuestionStatus.DRAFT, QuestionStatus.RETIRED}),
    QuestionStatus.ACTIVE: frozenset({QuestionStatus.RETIRED}),
    QuestionStatus.RETIRED: frozenset(),
}


def guessing_floor(question_type: QuestionType, option_count: int) -> float:
    """Chance of a right answer by pure guessing: the c parameter's starting value."""
    if question_type == QuestionType.MCQ_SINGLE and option_count >= MIN_OPTIONS:
        return round(1 / option_count, 3)
    if question_type == QuestionType.TRUE_FALSE:
        return 0.5
    return 0.0


def check_options(question_type: QuestionType, options: list[tuple[str, bool]]) -> None:
    """Raises ValueError describing the first problem with a question's options."""
    if question_type not in OPTION_TYPES:
        if options:
            raise ValueError(f"A {question_type} question has no options.")
        return

    if question_type == QuestionType.TRUE_FALSE and len(options) != 2:
        raise ValueError("A true/false question has exactly 2 options.")
    if not MIN_OPTIONS <= len(options) <= MAX_OPTIONS:
        raise ValueError(f"A multiple-choice question needs {MIN_OPTIONS} to {MAX_OPTIONS} options.")

    texts = [text.strip() for text, _ in options]
    if any(not t for t in texts):
        raise ValueError("Every option needs text.")
    if len({t.casefold() for t in texts}) != len(texts):
        raise ValueError("Two options have the same text.")

    correct = sum(1 for _, is_correct in options if is_correct)
    if question_type in (QuestionType.MCQ_SINGLE, QuestionType.TRUE_FALSE) and correct != 1:
        raise ValueError("Mark exactly one option as correct.")
    if question_type == QuestionType.MCQ_MULTI and not 1 <= correct < len(options):
        raise ValueError("Mark at least one correct option, and leave at least one incorrect.")


def check_answer_key(question_type: QuestionType, answer_key: dict | None) -> None:
    """Numeric and short-answer questions need a key; option types must not carry one."""
    if question_type in OPTION_TYPES:
        if answer_key:
            raise ValueError(f"A {question_type} question is keyed by its options, not an answer key.")
        return
    if question_type == QuestionType.NUMERIC and (not answer_key or "value" not in answer_key):
        raise ValueError("A numeric question needs the correct value.")
    if question_type == QuestionType.SHORT_ANSWER and not (answer_key or {}).get("accepted"):
        raise ValueError("A short-answer question needs at least one accepted answer.")


def math_is_closed(text: str | None) -> bool:
    """Math is written between $…$ (in the line) or $$…$$ (on its own line), and a
    backslash before $ makes it a plain dollar sign: the same reading as OnlineCampus's
    math renderer. True when every formula that opens also closes, so a half-written
    one never reaches a student."""
    if not text:
        return True
    mode = 0  # 0 text · 1 inline · 2 display
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text) and text[i + 1] == "$":
            i += 2
            continue
        if ch == "$":
            double = i + 1 < len(text) and text[i + 1] == "$"
            if mode == 0:
                mode = 2 if double else 1
                if double:
                    i += 1
            elif mode == 1:
                mode = 0
            elif double:
                mode = 0
                i += 1
        i += 1
    return mode == 0


def check_transition(current: QuestionStatus, target: QuestionStatus, n_responses: int) -> None:
    if target == current:
        raise Conflict(f"The question is already {current}.")
    if target not in ALLOWED_TRANSITIONS[current]:
        raise Conflict(f"A {current} question cannot become {target}.")
    if current == QuestionStatus.PILOT and target == QuestionStatus.DRAFT and n_responses > 0:
        raise Conflict("Students have already answered this pilot question; retire it instead.")


def edit_mode(status: QuestionStatus, n_responses: int) -> Literal["in_place", "new_version"]:
    """How an edit lands. A question students have answered keeps its wording, because
    its answers and parameters belong to that exact wording: the edit becomes a new draft."""
    if status == QuestionStatus.RETIRED:
        raise Conflict("A retired question cannot be edited.")
    if status == QuestionStatus.DRAFT:
        return "in_place"
    if status == QuestionStatus.PILOT and n_responses == 0:
        return "in_place"
    return "new_version"


def can_delete(status: QuestionStatus) -> None:
    if status != QuestionStatus.DRAFT:
        raise Conflict("Only a draft can be deleted; retire this question instead.")
