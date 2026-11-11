"""Questions: create, edit (in place or as a new version), change status, delete, search."""

from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app import audit
from app.bank import rules
from app.bank.enums import QuestionSource, QuestionStatus, QuestionType
from app.bank.models import ItemParams, Question, QuestionOption, Topic
from app.bank.schemas import (
    ItemParamsOut,
    OptionOut,
    QuestionIn,
    QuestionListItem,
    QuestionOut,
    QuestionPage,
    QuestionSaveOut,
)
from app.bank.topics import ensure_in_scope, load_topic, topic_and_units
from app.core.errors import Conflict, NotFound
from app.core.security import Caller, TeachingScope

MAX_PAGE_SIZE = 100
PREVIEW_LENGTH = 200

_WITH_TOPIC = selectinload(Question.topic).selectinload(Topic.parent)
_WITH_DETAIL = (selectinload(Question.options), selectinload(Question.params), _WITH_TOPIC)


async def list_questions(
    session: AsyncSession,
    caller: Caller,
    scope: TeachingScope,
    *,
    class_id: int,
    subject_id: int,
    topic_id: int | None = None,
    status: QuestionStatus | None = None,
    question_type: QuestionType | None = None,
    difficulty: str | None = None,
    search: str | None = None,
    page: int = 1,
    page_size: int = 25,
) -> QuestionPage:
    ensure_in_scope(scope, class_id, subject_id)
    page = max(page, 1)
    page_size = min(max(page_size, 1), MAX_PAGE_SIZE)

    query = select(Question).where(
        Question.institution_id == caller.institution_id,
        Question.class_id == class_id,
        Question.subject_id == subject_id,
    )
    if topic_id:
        query = query.where(Question.topic_id.in_(topic_and_units(topic_id)))
    if status:
        query = query.where(Question.status == status)
    if question_type:
        query = query.where(Question.type == question_type)
    if difficulty:
        query = query.where(Question.difficulty == difficulty)
    if search and search.strip():
        pattern = "%" + _escape_like(search.strip()) + "%"
        query = query.where(Question.stem.ilike(pattern, escape="\\"))

    total = await session.scalar(select(func.count()).select_from(query.subquery())) or 0
    questions = (
        await session.scalars(
            query.options(selectinload(Question.options), _WITH_TOPIC)
            .order_by(Question.updated_at.desc(), Question.id.desc())
            .limit(page_size)
            .offset((page - 1) * page_size)
        )
    ).all()

    return QuestionPage(
        items=[_list_item(q) for q in questions],
        total=total,
        page=page,
        page_size=page_size,
    )


async def get_question(session: AsyncSession, caller: Caller, scope: TeachingScope, question_id: int) -> QuestionOut:
    return _out(await _load(session, caller, scope, question_id))


async def create_question(
    session: AsyncSession, caller: Caller, scope: TeachingScope, data: QuestionIn, source: QuestionSource = QuestionSource.MANUAL
) -> QuestionSaveOut:
    ensure_in_scope(scope, data.class_id, data.subject_id)
    await _topic_matches(session, caller, scope, data)

    question = Question(
        institution_id=caller.institution_id,
        class_id=data.class_id,
        subject_id=data.subject_id,
        status=QuestionStatus.DRAFT,
        version=1,
        source=source,
        created_by=caller.user_id,
        updated_by=caller.user_id,
    )
    _apply(question, data)
    session.add(question)
    await session.flush()
    audit.record(session, caller, "question.create", "question", question.id, type=data.type)
    await session.commit()
    return QuestionSaveOut(question=await get_question(session, caller, scope, question.id), new_version_created=False)


async def update_question(
    session: AsyncSession, caller: Caller, scope: TeachingScope, question_id: int, data: QuestionIn
) -> QuestionSaveOut:
    current = await _load(session, caller, scope, question_id)
    if (data.class_id, data.subject_id) != (current.class_id, current.subject_id):
        raise Conflict("A question cannot move to another class or subject; create a new one instead.")
    await _topic_matches(session, caller, scope, data)

    n_responses = current.params.n_responses if current.params else 0
    mode = rules.edit_mode(QuestionStatus(current.status), n_responses)

    if mode == "in_place":
        # Old options are deleted before new ones are inserted: the unit of work
        # would otherwise insert first and trip the (question_id, position) key.
        current.options.clear()
        await session.flush()
        _apply(current, data)
        current.updated_by = caller.user_id
        current.updated_at = func.now()
        if current.params and current.params.is_prior:
            _set_prior(current.params, data.type, data.difficulty, len(data.options))
        audit.record(session, caller, "question.update", "question", current.id)
        await session.commit()
        return QuestionSaveOut(question=await get_question(session, caller, scope, current.id), new_version_created=False)

    replacement = Question(
        institution_id=current.institution_id,
        class_id=current.class_id,
        subject_id=current.subject_id,
        status=QuestionStatus.DRAFT,
        version=current.version + 1,
        previous_version_id=current.id,
        source=current.source,
        created_by=caller.user_id,
        updated_by=caller.user_id,
    )
    _apply(replacement, data)
    session.add(replacement)
    await session.flush()
    audit.record(session, caller, "question.new_version", "question", replacement.id, previous_version_id=current.id)
    await session.commit()
    return QuestionSaveOut(question=await get_question(session, caller, scope, replacement.id), new_version_created=True)


