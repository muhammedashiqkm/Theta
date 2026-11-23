"""AI marking for OnlineCampus's adaptive tests.

Stateless. OnlineCampus sends only what its own rules cannot decide — a short answer
that is not an accepted answer or a number to compare — and keeps every verdict. Any caller role: a student's
answer is marked on the student's own request.
"""

from typing import Annotated

from fastapi import APIRouter, Depends

from app.core.security import Caller, get_caller
from app.marking import service
from app.marking.marker import Marker, get_marker
from app.marking.schemas import (
    ShortMarkIn,
    ShortMarkOut,
    StudentCommentIn,
    StudentCommentOut,
    TestSummaryIn,
    TestSummaryOut,
)

router = APIRouter(prefix="/v1/mark", tags=["mark"])


@router.post("/short-answer", response_model=ShortMarkOut)
async def mark_short_answer(
    body: ShortMarkIn,
    caller: Annotated[Caller, Depends(get_caller)],
    marker: Annotated[Marker, Depends(get_marker)],
) -> ShortMarkOut:
    return await service.mark_short(body, marker, caller)


@router.post("/test-summary", response_model=TestSummaryOut)
async def write_test_summary(
    body: TestSummaryIn,
    _: Annotated[Caller, Depends(get_caller)],
    marker: Annotated[Marker, Depends(get_marker)],
) -> TestSummaryOut:
    """A class's test results in a few plain sentences, from the figures only (no names)."""
    return await service.write_test_summary(body, marker)

@router.post("/student-comment", response_model=StudentCommentOut)
async def write_student_comment(
    body: StudentCommentIn,
    _: Annotated[Caller, Depends(get_caller)],
    marker: Annotated[Marker, Depends(get_marker)],
) -> StudentCommentOut:
    """A teacher's comment for one student's printed report, from the figures only (no name)."""
    return await service.write_student_comment(body, marker)
