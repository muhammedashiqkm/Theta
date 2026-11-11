"""Teacher side: define, publish and close adaptive tests, and read their results."""

from collections import Counter
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app import audit
from app.assessments.models import Assessment, AssessmentChapter, AssessmentSection, Candidate, TestSession
from app.assessments.pool import load_pool
from app.assessments.schemas import (
    AssessmentIn,
    AssessmentListItem,
    AssessmentOut,
    AssessmentResults,
    ChapterOut,
    PoolOut,
    PublishIn,
    ResultRow,
    SectionOut,
)
from app.bank.models import Topic
from app.bank.topics import ensure_in_scope
from app.core.errors import Conflict, NotFound
from app.core.security import Caller, TeachingScope
from app.engine import irt


def now() -> datetime:
    return datetime.now(UTC)


def is_open(assessment: Assessment, at: datetime) -> bool:
    return assessment.status == "published" and assessment.starts_at <= at < assessment.ends_at


async def list_assessments(
    session: AsyncSession, caller: Caller, scope: TeachingScope, class_id: int, subject_id: int
) -> list[AssessmentListItem]:
    ensure_in_scope(scope, class_id, subject_id)
    assessments = (
        await session.scalars(
            select(Assessment)
            .where(
                Assessment.institution_id == caller.institution_id,
                Assessment.class_id == class_id,
                Assessment.subject_id == subject_id,
            )
            .options(selectinload(Assessment.sections))
            .order_by(Assessment.starts_at.desc(), Assessment.id.desc())
        )
    ).all()
    ids = [a.id for a in assessments]
    candidates = await _count_by_assessment(session, select(Candidate.assessment_id, func.count()).where(Candidate.assessment_id.in_(ids)))
    started = await _count_by_assessment(session, select(TestSession.assessment_id, func.count()).where(TestSession.assessment_id.in_(ids)))
    completed = await _count_by_assessment(
        session,
        select(TestSession.assessment_id, func.count()).where(TestSession.assessment_id.in_(ids), TestSession.status == "completed"),
    )
    at = now()
    return [_list_item(a, at, candidates.get(a.id, 0), started.get(a.id, 0), completed.get(a.id, 0)) for a in assessments]


async def get_assessment(session: AsyncSession, caller: Caller, scope: TeachingScope, assessment_id: int) -> AssessmentOut:
    assessment = await _load(session, caller, scope, assessment_id)
    return await _out(session, assessment)


async def create_assessment(session: AsyncSession, caller: Caller, scope: TeachingScope, data: AssessmentIn) -> AssessmentOut:
    ensure_in_scope(scope, data.class_id, data.subject_id)
    await _check_chapters(session, caller, data)
    assessment = Assessment(
        institution_id=caller.institution_id,
        class_id=data.class_id,
        subject_id=data.subject_id,
        created_by=caller.user_id,
        updated_by=caller.user_id,
    )
    _apply(assessment, data)
    session.add(assessment)
    await session.flush()
    audit.record(session, caller, "assessment.create", "assessment", assessment.id, title=data.title)
    await session.commit()
    return await get_assessment(session, caller, scope, assessment.id)


async def update_assessment(
    session: AsyncSession, caller: Caller, scope: TeachingScope, assessment_id: int, data: AssessmentIn
) -> AssessmentOut:
    assessment = await _load(session, caller, scope, assessment_id)
    _require_draft(assessment, "edited")
    if (data.class_id, data.subject_id) != (assessment.class_id, assessment.subject_id):
        raise Conflict("A test cannot move to another class or subject.")
    await _check_chapters(session, caller, data)
    # Composite primary keys: remove the old rows before inserting their replacements.
    assessment.chapters.clear()
    assessment.sections.clear()
    await session.flush()
    _apply(assessment, data)
    assessment.updated_by = caller.user_id
    assessment.updated_at = func.now()
    audit.record(session, caller, "assessment.update", "assessment", assessment.id)
    await session.commit()
    return await get_assessment(session, caller, scope, assessment.id)


async def delete_assessment(session: AsyncSession, caller: Caller, scope: TeachingScope, assessment_id: int) -> None:
    assessment = await _load(session, caller, scope, assessment_id)
    _require_draft(assessment, "deleted")
    audit.record(session, caller, "assessment.delete", "assessment", assessment.id, title=assessment.title)
    await session.delete(assessment)
    await session.commit()


