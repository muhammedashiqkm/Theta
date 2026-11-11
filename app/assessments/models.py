"""Tables for adaptive tests. Student and section IDs are OnlineCampus IDs
(StudentAcademicYearID, ClassGroupDivisionID) snapshotted when the test is published."""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

ASSESSMENT_STATUSES = ("draft", "published", "closed")
RESULT_POLICIES = ("immediate", "after_close", "hidden")
SESSION_STATUSES = ("in_progress", "completed")
FINISH_REASONS = ("precision", "max_questions", "pool_exhausted", "time_up", "student", "closed")


def one_of(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class Assessment(Base):
    __tablename__ = "assessments"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    institution_id: Mapped[int] = mapped_column(Integer)
    class_id: Mapped[int] = mapped_column(Integer)
    subject_id: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(200))
    instructions: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(10), server_default="draft")
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    duration_minutes: Mapped[int] = mapped_column(Integer)
    min_questions: Mapped[int] = mapped_column(Integer)
    max_questions: Mapped[int] = mapped_column(Integer)
    target_se: Mapped[Decimal] = mapped_column(Numeric(4, 2))
    result_policy: Mapped[str] = mapped_column(String(12), server_default="immediate")
    created_by: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_by: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    published_by: Mapped[int | None] = mapped_column(Integer)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_by: Mapped[int | None] = mapped_column(Integer)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    chapters: Mapped[list["AssessmentChapter"]] = relationship(cascade="all, delete-orphan", lazy="raise")
    sections: Mapped[list["AssessmentSection"]] = relationship(cascade="all, delete-orphan", lazy="raise")

    __table_args__ = (
        CheckConstraint(one_of("status", ASSESSMENT_STATUSES), name="status"),
        CheckConstraint(one_of("result_policy", RESULT_POLICIES), name="result_policy"),
        CheckConstraint("ends_at > starts_at", name="window"),
        CheckConstraint("duration_minutes BETWEEN 1 AND 300", name="duration"),
        CheckConstraint("min_questions BETWEEN 1 AND max_questions AND max_questions <= 100", name="question_range"),
        CheckConstraint("target_se BETWEEN 0.2 AND 1.0", name="target_se"),
        Index("ix_assessments_bank", "institution_id", "class_id", "subject_id"),
    )


class AssessmentChapter(Base):
    """A chapter in the test's blueprint and its share of the questions, in percent."""

    __tablename__ = "assessment_chapters"

    assessment_id: Mapped[int] = mapped_column(ForeignKey("assessments.id", ondelete="CASCADE"), primary_key=True)
    topic_id: Mapped[int] = mapped_column(ForeignKey("topics.id"), primary_key=True)
    share: Mapped[int] = mapped_column(Integer)

    __table_args__ = (CheckConstraint("share BETWEEN 1 AND 100", name="share"),)


class AssessmentSection(Base):
    __tablename__ = "assessment_sections"

    assessment_id: Mapped[int] = mapped_column(ForeignKey("assessments.id", ondelete="CASCADE"), primary_key=True)
    class_group_division_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[int] = mapped_column(Integer)
    division_id: Mapped[int] = mapped_column(Integer)
    label: Mapped[str] = mapped_column(String(200))


class Candidate(Base):
    """A student allowed to sit the test, snapshotted from the section rosters at publish."""

    __tablename__ = "assessment_candidates"

    assessment_id: Mapped[int] = mapped_column(ForeignKey("assessments.id", ondelete="CASCADE"), primary_key=True)
    student_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    roll_no: Mapped[str | None] = mapped_column(String(40))
    class_group_division_id: Mapped[int] = mapped_column(Integer)

    __table_args__ = (Index("ix_assessment_candidates_student_id", "student_id"),)


class TestSession(Base):
    """One student's attempt. Exactly one per student per test."""

    __tablename__ = "sessions"
    __test__ = False  # not a pytest class, despite the name

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    assessment_id: Mapped[int] = mapped_column(ForeignKey("assessments.id", ondelete="CASCADE"))
    institution_id: Mapped[int] = mapped_column(Integer)
    student_id: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(12), server_default="in_progress")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finish_reason: Mapped[str | None] = mapped_column(String(20))
    theta: Mapped[Decimal] = mapped_column(Numeric(6, 3), server_default="0")
    se: Mapped[Decimal] = mapped_column(Numeric(6, 3), server_default="1")
    answered: Mapped[int] = mapped_column(Integer, server_default="0")
    correct: Mapped[int] = mapped_column(Integer, server_default="0")
    # True while any answered question was scored with a prior (not yet calibrated).
    provisional: Mapped[bool] = mapped_column(Boolean, server_default="false")
    current_question_id: Mapped[int | None] = mapped_column(ForeignKey("questions.id"))
    current_shown_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(one_of("status", SESSION_STATUSES), name="status"),
        CheckConstraint(f"finish_reason IS NULL OR {one_of('finish_reason', FINISH_REASONS)}", name="finish_reason"),
        Index("uq_sessions_student", "assessment_id", "student_id", unique=True),
    )


class Response(Base):
    """One answered question, with the parameters it was scored with (so a later
    calibration never silently changes a result that was already reported)."""

    __tablename__ = "responses"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"))
    seq: Mapped[int] = mapped_column(Integer)
    question_id: Mapped[int] = mapped_column(ForeignKey("questions.id"))
    chapter_id: Mapped[int] = mapped_column(ForeignKey("topics.id"))
    answer: Mapped[dict] = mapped_column(JSONB)
    is_correct: Mapped[bool] = mapped_column(Boolean)
    shown_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    answered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    response_ms: Mapped[int] = mapped_column(Integer)
    a: Mapped[Decimal] = mapped_column(Numeric(6, 3))
    b: Mapped[Decimal] = mapped_column(Numeric(6, 3))
    c: Mapped[Decimal] = mapped_column(Numeric(4, 3))
    params_were_prior: Mapped[bool] = mapped_column(Boolean)
    theta_after: Mapped[Decimal] = mapped_column(Numeric(6, 3))
    se_after: Mapped[Decimal] = mapped_column(Numeric(6, 3))

    __table_args__ = (
        Index("uq_responses_seq", "session_id", "seq", unique=True),
        Index("ix_responses_question_id", "question_id"),
    )
