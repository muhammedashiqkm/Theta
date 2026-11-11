from datetime import datetime
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, Field, StringConstraints, model_validator

Id = Annotated[int, Field(gt=0)]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
ResultPolicy = Literal["immediate", "after_close", "hidden"]


# ----- teacher: define and publish ----------------------------------------------

class ChapterShareIn(BaseModel):
    topic_id: Id
    share: int = Field(ge=1, le=100)


class SectionIn(BaseModel):
    class_group_division_id: Id
    group_id: Id
    division_id: Id
    label: Name


class AssessmentIn(BaseModel):
    class_id: Id
    subject_id: Id
    title: Name
    instructions: str | None = Field(None, max_length=5000)
    starts_at: AwareDatetime
    ends_at: AwareDatetime
    duration_minutes: int = Field(ge=1, le=300)
    min_questions: int = Field(ge=1, le=100)
    max_questions: int = Field(ge=1, le=100)
    target_se: float = Field(ge=0.2, le=1.0)
    result_policy: ResultPolicy = "immediate"
    chapters: list[ChapterShareIn] = Field(min_length=1, max_length=50)
    sections: list[SectionIn] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _consistent(self) -> "AssessmentIn":
        if self.ends_at <= self.starts_at:
            raise ValueError("The test must end after it starts.")
        if self.min_questions > self.max_questions:
            raise ValueError("The minimum number of questions cannot exceed the maximum.")
        if len({c.topic_id for c in self.chapters}) != len(self.chapters):
            raise ValueError("A chapter is listed twice.")
        if sum(c.share for c in self.chapters) != 100:
            raise ValueError("Chapter shares must add up to 100%.")
        if len({s.class_group_division_id for s in self.sections}) != len(self.sections):
            raise ValueError("A section is listed twice.")
        return self


class CandidateIn(BaseModel):
    student_id: Id
    name: Name
    roll_no: str | None = Field(None, max_length=40)
    class_group_division_id: Id


class PublishIn(BaseModel):
    candidates: list[CandidateIn] = Field(min_length=1, max_length=5000)


class ChapterOut(BaseModel):
    topic_id: int
    name: str
    share: int
    # Approved (pilot or active), automatically marked questions in the chapter and its units.
    eligible_questions: int


class SectionOut(BaseModel):
    class_group_division_id: int
    group_id: int
    division_id: int
    label: str


class PoolOut(BaseModel):
    total: int
    easy: int
    medium: int
    hard: int


class AssessmentOut(BaseModel):
    id: int
    class_id: int
    subject_id: int
    title: str
    instructions: str | None
    status: str
    is_open: bool
    starts_at: datetime
    ends_at: datetime
    duration_minutes: int
    min_questions: int
    max_questions: int
    target_se: float
    result_policy: ResultPolicy
    chapters: list[ChapterOut]
    sections: list[SectionOut]
    pool: PoolOut
    candidates: int
    published_at: datetime | None
    closed_at: datetime | None


class AssessmentListItem(BaseModel):
    id: int
    title: str
    status: str
    is_open: bool
    starts_at: datetime
    ends_at: datetime
    duration_minutes: int
    max_questions: int
    sections: list[str]
    candidates: int
    started: int
    completed: int


class ResultRow(BaseModel):
    student_id: int
    name: str
    roll_no: str | None
    section: str
    status: Literal["not_started", "in_progress", "completed"]
    answered: int
    correct: int
    theta: float | None
    se: float | None
    scaled_score: int | None
    level: str | None
    provisional: bool
    finish_reason: str | None
    started_at: datetime | None
    finished_at: datetime | None


class AssessmentResults(BaseModel):
    assessment: AssessmentListItem
    rows: list[ResultRow]


# ----- student: sit the test ----------------------------------------------------

class OptionForStudent(BaseModel):
    position: int
    text: str


class QuestionForStudent(BaseModel):
    """What the student may see. Never the key, the options' correctness or the parameters."""

    seq: int
    question_id: int
    type: str
    stem: str
    marks: float
    options: list[OptionForStudent]
    unit: str | None


class SessionView(BaseModel):
    session_id: int
    assessment_id: int
    title: str
    status: Literal["in_progress", "completed"]
    answered: int
    min_questions: int
    max_questions: int
    seconds_left: int
    deadline_at: datetime
    finish_reason: str | None
    question: QuestionForStudent | None
    result_available: bool


class AnswerIn(BaseModel):
    seq: int = Field(ge=1)
    question_id: Id
    # {"positions": [2]} for choice questions, {"value": 9.8} numeric, {"text": "…"} short answer.
    answer: dict


class StudentTestItem(BaseModel):
    assessment_id: int
    title: str
    instructions: str | None
    starts_at: datetime
    ends_at: datetime
    duration_minutes: int
    max_questions: int
    availability: Literal["upcoming", "open", "ended"]
    session_status: Literal["not_started", "in_progress", "completed"]
    session_id: int | None
    result_available: bool


class StudentResult(BaseModel):
    assessment_id: int
    title: str
    answered: int
    scaled_score: int
    level: str
    theta: float
    se: float
    provisional: bool
    finish_reason: str | None
    finished_at: datetime | None
