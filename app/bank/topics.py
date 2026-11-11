"""Topics: the chapter → unit tree a question bank is organised by, per class and subject."""

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import audit
from app.bank.enums import QuestionStatus, TopicLevel
from app.bank.models import Question, Topic
from app.bank.schemas import SyllabusNode, TopicCreate, TopicImport, TopicImportOut, TopicOut, TopicUpdate
from app.core.errors import Conflict, Forbidden, NotFound
from app.core.security import Caller, TeachingScope

DUPLICATE_NAME = "A topic with this name already exists at this level."


def ensure_in_scope(scope: TeachingScope, class_id: int, subject_id: int) -> None:
    if not scope.allows(class_id, subject_id):
        raise Forbidden("This class and subject are outside your teaching scope.")


async def load_topic(session: AsyncSession, caller: Caller, scope: TeachingScope, topic_id: int) -> Topic:
    """The active topic, or NotFound when it is missing, another institution's, or out of scope.
    Out-of-scope reads as missing so a teacher cannot probe for other classes' topics."""
    topic = await session.scalar(
        select(Topic).where(Topic.id == topic_id, Topic.institution_id == caller.institution_id, Topic.is_active)
    )
    if topic is None or not scope.allows(topic.class_id, topic.subject_id):
        raise NotFound("Topic not found.")
    return topic


def topic_and_units(topic_id: int):
    """IDs covered by a topic: itself plus, for a chapter, its units. Picking a chapter
    therefore includes everything filed under its units."""
    return select(Topic.id).where(or_(Topic.id == topic_id, Topic.parent_id == topic_id))


async def list_topics(
    session: AsyncSession, caller: Caller, scope: TeachingScope, class_id: int, subject_id: int
) -> list[TopicOut]:
    ensure_in_scope(scope, class_id, subject_id)
    return await _tree(session, caller.institution_id, class_id, subject_id)


async def create_topic(session: AsyncSession, caller: Caller, scope: TeachingScope, data: TopicCreate) -> TopicOut:
    ensure_in_scope(scope, data.class_id, data.subject_id)
    level = TopicLevel.CHAPTER
    if data.parent_id is not None:
        parent = await load_topic(session, caller, scope, data.parent_id)
        if parent.level != TopicLevel.CHAPTER:
            raise Conflict("A unit can only be added under a chapter.")
        if (parent.class_id, parent.subject_id) != (data.class_id, data.subject_id):
            raise Conflict("The chapter belongs to a different class or subject.")
        level = TopicLevel.UNIT

    topic = Topic(
        institution_id=caller.institution_id,
        class_id=data.class_id,
        subject_id=data.subject_id,
        parent_id=data.parent_id,
        level=level,
        name=data.name,
        name_nat=data.name_nat or None,
        sort_order=data.sort_order,
        created_by=caller.user_id,
        updated_by=caller.user_id,
    )
    session.add(topic)
    await _flush_unique(session)
    audit.record(session, caller, "topic.create", "topic", topic.id, name=topic.name, level=level)
    await session.commit()
    return _out(topic, 0)


async def update_topic(
    session: AsyncSession, caller: Caller, scope: TeachingScope, topic_id: int, data: TopicUpdate
) -> TopicOut:
    topic = await load_topic(session, caller, scope, topic_id)
    old_name = topic.name
    topic.name = data.name
    topic.name_nat = data.name_nat or None
    topic.sort_order = data.sort_order
    topic.updated_by = caller.user_id
    topic.updated_at = func.now()
    await _flush_unique(session)
    audit.record(session, caller, "topic.update", "topic", topic.id, old_name=old_name, name=data.name)
    await session.commit()
    await session.refresh(topic)
    return _out(topic, await _live_question_count(session, topic.id))


async def delete_topic(session: AsyncSession, caller: Caller, scope: TeachingScope, topic_id: int) -> None:
    """Deactivates the topic. Refused while it holds live questions or, for a chapter, active units."""
    topic = await load_topic(session, caller, scope, topic_id)
    if await _live_question_count(session, topic.id):
        raise Conflict("Move or retire this topic's questions before deleting it.")
    if topic.level == TopicLevel.CHAPTER:
        units = await session.scalar(select(func.count()).where(Topic.parent_id == topic.id, Topic.is_active)) or 0
        if units:
            raise Conflict("Delete this chapter's units before deleting the chapter.")
    topic.is_active = False
    topic.updated_by = caller.user_id
    topic.updated_at = func.now()
    audit.record(session, caller, "topic.delete", "topic", topic.id, name=topic.name)
    await session.commit()


