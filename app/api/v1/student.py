from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.assessments import delivery
from app.assessments.schemas import AnswerIn, SessionView, StudentResult, StudentTestItem
from app.core.db import get_session
from app.core.security import Student, require_student

router = APIRouter(prefix="/v1/student", tags=["student"])

Session = Annotated[AsyncSession, Depends(get_session)]
Me = Annotated[Student, Depends(require_student)]


@router.get("/tests", response_model=list[StudentTestItem])
async def my_tests(session: Session, student: Me):
    return await delivery.my_tests(session, student)


@router.post("/tests/{assessment_id}/start", response_model=SessionView)
async def start(session: Session, student: Me, assessment_id: int):
    """Starts the attempt, or resumes it if already started."""
    return await delivery.start(session, student, assessment_id)


@router.get("/tests/{assessment_id}/result", response_model=StudentResult)
async def result(session: Session, student: Me, assessment_id: int):
    return await delivery.result(session, student, assessment_id)


@router.get("/sessions/{session_id}", response_model=SessionView)
async def get_session_view(session: Session, student: Me, session_id: int):
    return await delivery.get_session(session, student, session_id)


@router.post("/sessions/{session_id}/answer", response_model=SessionView)
async def answer(session: Session, student: Me, session_id: int, body: AnswerIn):
    return await delivery.answer(session, student, session_id, body)


@router.post("/sessions/{session_id}/finish", response_model=SessionView)
async def finish(session: Session, student: Me, session_id: int):
    return await delivery.finish(session, student, session_id)
