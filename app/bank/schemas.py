from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints, ValidationError, model_validator

from app.bank import rules
from app.bank.enums import BloomLevel, Difficulty, QuestionSource, QuestionStatus, QuestionType, TopicLevel

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Id = Annotated[int, Field(gt=0)]


# ----- topics -----------------------------------------------------------------

OptionalName = Annotated[str | None, StringConstraints(strip_whitespace=True, max_length=200)]
SortOrder = Annotated[int, Field(ge=0, le=32767)]


class TopicCreate(BaseModel):
    class_id: Id
    subject_id: Id
    # Omitted for a chapter; the chapter's id for a unit.
    parent_id: Id | None = None
    name: Name
    name_nat: OptionalName = None
    sort_order: SortOrder = 0


class TopicUpdate(BaseModel):
    name: Name
    name_nat: OptionalName = None
    sort_order: SortOrder = 0


class TopicOut(BaseModel):
    id: int
    class_id: int
    subject_id: int
    parent_id: int | None
    level: TopicLevel
    name: str
    name_nat: str | None
    sort_order: int
    source_syllabus_detail_id: int | None
    # Live (not retired) questions filed directly under this topic.
    question_count: int


class SyllabusNode(BaseModel):
    """One LMSSyllabusInClassDetail row as OnlineCampus reads it."""

    source_id: Id
    parent_source_id: int = Field(0, ge=0)  # 0 for a chapter
    level: int = Field(ge=1, le=3)           # 1 chapter · 2 unit · 3 subunit
    name: Name
    name_nat: OptionalName = None
    order: int = Field(0, ge=0, le=32767)


class TopicImport(BaseModel):
    class_id: Id
    subject_id: Id
    nodes: list[SyllabusNode] = Field(max_length=5000)


class TopicImportOut(BaseModel):
    created_chapters: int
    created_units: int
    matched: int
    # Subunits are not a topic level; units whose chapter was not in the import are orphans.
    skipped_subunits: int
    skipped_orphans: int
    topics: list[TopicOut]


# ----- answer keys ------------------------------------------------------------

class NumericKey(BaseModel):
    value: float
    tolerance: float = Field(0, ge=0)
    unit: str | None = Field(None, max_length=30)


class ShortAnswerKey(BaseModel):
    accepted: list[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]] = Field(
        min_length=1, max_length=10
    )
    case_sensitive: bool = False


class RubricLevel(BaseModel):
    level: int = Field(ge=0, le=10)
    description: str = Field(min_length=1, max_length=1000)


class LongAnswerKey(BaseModel):
    model_answer: str | None = Field(None, max_length=10000)
    rubric: list[RubricLevel] = Field(default_factory=list, max_length=11)


KEY_MODELS: dict[QuestionType, type[BaseModel]] = {
    QuestionType.NUMERIC: NumericKey,
    QuestionType.SHORT_ANSWER: ShortAnswerKey,
    QuestionType.LONG_ANSWER: LongAnswerKey,
}


# ----- questions --------------------------------------------------------------

class OptionIn(BaseModel):
    text: str = Field(max_length=2000)
    is_correct: bool = False


class QuestionIn(BaseModel):
    class_id: Id
    subject_id: Id
    topic_id: Id
    type: QuestionType
    stem: str = Field(min_length=1, max_length=10000)
    explanation: str | None = Field(None, max_length=10000)
    difficulty: Difficulty
    bloom_level: BloomLevel
    marks: Decimal = Field(Decimal("1"), gt=0, le=100, max_digits=5, decimal_places=2)
    options: list[OptionIn] = Field(default_factory=list, max_length=rules.MAX_OPTIONS)
    answer_key: dict | None = None

    @model_validator(mode="after")
    def _structure_matches_type(self) -> "QuestionIn":
        if not self.stem.strip():
            raise ValueError("The question text is empty.")
        rules.check_options(self.type, [(o.text, o.is_correct) for o in self.options])
        if self.type in KEY_MODELS and self.answer_key is not None:
            # Normalise through the typed model: unknown keys dropped, values coerced.
            # A nested ValidationError would escape FastAPI's 422 handling, so it is
            # re-raised as the ValueError pydantic expects from a validator.
            try:
                self.answer_key = KEY_MODELS[self.type].model_validate(self.answer_key).model_dump()
            except ValidationError as exc:
                first = exc.errors()[0]
                where = ".".join(str(part) for part in first["loc"]) or "answer_key"
                raise ValueError(f"Answer key {where}: {first['msg']}") from None
        rules.check_answer_key(self.type, self.answer_key)
        return self


class OptionOut(BaseModel):
    position: int
    text: str
    is_correct: bool


class ItemParamsOut(BaseModel):
    model: str
    a: float
    b: float
    c: float
    is_prior: bool
    n_responses: int
    calibrated_at: datetime | None


class QuestionOut(BaseModel):
    id: int
    class_id: int
    subject_id: int
    topic_id: int
    topic_name: str
    topic_path: str  # "Chapter › Unit" for a unit, the chapter's name otherwise
    type: QuestionType
    stem: str
    explanation: str | None
    difficulty: Difficulty
    bloom_level: BloomLevel
    marks: float
    status: QuestionStatus
    version: int
    previous_version_id: int | None
    source: QuestionSource
    options: list[OptionOut]
    answer_key: dict | None
    params: ItemParamsOut | None
    created_by: int
    created_at: datetime
    updated_by: int
    updated_at: datetime


class QuestionSaveOut(BaseModel):
    question: QuestionOut
    # True when the edited question had already been answered, so the edit was saved
    # as a NEW draft version and the original was left untouched.
    new_version_created: bool


class QuestionListItem(BaseModel):
    id: int
    topic_id: int
    topic_name: str
    topic_path: str
    type: QuestionType
    stem_preview: str
    difficulty: Difficulty
    bloom_level: BloomLevel
    marks: float
    status: QuestionStatus
    version: int
    option_count: int
    updated_at: datetime


class QuestionPage(BaseModel):
    items: list[QuestionListItem]
    total: int
    page: int
    page_size: int


class StatusChange(BaseModel):
    status: Literal[QuestionStatus.PILOT, QuestionStatus.DRAFT, QuestionStatus.RETIRED]
