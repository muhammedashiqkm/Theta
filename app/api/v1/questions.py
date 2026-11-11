from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.bank import questions
from app.bank.enums import Difficulty, QuestionStatus, QuestionType
from app.bank.schemas import QuestionIn, QuestionOut, QuestionPage, QuestionSaveOut, StatusChange
from app.core.db import get_session
from app.core.security import Caller, TeachingScope, get_scope, require_staff

router = APIRouter(prefix="/v1/questions", tags=["questions"])

Session = Annotated[AsyncSession, Depends(get_session)]
Staff = Annotated[Caller, Depends(require_staff)]
Scope = Annotated[TeachingScope, Depends(get_scope)]


@router.get("", response_model=QuestionPage)
async def list_questions(
    session: Session, caller: Staff, scope: Scope,
    class_id: Annotated[int, Query(gt=0)],
    subject_id: Annotated[int, Query(gt=0)],
    topic_id: Annotated[int | None, Query(gt=0)] = None,
    status_filter: Annotated[QuestionStatus | None, Query(alias="status")] = None,
    type_filter: Annotated[QuestionType | None, Query(alias="type")] = None,
    difficulty: Difficulty | None = None,
    search: Annotated[str | None, Query(max_length=200)] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=questions.MAX_PAGE_SIZE)] = 25,
):
    return await questions.list_questions(
        session, caller, scope,
        class_id=class_id, subject_id=subject_id, topic_id=topic_id,
        status=status_filter, question_type=type_filter, difficulty=difficulty,
        search=search, page=page, page_size=page_size,
    )


@router.get("/{question_id}", response_model=QuestionOut)
async def get_question(session: Session, caller: Staff, scope: Scope, question_id: int):
    return await questions.get_question(session, caller, scope, question_id)


@router.post("", response_model=QuestionSaveOut, status_code=status.HTTP_201_CREATED)
async def create_question(session: Session, caller: Staff, scope: Scope, body: QuestionIn):
    return await questions.create_question(session, caller, scope, body)


@router.put("/{question_id}", response_model=QuestionSaveOut)
async def update_question(session: Session, caller: Staff, scope: Scope, question_id: int, body: QuestionIn):
    return await questions.update_question(session, caller, scope, question_id, body)


@router.post("/{question_id}/status", response_model=QuestionOut)
async def change_status(session: Session, caller: Staff, scope: Scope, question_id: int, body: StatusChange):
    return await questions.change_status(session, caller, scope, question_id, body.status)


@router.delete("/{question_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_question(session: Session, caller: Staff, scope: Scope, question_id: int):
    await questions.delete_question(session, caller, scope, question_id)