async def change_status(
    session: AsyncSession, caller: Caller, scope: TeachingScope, question_id: int, target: QuestionStatus
) -> QuestionOut:
    question = await _load(session, caller, scope, question_id)
    current = QuestionStatus(question.status)
    n_responses = question.params.n_responses if question.params else 0
    rules.check_transition(current, target, n_responses)

    if target == QuestionStatus.PILOT and question.params is None:
        params = ItemParams(question_id=question.id)
        _set_prior(params, QuestionType(question.type), question.difficulty, len(question.options))
        question.params = params
    if target == QuestionStatus.DRAFT and question.params is not None and question.params.is_prior:
        question.params = None  # back to writing: the prior is rebuilt on the next approval

    question.status = target
    question.status_changed_by = caller.user_id
    question.status_changed_at = func.now()
    audit.record(session, caller, "question.status", "question", question.id, old=current, new=target)
    await session.commit()
    return await get_question(session, caller, scope, question.id)


async def delete_question(session: AsyncSession, caller: Caller, scope: TeachingScope, question_id: int) -> None:
    question = await _load(session, caller, scope, question_id)
    rules.can_delete(QuestionStatus(question.status))
    audit.record(session, caller, "question.delete", "question", question.id, stem=question.stem[:PREVIEW_LENGTH])
    await session.delete(question)
    await session.commit()


# ----- helpers ----------------------------------------------------------------

async def _load(session: AsyncSession, caller: Caller, scope: TeachingScope, question_id: int) -> Question:
    """The question, or NotFound when it is missing, another institution's, or out of scope."""
    question = await session.scalar(
        select(Question)
        .where(Question.id == question_id, Question.institution_id == caller.institution_id)
        .options(*_WITH_DETAIL)
        .execution_options(populate_existing=True)
    )
    if question is None or not scope.allows(question.class_id, question.subject_id):
        raise NotFound("Question not found.")
    return question


async def _topic_matches(session: AsyncSession, caller: Caller, scope: TeachingScope, data: QuestionIn) -> None:
    topic = await load_topic(session, caller, scope, data.topic_id)
    if (topic.class_id, topic.subject_id) != (data.class_id, data.subject_id):
        raise Conflict("The topic belongs to a different class or subject.")


def _apply(question: Question, data: QuestionIn) -> None:
    question.topic_id = data.topic_id
    question.type = data.type
    question.stem = data.stem.strip()
    question.explanation = (data.explanation or "").strip() or None
    question.answer_key = data.answer_key
    question.difficulty = data.difficulty
    question.bloom_level = data.bloom_level
    question.marks = data.marks
    question.options = [
        QuestionOption(position=i, text=option.text.strip(), is_correct=option.is_correct)
        for i, option in enumerate(data.options, start=1)
    ]


def _set_prior(params: ItemParams, question_type: QuestionType, difficulty: str, option_count: int) -> None:
    params.model = "1PL"
    params.a = Decimal("1")
    params.b = Decimal(str(rules.PRIOR_DIFFICULTY[difficulty]))
    params.c = Decimal(str(rules.guessing_floor(question_type, option_count)))
    params.is_prior = True


def _topic_path(question: Question) -> str:
    parent = question.topic.parent
    return f"{parent.name} › {question.topic.name}" if parent else question.topic.name


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _list_item(question: Question) -> QuestionListItem:
    stem = " ".join(question.stem.split())
    return QuestionListItem(
        id=question.id,
        topic_id=question.topic_id,
        topic_name=question.topic.name,
        topic_path=_topic_path(question),
        type=question.type,
        stem_preview=stem if len(stem) <= PREVIEW_LENGTH else stem[: PREVIEW_LENGTH - 1] + "…",
        difficulty=question.difficulty,
        bloom_level=question.bloom_level,
        marks=float(question.marks),
        status=question.status,
        version=question.version,
        option_count=len(question.options),
        updated_at=question.updated_at,
    )


def _out(question: Question) -> QuestionOut:
    params = question.params
    return QuestionOut(
        id=question.id,
        class_id=question.class_id,
        subject_id=question.subject_id,
        topic_id=question.topic_id,
        topic_name=question.topic.name,
        topic_path=_topic_path(question),
        type=question.type,
        stem=question.stem,
        explanation=question.explanation,
        difficulty=question.difficulty,
        bloom_level=question.bloom_level,
        marks=float(question.marks),
        status=question.status,
        version=question.version,
        previous_version_id=question.previous_version_id,
        source=question.source,
        options=[OptionOut(position=o.position, text=o.text, is_correct=o.is_correct) for o in question.options],
        answer_key=question.answer_key,
        params=None
        if params is None
        else ItemParamsOut(
            model=params.model,
            a=float(params.a),
            b=float(params.b),
            c=float(params.c),
            is_prior=params.is_prior,
            n_responses=params.n_responses,
            calibrated_at=params.calibrated_at,
        ),
        created_by=question.created_by,
        created_at=question.created_at,
        updated_by=question.updated_by,
        updated_at=question.updated_at,
    )
