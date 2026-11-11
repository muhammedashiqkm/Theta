"""Student side: start a test, answer one question at a time, finish, see the result.

The server is the only place the test runs. The student is shown one question; the
key, the other questions and the parameters never leave this module. Every answer
is checked against the session's expected sequence number under a row lock, so a
double click or a retried request can never be counted twice.
"""

import random
from collections import Counter
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.assessments.models import Assessment, Candidate, Response, TestSession
from app.assessments.pool import chapter_of, fallback_params, load_pool
from app.assessments.schemas import (
    AnswerIn,
    OptionForStudent,
    QuestionForStudent,
    SessionView,
    StudentResult,
    StudentTestItem,
)
from app.bank.models import ItemParams, Question
from app.core.errors import Conflict, Forbidden, NotFound, Unprocessable
from app.core.security import Student
from app.engine import irt
from app.engine.irt import ItemParams as EngineParams
from app.engine.marking import InvalidAnswer, OptionKey, is_correct
from app.engine.selection import choose_next, stop_reason

# An answer that left the browser just before the deadline is still accepted.
ANSWER_GRACE = timedelta(seconds=30)

_rng = random.SystemRandom()


def now() -> datetime:
    return datetime.now(UTC)


async def my_tests(session: AsyncSession, student: Student) -> list[StudentTestItem]:
    rows = (
        await session.execute(
            select(Assessment, TestSession)
            .join(Candidate, (Candidate.assessment_id == Assessment.id) & (Candidate.student_id == student.student_id))
            .outerjoin(
                TestSession,
                (TestSession.assessment_id == Assessment.id) & (TestSession.student_id == student.student_id),
            )
            .where(Assessment.institution_id == student.institution_id, Assessment.status.in_(("published", "closed")))
            .order_by(Assessment.starts_at.desc())
        )
    ).all()
    at = now()
    items = []
    for assessment, attempt in rows:
        if assessment.status == "closed" or at >= assessment.ends_at:
            availability = "ended"
        elif at < assessment.starts_at:
            availability = "upcoming"
        else:
            availability = "open"
        items.append(
            StudentTestItem(
                assessment_id=assessment.id,
                title=assessment.title,
                instructions=assessment.instructions,
                starts_at=assessment.starts_at,
                ends_at=assessment.ends_at,
                duration_minutes=assessment.duration_minutes,
                max_questions=assessment.max_questions,
                availability=availability,
                session_status="not_started" if attempt is None else attempt.status,
                session_id=attempt.id if attempt else None,
                result_available=bool(attempt) and _result_available(assessment, attempt, at),
            )
        )
    return items


async def start(session: AsyncSession, student: Student, assessment_id: int) -> SessionView:
    assessment = await _assessment_for_candidate(session, student, assessment_id)
    existing = await _attempt(session, student, assessment_id=assessment_id, lock=True)
    at = now()
    if existing is not None:
        if existing.status == "in_progress" and at > existing.deadline_at + ANSWER_GRACE:
            _finish(existing, "time_up")
            await session.commit()
        return await _view(session, assessment, existing)

    if assessment.status != "published" or at >= assessment.ends_at:
        raise Conflict("This test has ended.")
    if at < assessment.starts_at:
        raise Conflict("This test has not opened yet.")

    pool = await _pool(session, assessment)
    shares = _shares(assessment)
    first = choose_next(0.0, [p.candidate for p in pool], set(), shares, {}, _rng)
    if first is None:
        raise Conflict("This test has no questions available yet. Please tell your teacher.")

    attempt = TestSession(
        assessment_id=assessment.id,
        institution_id=student.institution_id,
        student_id=student.student_id,
        status="in_progress",
        started_at=at,
        deadline_at=min(at + timedelta(minutes=assessment.duration_minutes), assessment.ends_at),
        current_question_id=first.question_id,
        current_shown_at=at,
    )
    session.add(attempt)
    try:
        await session.commit()
    except IntegrityError:
        # Started twice at the same moment (two tabs): continue the attempt that won.
        await session.rollback()
        attempt = await _attempt(session, student, assessment_id=assessment_id)
        if attempt is None:
            raise
    return await _view(session, assessment, attempt)