async def import_topics(session: AsyncSession, caller: Caller, scope: TeachingScope, data: TopicImport) -> TopicImportOut:
    """Builds or refreshes the chapter → unit tree from the campus syllabus.

    Matching is by NAME within the same parent (case-insensitive), never by syllabus
    node ID, because those IDs change every academic year. A matched topic keeps its
    questions; its order, Arabic name and source node are refreshed. Nothing is ever
    deleted: a topic missing from this year's syllabus stays as it is.
    """
    ensure_in_scope(scope, data.class_id, data.subject_id)
    existing = (
        await session.scalars(
            select(Topic).where(
                Topic.institution_id == caller.institution_id,
                Topic.class_id == data.class_id,
                Topic.subject_id == data.subject_id,
                Topic.is_active,
            )
        )
    ).all()
    by_key: dict[tuple[int | None, str], Topic] = {(t.parent_id, t.name.casefold()): t for t in existing}

    created_chapters = created_units = matched = skipped_subunits = skipped_orphans = 0
    topic_for_source: dict[int, Topic] = {}

    def upsert(node: SyllabusNode, parent: Topic | None) -> bool:
        """Returns True when a new topic was created."""
        key = (parent.id if parent else None, node.name.casefold())
        topic = by_key.get(key)
        created = topic is None
        if created:
            topic = Topic(
                institution_id=caller.institution_id,
                class_id=data.class_id,
                subject_id=data.subject_id,
                parent_id=parent.id if parent else None,
                level=TopicLevel.UNIT if parent else TopicLevel.CHAPTER,
                name=node.name,
                created_by=caller.user_id,
                updated_by=caller.user_id,
            )
            session.add(topic)
            by_key[key] = topic
        else:
            topic.updated_by = caller.user_id
            topic.updated_at = func.now()
        topic.name_nat = node.name_nat or topic.name_nat
        topic.sort_order = node.order
        topic.source_syllabus_detail_id = node.source_id
        topic_for_source[node.source_id] = topic
        return created

    ordered = sorted(data.nodes, key=lambda n: (n.level, n.order, n.source_id))
    for node in (n for n in ordered if n.level == 1):
        if upsert(node, None):
            created_chapters += 1
        else:
            matched += 1
    await session.flush()  # new chapters need IDs before their units can point at them

    for node in (n for n in ordered if n.level == 2):
        parent = topic_for_source.get(node.parent_source_id)
        if parent is None:
            skipped_orphans += 1
            continue
        if upsert(node, parent):
            created_units += 1
        else:
            matched += 1
    skipped_subunits = sum(1 for n in data.nodes if n.level == 3)

    await _flush_unique(session)
    audit.record(
        session, caller, "topic.import", "topic", 0,
        class_id=data.class_id, subject_id=data.subject_id,
        created_chapters=created_chapters, created_units=created_units, matched=matched,
    )
    await session.commit()
    return TopicImportOut(
        created_chapters=created_chapters,
        created_units=created_units,
        matched=matched,
        skipped_subunits=skipped_subunits,
        skipped_orphans=skipped_orphans,
        topics=await _tree(session, caller.institution_id, data.class_id, data.subject_id),
    )


# ----- helpers ----------------------------------------------------------------

async def _tree(session: AsyncSession, institution_id: int, class_id: int, subject_id: int) -> list[TopicOut]:
    """Every active topic, each chapter followed by its units, both in syllabus order."""
    live_questions = (
        select(Question.topic_id, func.count().label("n"))
        .where(Question.status != QuestionStatus.RETIRED)
        .group_by(Question.topic_id)
        .subquery()
    )
    rows = (
        await session.execute(
            select(Topic, func.coalesce(live_questions.c.n, 0))
            .outerjoin(live_questions, live_questions.c.topic_id == Topic.id)
            .where(
                Topic.institution_id == institution_id,
                Topic.class_id == class_id,
                Topic.subject_id == subject_id,
                Topic.is_active,
            )
            .order_by(Topic.sort_order, func.lower(Topic.name))
        )
    ).all()

    chapters = [(t, n) for t, n in rows if t.parent_id is None]
    units_by_parent: dict[int, list[tuple[Topic, int]]] = {}
    for topic, count in rows:
        if topic.parent_id is not None:
            units_by_parent.setdefault(topic.parent_id, []).append((topic, count))

    ordered: list[TopicOut] = []
    for chapter, count in chapters:
        ordered.append(_out(chapter, count))
        ordered.extend(_out(unit, unit_count) for unit, unit_count in units_by_parent.get(chapter.id, []))
    return ordered


async def _live_question_count(session: AsyncSession, topic_id: int) -> int:
    return await session.scalar(
        select(func.count()).where(Question.topic_id == topic_id, Question.status != QuestionStatus.RETIRED)
    ) or 0


async def _flush_unique(session: AsyncSession) -> None:
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        if "uq_topics_active_name" in str(exc.orig):
            raise Conflict(DUPLICATE_NAME) from None
        raise


def _out(topic: Topic, question_count: int) -> TopicOut:
    return TopicOut(
        id=topic.id,
        class_id=topic.class_id,
        subject_id=topic.subject_id,
        parent_id=topic.parent_id,
        level=topic.level,
        name=topic.name,
        name_nat=topic.name_nat,
        sort_order=topic.sort_order,
        source_syllabus_detail_id=topic.source_syllabus_detail_id,
        question_count=question_count,
    )
