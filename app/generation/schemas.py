from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints, field_validator, model_validator

from app.bank.enums import BloomLevel, Difficulty, QuestionType
from app.textbook.schemas import MAX_PASSAGES, SourcePassage

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
OptionalName = Annotated[str | None, StringConstraints(strip_whitespace=True, max_length=200)]

MAX_QUESTIONS = 10
MAX_AVOID = 100
# Learning objectives / outcomes of the lesson, sent by OnlineCampus when the school wrote some.
MAX_GOALS = 12


class DifficultyCounts(BaseModel):
    """How many questions of each difficulty the batch should have."""

    easy: int = Field(0, ge=0, le=MAX_QUESTIONS)
    medium: int = Field(0, ge=0, le=MAX_QUESTIONS)
    hard: int = Field(0, ge=0, le=MAX_QUESTIONS)

    def of(self, level: Difficulty) -> int:
        return getattr(self, level.value)


class GenerateIn(BaseModel):
    """What to write. Names, not ids: Theta keeps no copy of the bank's topics."""

    class_name: Name
    subject_name: Name
    chapter: Name
    # Omitted when the questions may cover the whole chapter.
    unit: OptionalName = None
    # A sub-unit of that unit, when the questions are for it alone.
    subunit: OptionalName = None
    count: int = Field(5, ge=1, le=MAX_QUESTIONS)
    types: list[QuestionType] = Field(min_length=1, max_length=len(QuestionType))
    # The teacher's mix, as counts that add up to `count` (OnlineCampus turns the
    # percentages into counts).
    difficulty_counts: DifficultyCounts
    bloom_levels: list[BloomLevel] = Field(default_factory=list, max_length=len(BloomLevel))
    language: Literal["en", "ar"] = "en"
    # The model to write with: the institution's setting, sent by OnlineCampus. Omitted =
    # this server's default. Checked against the allow-list before any provider is called.
    model: Annotated[str | None, StringConstraints(strip_whitespace=True, max_length=100)] = None
    instructions: Annotated[str | None, StringConstraints(strip_whitespace=True, max_length=1000)] = None
    # Stems of questions already in the bank for this topic, so none is repeated.
    avoid: list[Annotated[str, StringConstraints(max_length=10000)]] = Field(default_factory=list, max_length=MAX_AVOID)
    # What the lesson is meant to teach (the school's syllabus outcomes and the teachers'
    # lesson outcomes, "Objective: ..." / "Outcome: ..."). Empty when none are written down.
    lesson_goals: list[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=400)]] = Field(
        default_factory=list, max_length=MAX_GOALS)

    # Pages of the school's own textbook for this topic (OnlineCampus picks the closest
    # passages). When given, every question is written from them and names its page.
    source_passages: list[SourcePassage] = Field(default_factory=list, max_length=MAX_PASSAGES)

    @field_validator("types", "bloom_levels")
    @classmethod
    def _distinct(cls, values: list) -> list:
        return list(dict.fromkeys(values))

    @field_validator("types")
    @classmethod
    def _no_numeric(cls, values: list) -> list:
        # Numbers are asked as short answers now: OnlineCampus compares a number
        # typed by the student with the accepted numbers exactly.
        if QuestionType.NUMERIC in values:
            raise ValueError("Numeric questions are no longer written; ask for short answers.")
        if QuestionType.LONG_ANSWER in values:
            raise ValueError("Long-answer questions are no longer written.")
        return values

    @model_validator(mode="after")
    def _mix_matches_count(self) -> "GenerateIn":
        c = self.difficulty_counts
        if c.easy + c.medium + c.hard != self.count:
            raise ValueError("The difficulty counts must add up to the number of questions.")
        return self


class GeneratedOption(BaseModel):
    text: str
    is_correct: bool


class GeneratedKey(BaseModel):
    """The answer key in OnlineCampus's shape; only the fields of the question's type are set."""

    value: float | None = None
    tolerance: float | None = None
    unit: str | None = None
    accepted: list[str] | None = None
    case_sensitive: bool | None = None
    model_answer: str | None = None


class GeneratedQuestion(BaseModel):
    type: QuestionType
    stem: str
    explanation: str | None
    difficulty: Difficulty
    bloom_level: BloomLevel
    options: list[GeneratedOption]
    answer_key: GeneratedKey | None
    # The textbook page the question was written from (None when no pages were given).
    source_page: int | None = None


class RejectedQuestion(BaseModel):
    """A question Claude wrote that failed the bank's rules, with the reason."""

    index: int
    stem: str
    reason: str


class GenerateOut(BaseModel):
    model: str
    questions: list[GeneratedQuestion]
    rejected: list[RejectedQuestion]
    input_tokens: int
    output_tokens: int
