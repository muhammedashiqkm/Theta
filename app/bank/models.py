"""Question bank tables. OnlineCampus IDs (institution, class, subject, user) are plain
integers: Theta stores them but never owns or joins those masters."""

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
    SmallInteger,
    String,
    Text,
    func,
    text as sql_text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.bank.enums import BloomLevel, Difficulty, QuestionSource, QuestionStatus, QuestionType, TopicLevel
from app.db.base import Base


def one_of(column: str, values: type) -> str:
    return f"{column} IN ({', '.join(repr(str(v)) for v in values)})"


class Topic(Base):
    """A chapter, or a unit inside a chapter. Year-independent on purpose: the campus
    syllabus gets new node IDs every academic year, and a question bank (with the
    difficulty it has learned) must outlive that. `source_syllabus_detail_id` only
    records which LMSSyllabusInClassDetail node the topic was last matched to."""

    __tablename__ = "topics"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    institution_id: Mapped[int] = mapped_column(Integer)
    class_id: Mapped[int] = mapped_column(Integer)
    subject_id: Mapped[int] = mapped_column(Integer)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("topics.id"))
    level: Mapped[str] = mapped_column(String(10), server_default=str(TopicLevel.CHAPTER))
    name: Mapped[str] = mapped_column(String(200))
    name_nat: Mapped[str | None] = mapped_column(String(200))
    source_syllabus_detail_id: Mapped[int | None] = mapped_column(Integer)
    sort_order: Mapped[int] = mapped_column(Integer, server_default="0")
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=sql_text("true"))
    created_by: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_by: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    parent: Mapped["Topic | None"] = relationship(remote_side=[id], lazy="raise")

    __table_args__ = (
        CheckConstraint(one_of("level", TopicLevel), name="level"),
        # A chapter has no parent; a unit always has one.
        CheckConstraint("(level = 'chapter') = (parent_id IS NULL)", name="level_parent"),
        # Names are unique among siblings, so "Exercises" may exist in every chapter.
        Index(
            "uq_topics_active_name",
            "institution_id", "class_id", "subject_id",
            sql_text("coalesce(parent_id, 0)"), sql_text("lower(name)"),
            unique=True,
            postgresql_where=sql_text("is_active"),
        ),
        Index("ix_topics_parent_id", "parent_id"),
    )


class Question(Base):
    __tablename__ = "questions"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    institution_id: Mapped[int] = mapped_column(Integer)
    class_id: Mapped[int] = mapped_column(Integer)
    subject_id: Mapped[int] = mapped_column(Integer)
    topic_id: Mapped[int] = mapped_column(ForeignKey("topics.id"))
    type: Mapped[str] = mapped_column(String(20))
    stem: Mapped[str] = mapped_column(Text)
    explanation: Mapped[str | None] = mapped_column(Text)
    answer_key: Mapped[dict | None] = mapped_column(JSONB)
    difficulty: Mapped[str] = mapped_column(String(10))
    bloom_level: Mapped[str] = mapped_column(String(12))
    marks: Mapped[Decimal] = mapped_column(Numeric(6, 2), server_default="1")
    status: Mapped[str] = mapped_column(String(10), server_default=str(QuestionStatus.DRAFT))
    version: Mapped[int] = mapped_column(Integer, server_default="1")
    previous_version_id: Mapped[int | None] = mapped_column(ForeignKey("questions.id"))
    source: Mapped[str] = mapped_column(String(10), server_default=str(QuestionSource.MANUAL))
    created_by: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_by: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    status_changed_by: Mapped[int | None] = mapped_column(Integer)
    status_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    topic: Mapped[Topic] = relationship(lazy="raise")
    options: Mapped[list["QuestionOption"]] = relationship(
        order_by="QuestionOption.position", cascade="all, delete-orphan", lazy="raise"
    )
    params: Mapped["ItemParams | None"] = relationship(cascade="all, delete-orphan", lazy="raise")

    __table_args__ = (
        CheckConstraint(one_of("type", QuestionType), name="type"),
        CheckConstraint(one_of("difficulty", Difficulty), name="difficulty"),
        CheckConstraint(one_of("bloom_level", BloomLevel), name="bloom_level"),
        CheckConstraint(one_of("status", QuestionStatus), name="status"),
        CheckConstraint(one_of("source", QuestionSource), name="source"),
        CheckConstraint("marks > 0", name="marks_positive"),
        Index("ix_questions_bank", "institution_id", "class_id", "subject_id", "status"),
        Index("ix_questions_topic_id", "topic_id"),
    )


class QuestionOption(Base):
    __tablename__ = "question_options"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    question_id: Mapped[int] = mapped_column(ForeignKey("questions.id", ondelete="CASCADE"))
    position: Mapped[int] = mapped_column(SmallInteger)
    text: Mapped[str] = mapped_column(Text)
    is_correct: Mapped[bool] = mapped_column(Boolean, server_default=sql_text("false"))

    __table_args__ = (Index("uq_question_options_position", "question_id", "position", unique=True),)


class ItemParams(Base):
    """IRT parameters. Created with a PRIOR when a question is approved (b from the
    teacher's difficulty tag, c from the chance of guessing); replaced by calibration."""

    __tablename__ = "item_params"

    question_id: Mapped[int] = mapped_column(ForeignKey("questions.id", ondelete="CASCADE"), primary_key=True)
    model: Mapped[str] = mapped_column(String(4), server_default="1PL")
    a: Mapped[Decimal] = mapped_column(Numeric(6, 3), server_default="1")
    b: Mapped[Decimal] = mapped_column(Numeric(6, 3))
    c: Mapped[Decimal] = mapped_column(Numeric(4, 3), server_default="0")
    is_prior: Mapped[bool] = mapped_column(Boolean, server_default=sql_text("true"))
    n_responses: Mapped[int] = mapped_column(Integer, server_default="0")
    infit: Mapped[Decimal | None] = mapped_column(Numeric(6, 3))
    outfit: Mapped[Decimal | None] = mapped_column(Numeric(6, 3))
    flags: Mapped[list] = mapped_column(JSONB, server_default=sql_text("'[]'::jsonb"))
    calibrated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint("model IN ('1PL', '2PL', '3PL')", name="model"),
        CheckConstraint("c >= 0 AND c < 1", name="c_range"),
    )


class AuditEntry(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    institution_id: Mapped[int] = mapped_column(Integer)
    actor_user_id: Mapped[int] = mapped_column(Integer)
    actor_role: Mapped[str] = mapped_column(String(10))
    action: Mapped[str] = mapped_column(String(40))
    entity: Mapped[str] = mapped_column(String(30))
    entity_id: Mapped[int] = mapped_column(BigInteger)
    detail: Mapped[dict] = mapped_column(JSONB, server_default=sql_text("'{}'::jsonb"))
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (Index("ix_audit_log_institution_at", "institution_id", "at"),)
