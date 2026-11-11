"""What OnlineCampus sends to have an answer marked, and what it gets back.

Theta keeps nothing: OnlineCampus stores the verdicts, the mark schemes and the marks.
Names and texts only, never a student's name or id.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints

Language = Literal["en", "ar"]
QuestionText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=10000)]
ModelAnswer = Annotated[str, StringConstraints(strip_whitespace=True, max_length=10000)]
Accepted = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
Subject = Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)]

MAX_POINTS = 8


class ShortMarkIn(BaseModel):
    """One short answer the rules could not decide (not an accepted answer, not a number to compare)."""

    question: QuestionText
    accepted: list[Accepted] = Field(min_length=1, max_length=10)
    # The allowed difference for number answers (7 with 0.1 accepts 6.9 to 7.1).
    tolerance: float | None = Field(None, ge=0, le=1e9)
    case_sensitive: bool = False
    answer: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
    subject_name: Subject | None = None
    # The language the reason is written in (the school's page language).
    language: Language = "en"


class ShortMarkOut(BaseModel):
    correct: bool
    # One sentence for the teacher.
    reason: str
    # The quick check was unsure, so a careful second check decided.
    checked_twice: bool
    model: str


class RubricPoint(BaseModel):
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
    marks: float = Field(gt=0, le=100)


class RubricIn(BaseModel):
    """Write the mark scheme of one long-answer question (once; OnlineCampus freezes it)."""

    question: QuestionText
    model_answer: ModelAnswer = ""
    marks: float = Field(gt=0, le=100)
    subject_name: Subject | None = None
    language: Language = "en"


class RubricOut(BaseModel):
    points: list[RubricPoint]
    total: float
    model: str


class LongMarkIn(BaseModel):
    """Mark one written answer against its mark scheme."""

    question: QuestionText
    model_answer: ModelAnswer = ""
    points: list[RubricPoint] = Field(min_length=1, max_length=MAX_POINTS)
    answer: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20000)]
    subject_name: Subject | None = None
    # The language the feedback is written in.
    language: Language = "en"


class AwardedPoint(BaseModel):
    index: int
    awarded: float
    # The student's own words that earned the marks ("" when none).
    evidence: str


class LongMarkOut(BaseModel):
    points: list[AwardedPoint]
    total: float
    max: float
    # Two or three sentences for the student.
    feedback: str
    # 2 when the two markings agreed within a mark, 3 when a third decided.
    markings: int
    model: str
