from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.assessments import manage
from app.assessments.schemas import AssessmentIn, AssessmentListItem, AssessmentOut, AssessmentResults, PublishIn
from app.core.db import get_session
from app.core.security import Caller, TeachingScope, get_scope, require_staff

router = APIRouter(prefix="/v1/assessments", tags=["assessments"])

Session = Annotated[AsyncSession, Depends(get_session)]
Staff = Annotated[Caller, Depends(require_staff)]
Scope = Annotated[TeachingScope, Depends(get_scope)]


@router.get("", response_model=list[AssessmentListItem])
async def list_assessments(
    session: Session, caller: Staff, scope: Scope,
    class_id: Annotated[int, Query(gt=0)],
    subject_id: Annotated[int, Query(gt=0)],
):
    return await manage.list_assessments(session, caller, scope, class_id, subject_id)


@router.post("", response_model=AssessmentOut, status_code=status.HTTP_201_CREATED)
async def create_assessment(session: Session, caller: Staff, scope: Scope, body: AssessmentIn):
    return await manage.create_assessment(session, caller, scope, body)


@router.get("/{assessment_id}", response_model=AssessmentOut)
async def get_assessment(session: Session, caller: Staff, scope: Scope, assessment_id: int):
    return await manage.get_assessment(session, caller, scope, assessment_id)


@router.put("/{assessment_id}", response_model=AssessmentOut)
async def update_assessment(session: Session, caller: Staff, scope: Scope, assessment_id: int, body: AssessmentIn):
    return await manage.update_assessment(session, caller, scope, assessment_id, body)


@router.delete("/{assessment_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_assessment(session: Session, caller: Staff, scope: Scope, assessment_id: int):
    await manage.delete_assessment(session, caller, scope, assessment_id)


@router.post("/{assessment_id}/publish", response_model=AssessmentOut)
async def publish_assessment(session: Session, caller: Staff, scope: Scope, assessment_id: int, body: PublishIn):
    """Opens the test to the given roster (snapshotted from OnlineCampus at this moment)."""
    return await manage.publish_assessment(session, caller, scope, assessment_id, body)


@router.post("/{assessment_id}/close", response_model=AssessmentOut)
async def close_assessment(session: Session, caller: Staff, scope: Scope, assessment_id: int):
    return await manage.close_assessment(session, caller, scope, assessment_id)


@router.get("/{assessment_id}/results", response_model=AssessmentResults)
async def assessment_results(session: Session, caller: Staff, scope: Scope, assessment_id: int):
    return await manage.assessment_results(session, caller, scope, assessment_id)