async def publish_assessment(
    session: AsyncSession, caller: Caller, scope: TeachingScope, assessment_id: int, data: PublishIn
) -> AssessmentOut:
    assessment = await _load(session, caller, scope, assessment_id)
    _require_draft(assessment, "published")
    at = now()
    if assessment.ends_at <= at:
        raise Conflict("The test window has already ended. Change the dates before publishing.")

    chapter_ids = [c.topic_id for c in assessment.chapters]
    pool = await load_pool(session, assessment.institution_id, assessment.class_id, assessment.subject_id, chapter_ids)
    if len(pool) < assessment.min_questions:
        raise Conflict(
            f"Only {len(pool)} approved questions match these chapters; the test needs at least {assessment.min_questions}."
        )
    per_chapter = Counter(item.candidate.chapter_id for item in pool)
    empty = [c.topic_id for c in assessment.chapters if per_chapter[c.topic_id] == 0]
    if empty:
        names = await session.scalars(select(Topic.name).where(Topic.id.in_(empty)))
        raise Conflict("These chapters have no approved questions: " + ", ".join(names) + ".")

    sections = {s.class_group_division_id for s in assessment.sections}
    roster: dict[int, Candidate] = {}
    for c in data.candidates:
        if c.class_group_division_id not in sections:
            raise Conflict(f"{c.name} is not in one of this test's sections.")
        roster[c.student_id] = Candidate(
            assessment_id=assessment.id,
            student_id=c.student_id,
            name=c.name,
            roll_no=(c.roll_no or "").strip() or None,
            class_group_division_id=c.class_group_division_id,
        )
    session.add_all(roster.values())

    assessment.status = "published"
    assessment.published_by = caller.user_id
    assessment.published_at = func.now()
    audit.record(session, caller, "assessment.publish", "assessment", assessment.id, candidates=len(roster), pool=len(pool))
    await session.commit()
    return await get_assessment(session, caller, scope, assessment.id)


async def close_assessment(session: AsyncSession, caller: Caller, scope: TeachingScope, assessment_id: int) -> AssessmentOut:
    """Ends the test for everyone. Attempts still in progress are finished with what they have."""
    assessment = await _load(session, caller, scope, assessment_id)
    if assessment.status != "published":
        raise Conflict("Only a published test can be closed.")
    running = (
        await session.scalars(
            select(TestSession).where(TestSession.assessment_id == assessment.id, TestSession.status == "in_progress").with_for_update()
        )
    ).all()
    for attempt in running:
        attempt.status = "completed"
        attempt.finish_reason = "closed"
        attempt.finished_at = func.now()
        attempt.current_question_id = None
    assessment.status = "closed"
    assessment.closed_by = caller.user_id
    assessment.closed_at = func.now()
    audit.record(session, caller, "assessment.close", "assessment", assessment.id, finished_in_progress=len(running))
    await session.commit()
    return await get_assessment(session, caller, scope, assessment.id)


async def assessment_results(session: AsyncSession, caller: Caller, scope: TeachingScope, assessment_id: int) -> AssessmentResults:
    assessment = await _load(session, caller, scope, assessment_id)
    candidates = (
        await session.scalars(select(Candidate).where(Candidate.assessment_id == assessment.id).order_by(Candidate.name))
    ).all()
    attempts = {
        s.student_id: s
        for s in (await session.scalars(select(TestSession).where(TestSession.assessment_id == assessment.id))).all()
    }
    labels = {s.class_group_division_id: s.label for s in assessment.sections}

    rows = []
    for c in candidates:
        attempt = attempts.get(c.student_id)
        done = attempt is not None and attempt.status == "completed"
        theta = float(attempt.theta) if attempt else None
        rows.append(
            ResultRow(
                student_id=c.student_id,
                name=c.name,
                roll_no=c.roll_no,
                section=labels.get(c.class_group_division_id, ""),
                status="not_started" if attempt is None else attempt.status,
                answered=attempt.answered if attempt else 0,
                correct=attempt.correct if attempt else 0,
                theta=theta,
                se=float(attempt.se) if attempt else None,
                scaled_score=irt.scaled_score(theta) if done and theta is not None else None,
                level=irt.level_for(theta) if done and theta is not None else None,
                provisional=bool(attempt and attempt.provisional),
                finish_reason=attempt.finish_reason if attempt else None,
                started_at=attempt.started_at if attempt else None,
                finished_at=attempt.finished_at if attempt else None,
            )
        )
    started = sum(1 for r in rows if r.status != "not_started")
    completed = sum(1 for r in rows if r.status == "completed")
    return AssessmentResults(assessment=_list_item(assessment, now(), len(rows), started, completed), rows=rows)


# ----- helpers ----------------------------------------------------------------

