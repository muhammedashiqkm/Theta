from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.bank import topics
from app.bank.schemas import TopicCreate, TopicImport, TopicImportOut, TopicOut, TopicUpdate
from app.core.db import get_session
from app.core.security import Caller, TeachingScope, get_scope, require_staff

router = APIRouter(prefix="/v1/topics", tags=["topics"])

Session = Annotated[AsyncSession, Depends(get_session)]
Staff = Annotated[Caller, Depends(require_staff)]
Scope = Annotated[TeachingScope, Depends(get_scope)]


@router.get("", response_model=list[TopicOut])
async def list_topics(
    session: Session, caller: Staff, scope: Scope,
    class_id: Annotated[int, Query(gt=0)],
    subject_id: Annotated[int, Query(gt=0)],
):
    return await topics.list_topics(session, caller, scope, class_id, subject_id)


@router.post("", response_model=TopicOut, status_code=status.HTTP_201_CREATED)
async def create_topic(session: Session, caller: Staff, scope: Scope, body: TopicCreate):
    return await topics.create_topic(session, caller, scope, body)


@router.post("/import", response_model=TopicImportOut)
async def import_topics(session: Session, caller: Staff, scope: Scope, body: TopicImport):
    """Create or refresh the chapter → unit tree from the campus syllabus nodes."""
    return await topics.import_topics(session, caller, scope, body)


@router.put("/{topic_id}", response_model=TopicOut)
async def update_topic(session: Session, caller: Staff, scope: Scope, topic_id: int, body: TopicUpdate):
    return await topics.update_topic(session, caller, scope, topic_id, body)


@router.delete("/{topic_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_topic(session: Session, caller: Staff, scope: Scope, topic_id: int):
    await topics.delete_topic(session, caller, scope, topic_id)