async def answer(session: AsyncSession, student: Student, session_id: int, data: AnswerIn) -> SessionView:
    attempt = await _attempt(session, student, session_id=session_id, lock=True)
    if attempt is None:
        raise NotFound("Test attempt not found.")
    assessment = await _assessment(session, attempt.assessment_id)
    if attempt.status == "completed":
        return await _view(session, assessment, attempt)

    at = now()
    if at > attempt.deadline_at + ANSWER_GRACE:
        _finish(attempt, "time_up")
        await session.commit()
        return await _view(session, assessment, attempt)

    previous = (
        await session.scalars(select(Response).where(Response.session_id == attempt.id).order_by(Response.seq))
    ).all()
    if data.seq == attempt.answered and previous and previous[-1].question_id == data.question_id:
        return await _view(session, assessment, attempt)  # a retry of the answer already recorded
    if data.seq != attempt.answered + 1 or data.question_id != attempt.current_question_id:
        raise Conflict("This answer is out of date. Reload the test to continue.")

    question = await session.scalar(
        select(Question)
        .where(Question.id == data.question_id)
        .options(selectinload(Question.options), selectinload(Question.params), selectinload(Question.topic))
    )
    if question is None:
        raise NotFound("Question not found.")
    try:
        correct = is_correct(
            question.type,
            [OptionKey(o.position, o.is_correct) for o in question.options],
            question.answer_key,
            data.answer,
        )
    except InvalidAnswer as exc:
        raise Unprocessable(str(exc)) from None

    params = (
        EngineParams(a=float(question.params.a), b=float(question.params.b), c=float(question.params.c))
        if question.params
        else fallback_params(question)
    )
    params_were_prior = question.params.is_prior if question.params else True
    history = [(EngineParams(float(r.a), float(r.b), float(r.c)), r.is_correct) for r in previous]
    estimate = irt.estimate([*history, (params, correct)])

    shown_at = attempt.current_shown_at or at
    session.add(
        Response(
            session_id=attempt.id,
            seq=data.seq,
            question_id=question.id,
            chapter_id=chapter_of(question.topic),
            answer=data.answer,
            is_correct=correct,
            shown_at=shown_at,
            answered_at=at,
            response_ms=max(int((at - shown_at).total_seconds() * 1000), 0),
            a=params.a,
            b=params.b,
            c=params.c,
            params_were_prior=params_were_prior,
            theta_after=round(estimate.theta, 3),
            se_after=round(estimate.se, 3),
        )
    )
    if question.params:
        await session.execute(
            update(ItemParams).where(ItemParams.question_id == question.id).values(n_responses=ItemParams.n_responses + 1)
        )

    attempt.answered += 1
    attempt.correct += int(correct)
    attempt.theta = round(estimate.theta, 3)
    attempt.se = round(estimate.se, 3)
    attempt.provisional = attempt.provisional or params_were_prior

    pool = await _pool(session, assessment)
    used = {r.question_id for r in previous} | {question.id}
    served = Counter(r.chapter_id for r in previous)
    served[chapter_of(question.topic)] += 1
    reason = stop_reason(
        answered=attempt.answered,
        se=estimate.se,
        min_questions=assessment.min_questions,
        max_questions=assessment.max_questions,
        target_se=float(assessment.target_se),
        seconds_left=(attempt.deadline_at - at).total_seconds(),
        pool_left=sum(1 for p in pool if p.candidate.question_id not in used),
    )
    if reason is None:
        nxt = choose_next(estimate.theta, [p.candidate for p in pool], used, _shares(assessment), dict(served), _rng)
        if nxt is None:
            reason = "pool_exhausted"
        else:
            attempt.current_question_id = nxt.question_id
            attempt.current_shown_at = at
    if reason is not None:
        _finish(attempt, reason)

    await session.commit()
    return await _view(session, assessment, attempt)


async def finish(session: AsyncSession, student: Student, session_id: int) -> SessionView:
    attempt = await _attempt(session, student, session_id=session_id, lock=True)
    if attempt is None:
        raise NotFound("Test attempt not found.")
    if attempt.status == "in_progress":
        _finish(attempt, "time_up" if now() > attempt.deadline_at + ANSWER_GRACE else "student")
        await session.commit()
    return await _view(session, await _assessment(session, attempt.assessment_id), attempt)


async def get_session(session: AsyncSession, student: Student, session_id: int) -> SessionView:
    attempt = await _attempt(session, student, session_id=session_id, lock=True)
    if attempt is None:
        raise NotFound("Test attempt not found.")
    if attempt.status == "in_progress" and now() > attempt.deadline_at + ANSWER_GRACE:
        _finish(attempt, "time_up")
        await session.commit()
    return await _view(session, await _assessment(session, attempt.assessment_id), attempt)