async def _load(session: AsyncSession, caller: Caller, scope: TeachingScope, assessment_id: int) -> Assessment:
    assessment = await session.scalar(
        select(Assessment)
        .where(Assessment.id == assessment_id, Assessment.institution_id == caller.institution_id)
        .options(selectinload(Assessment.chapters), selectinload(Assessment.sections))
        .execution_options(populate_existing=True)
    )
    if assessment is None or not scope.allows(assessment.class_id, assessment.subject_id):
        raise NotFound("Test not found.")
    return assessment


def _require_draft(assessment: Assessment, action: str) -> None:
    if assessment.status != "draft":
        raise Conflict(f"A {assessment.status} test cannot be {action}.")


async def _check_chapters(session: AsyncSession, caller: Caller, data: AssessmentIn) -> None:
    ids = [c.topic_id for c in data.chapters]
    found = set(
        await session.scalars(
            select(Topic.id).where(
                Topic.id.in_(ids),
                Topic.institution_id == caller.institution_id,
                Topic.class_id == data.class_id,
                Topic.subject_id == data.subject_id,
                Topic.parent_id.is_(None),
                Topic.is_active,
            )
        )
    )
    if missing := [i for i in ids if i not in found]:
        raise Conflict(f"Topic {missing[0]} is not a chapter of this class and subject.")


def _apply(assessment: Assessment, data: AssessmentIn) -> None:
    assessment.title = data.title
    assessment.instructions = (data.instructions or "").strip() or None
    assessment.starts_at = data.starts_at
    assessment.ends_at = data.ends_at
    assessment.duration_minutes = data.duration_minutes
    assessment.min_questions = data.min_questions
    assessment.max_questions = data.max_questions
    assessment.target_se = data.target_se
    assessment.result_policy = data.result_policy
    assessment.chapters = [AssessmentChapter(topic_id=c.topic_id, share=c.share) for c in data.chapters]
    assessment.sections = [
        AssessmentSection(
            class_group_division_id=s.class_group_division_id, group_id=s.group_id, division_id=s.division_id, label=s.label
        )
        for s in data.sections
    ]


async def _out(session: AsyncSession, assessment: Assessment) -> AssessmentOut:
    chapter_ids = [c.topic_id for c in assessment.chapters]
    names = dict((await session.execute(select(Topic.id, Topic.name).where(Topic.id.in_(chapter_ids)))).all()) if chapter_ids else {}
    pool = await load_pool(session, assessment.institution_id, assessment.class_id, assessment.subject_id, chapter_ids)
    per_chapter = Counter(item.candidate.chapter_id for item in pool)
    by_difficulty = Counter(item.difficulty for item in pool)
    candidates = await session.scalar(select(func.count()).where(Candidate.assessment_id == assessment.id)) or 0
    return AssessmentOut(
        id=assessment.id,
        class_id=assessment.class_id,
        subject_id=assessment.subject_id,
        title=assessment.title,
        instructions=assessment.instructions,
        status=assessment.status,
        is_open=is_open(assessment, now()),
        starts_at=assessment.starts_at,
        ends_at=assessment.ends_at,
        duration_minutes=assessment.duration_minutes,
        min_questions=assessment.min_questions,
        max_questions=assessment.max_questions,
        target_se=float(assessment.target_se),
        result_policy=assessment.result_policy,
        chapters=sorted(
            (ChapterOut(topic_id=c.topic_id, name=names.get(c.topic_id, ""), share=c.share, eligible_questions=per_chapter[c.topic_id])
             for c in assessment.chapters),
            key=lambda ch: -ch.share,
        ),
        sections=[
            SectionOut(class_group_division_id=s.class_group_division_id, group_id=s.group_id, division_id=s.division_id, label=s.label)
            for s in sorted(assessment.sections, key=lambda s: s.label)
        ],
        pool=PoolOut(total=len(pool), easy=by_difficulty["easy"], medium=by_difficulty["medium"], hard=by_difficulty["hard"]),
        candidates=candidates,
        published_at=assessment.published_at,
        closed_at=assessment.closed_at,
    )


def _list_item(assessment: Assessment, at: datetime, candidates: int, started: int, completed: int) -> AssessmentListItem:
    return AssessmentListItem(
        id=assessment.id,
        title=assessment.title,
        status=assessment.status,
        is_open=is_open(assessment, at),
        starts_at=assessment.starts_at,
        ends_at=assessment.ends_at,
        duration_minutes=assessment.duration_minutes,
        max_questions=assessment.max_questions,
        sections=sorted(s.label for s in assessment.sections),
        candidates=candidates,
        started=started,
        completed=completed,
    )


async def _count_by_assessment(session: AsyncSession, query) -> dict[int, int]:
    rows = await session.execute(query.group_by(query.selected_columns[0]))
    return {assessment_id: count for assessment_id, count in rows.all()}
