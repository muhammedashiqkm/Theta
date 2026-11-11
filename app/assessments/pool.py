"""The questions a test may draw from."""

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bank import rules
from app.bank.enums import Difficulty, QuestionType
from app.bank.models import ItemParams, Question, Topic
from app.engine.irt import ItemParams as EngineParams
from app.engine.marking import AUTO_MARKED_TYPES
from app.engine.selection import Candidate

# Approved questions only. Pilots take part (and are scored with their prior until
# calibration replaces it); drafts and retired questions never reach a student.
DELIVERABLE_STATUSES = ("pilot", "active")


@dataclass(frozen=True)
class PoolItem:
    candidate: Candidate
    difficulty: str
    is_prior: bool


def chapter_of(topic: Topic) -> int:
    return topic.parent_id if topic.parent_id is not None else topic.id


async def load_pool(
    session: AsyncSession, institution_id: int, class_id: int, subject_id: int, chapter_ids: list[int]
) -> list[PoolItem]:
    """Every deliverable question in the given chapters, including their units."""
    if not chapter_ids:
        return []
    chapter = func.coalesce(Topic.parent_id, Topic.id)
    rows = await session.execute(
        select(Question.id, Question.difficulty, chapter.label("chapter_id"), ItemParams.a, ItemParams.b, ItemParams.c, ItemParams.is_prior)
        .join(Topic, Topic.id == Question.topic_id)
        .join(ItemParams, ItemParams.question_id == Question.id)
        .where(
            Question.institution_id == institution_id,
            Question.class_id == class_id,
            Question.subject_id == subject_id,
            Question.status.in_(DELIVERABLE_STATUSES),
            Question.type.in_(AUTO_MARKED_TYPES),
            Topic.is_active,
            chapter.in_(chapter_ids),
        )
    )
    return [
        PoolItem(
            candidate=Candidate(
                question_id=row.id,
                chapter_id=row.chapter_id,
                params=EngineParams(a=float(row.a), b=float(row.b), c=float(row.c)),
            ),
            difficulty=row.difficulty,
            is_prior=row.is_prior,
        )
        for row in rows
    ]


def fallback_params(question: Question) -> EngineParams:
    """A question that lost its parameters mid-test (sent back to draft) is still scored,
    with the same prior approval would have given it."""
    return EngineParams(
        a=1.0,
        b=rules.PRIOR_DIFFICULTY[Difficulty(question.difficulty)],
        c=rules.guessing_floor(QuestionType(question.type), len(question.options)),
    )
