"""What OnlineCampus sends to have an answer marked, and what it gets back.

Theta keeps nothing: OnlineCampus stores the verdicts, the mark schemes and the marks.
Names and texts only, never a student's name or id.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints

Language = Literal["en", "ar"]
QuestionText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=10000)]
Accepted = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
Subject = Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)]



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


# ----- a test's results in plain words (the teacher's Results page) -----------------------

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]


class SummaryChapter(BaseModel):
    name: Name
    percent_right: int = Field(ge=0, le=100)
    answers: int = Field(ge=0)


class SummaryDivision(BaseModel):
    name: Name
    average: int | None = Field(None, ge=200, le=800)
    # Chapters under half right in this division.
    weak_chapters: list[Name] = Field(default_factory=list, max_length=40)


class TestSummaryIn(BaseModel):
    """The figures of one class's test. No student names: only counts and chapter names."""

    test_title: Name
    subject_name: Subject | None = None
    students: int = Field(ge=0)
    sat: int = Field(ge=0)
    absent: int = Field(ge=0)
    # Scores on the 200-800 scale; 500 is the scale's middle.
    average: int | None = Field(None, ge=200, le=800)
    previous_average: int | None = Field(None, ge=200, le=800)
    lowest: int | None = Field(None, ge=200, le=800)
    highest: int | None = Field(None, ge=200, le=800)
    beginning: int = Field(0, ge=0)
    developing: int = Field(0, ge=0)
    proficient: int = Field(0, ge=0)
    advanced: int = Field(0, ge=0)
    chapters: list[SummaryChapter] = Field(default_factory=list, max_length=60)
    divisions: list[SummaryDivision] = Field(default_factory=list, max_length=40)
    odd_questions: int = Field(0, ge=0)
    language: Language = "en"


class TestSummaryOut(BaseModel):
    """The summary in blocks: one headline, up to two good points and two points to watch,
    and one teaching tip. `summary` is the same as plain text (all blocks joined)."""

    headline: str = ""
    went_well: list[str] = Field(default_factory=list)
    needs_attention: list[str] = Field(default_factory=list)
    tip: str = ""
    summary: str
    model: str

# ----- one student's progress in a teacher's words (the Student Report) -------------------

Level = Literal["beginning", "developing", "proficient", "advanced"]


class CommentTest(BaseModel):
    score: int = Field(ge=200, le=800)
    level: Level | None = None


class StudentCommentIn(BaseModel):
    """One student's results in a class + subject. No name: the reply says [NAME]."""

    subject_name: Subject | None = None
    # Oldest first; scores on the 200-800 scale.
    tests: list[CommentTest] = Field(min_length=1, max_length=40)
    latest_level: Level | None = None
    strengths: list[Name] = Field(default_factory=list, max_length=20)
    focus: list[Name] = Field(default_factory=list, max_length=20)
    language: Language = "en"


class StudentCommentOut(BaseModel):
    comment: str
    model: str
