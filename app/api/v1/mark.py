"""AI marking for OnlineCampus's adaptive tests.

Stateless. OnlineCampus sends only what its own rules cannot decide — a short answer
that is not an accepted answer or a number to compare, and the written part's long
answers — and keeps every verdict, mark scheme and mark. Any caller role: a student's
answer is marked on the student's own request.
"""

from typing import Annotated

from fastapi import APIRouter, Depends

from app.core.security import Caller, get_caller
from app.marking import service
from app.marking.marker import Marker, get_marker
from app.marking.schemas import LongMarkIn, LongMarkOut, RubricIn, RubricOut, ShortMarkIn, ShortMarkOut

router = APIRouter(prefix="/v1/mark", tags=["mark"])


@router.post("/short-answer", response_model=ShortMarkOut)
async def mark_short_answer(
    body: ShortMarkIn,
    caller: Annotated[Caller, Depends(get_caller)],
    marker: Annotated[Marker, Depends(get_marker)],
) -> ShortMarkOut:
    return await service.mark_short(body, marker, caller)


@router.post("/rubric", response_model=RubricOut)
async def write_rubric(
    body: RubricIn,
    _: Annotated[Caller, Depends(get_caller)],
    marker: Annotated[Marker, Depends(get_marker)],
) -> RubricOut:
    return await service.write_rubric(body, marker)


@router.post("/long-answer", response_model=LongMarkOut)
async def mark_long_answer(
    body: LongMarkIn,
    _: Annotated[Caller, Depends(get_caller)],
    marker: Annotated[Marker, Depends(get_marker)],
) -> LongMarkOut:
    return await service.mark_long(body, marker)