async def result(session: AsyncSession, student: Student, assessment_id: int) -> StudentResult:
    assessment = await _assessment_for_candidate(session, student, assessment_id)
    attempt = await _attempt(session, student, assessment_id=assessment_id)
    if attempt is None or attempt.status != "completed":
        raise Conflict("Finish the test to see your result.")
    if not _result_available(assessment, attempt, now()):
        raise Forbidden(
            "Your teacher has not released the results yet."
            if assessment.result_policy == "hidden"
            else "Results are shown after the test closes."
        )
    theta = float(attempt.theta)
    return StudentResult(
        assessment_id=assessment.id,
        title=assessment.title,
        answered=attempt.answered,
        scaled_score=irt.scaled_score(theta),
        level=irt.level_for(theta),
        theta=theta,
        se=float(attempt.se),
        provisional=attempt.provisional,
        finish_reason=attempt.finish_reason,
        finished_at=attempt.finished_at,
    )


# ----- helpers ----------------------------------------------------------------

async def _assessment(session: AsyncSession, assessment_id: int) -> Assessment:
    return await session.scalar(
        select(Assessment).where(Assessment.id == assessment_id).options(selectinload(Assessment.chapters))
    )


async def _assessment_for_candidate(session: AsyncSession, student: Student, assessment_id: int) -> Assessment:
    """The test, or NotFound unless this student is on its roster and it has been published."""
    assessment = await session.scalar(
        select(Assessment)
        .join(Candidate, (Candidate.assessment_id == Assessment.id) & (Candidate.student_id == student.student_id))
        .where(
            Assessment.id == assessment_id,
            Assessment.institution_id == student.institution_id,
            Assessment.status.in_(("published", "closed")),
        )
        .options(selectinload(Assessment.chapters))
    )
    if assessment is None:
        raise NotFound("Test not found.")
    return assessment


async def _attempt(
    session: AsyncSession, student: Student, *, session_id: int | None = None, assessment_id: int | None = None, lock: bool = False
) -> TestSession | None:
    query = select(TestSession).where(
        TestSession.student_id == student.student_id, TestSession.institution_id == student.institution_id
    )
    if session_id is not None:
        query = query.where(TestSession.id == session_id)
    if assessment_id is not None:
        query = query.where(TestSession.assessment_id == assessment_id)
    if lock:
        query = query.with_for_update()
    return await session.scalar(query.execution_options(populate_existing=True))


async def _pool(session: AsyncSession, assessment: Assessment):
    return await load_pool(
        session, assessment.institution_id, assessment.class_id, assessment.subject_id, [c.topic_id for c in assessment.chapters]
    )


def _shares(assessment: Assessment) -> dict[int, float]:
    return {c.topic_id: c.share / 100 for c in assessment.chapters}


def _finish(attempt: TestSession, reason: str) -> None:
    attempt.status = "completed"
    attempt.finish_reason = reason
    attempt.finished_at = now()
    attempt.current_question_id = None
    attempt.current_shown_at = None


def _result_available(assessment: Assessment, attempt: TestSession, at: datetime) -> bool:
    if attempt.status != "completed":
        return False
    if assessment.result_policy == "immediate":
        return True
    if assessment.result_policy == "after_close":
        return assessment.status == "closed" or at >= assessment.ends_at
    return False


async def _view(session: AsyncSession, assessment: Assessment, attempt: TestSession) -> SessionView:
    at = now()
    question = None
    if attempt.status == "in_progress" and attempt.current_question_id is not None:
        q = await session.scalar(
            select(Question).where(Question.id == attempt.current_question_id).options(selectinload(Question.options))
        )
        if q is not None:
            question = QuestionForStudent(
                seq=attempt.answered + 1,
                question_id=q.id,
                type=q.type,
                stem=q.stem,
                marks=float(q.marks),
                options=[OptionForStudent(position=o.position, text=o.text) for o in q.options],
                unit=(q.answer_key or {}).get("unit") if q.type == "numeric" else None,
            )
    return SessionView(
        session_id=attempt.id,
        assessment_id=assessment.id,
        title=assessment.title,
        status=attempt.status,
        answered=attempt.answered,
        min_questions=assessment.min_questions,
        max_questions=assessment.max_questions,
        seconds_left=max(int((attempt.deadline_at - at).total_seconds()), 0) if attempt.status == "in_progress" else 0,
        deadline_at=attempt.deadline_at,
        finish_reason=attempt.finish_reason,
        question=question,
        result_available=_result_available(assessment, attempt, at),
    )
