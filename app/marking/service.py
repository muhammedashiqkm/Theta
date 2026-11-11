"""Marking with Claude: a short answer's verdict, a long answer's mark scheme and marks.

OnlineCampus asks only when its own rules cannot decide (an exact or numeric match is
never sent here), keeps every verdict and mark, and re-asks later when Theta or Claude
could not be reached.
"""

import asyncio
import logging

from app.core.security import Caller
from app.marking import prompt
from app.marking.marker import Marker, MarkRefused
from app.marking.schemas import (
    AwardedPoint,
    LongMarkIn,
    LongMarkOut,
    RubricIn,
    RubricOut,
    RubricPoint,
    ShortMarkIn,
    ShortMarkOut,
)

log = logging.getLogger(__name__)

REASON_MAX = 500
FEEDBACK_MAX = 1500
EVIDENCE_MAX = 500
# Two markings further apart than this (in marks) need a third.
AGREE_WITHIN = 1.0

# Seconds. The quick check runs while the student waits for the next question.
SHORT_QUICK_TIMEOUT = 20.0
SHORT_CAREFUL_TIMEOUT = 45.0
RUBRIC_TIMEOUT = 90.0
LONG_TIMEOUT = 80.0  # two in parallel, then maybe a third: under OnlineCampus's 200 s client limit

REFUSED_REASON = {
    "en": "The answer could not be checked automatically.",
    "ar": "تعذّر التحقق من الإجابة تلقائياً.",
}
REFUSED_FEEDBACK = {
    "en": "This answer could not be marked automatically.",
    "ar": "تعذّر تصحيح هذه الإجابة تلقائياً.",
}


async def mark_short(request: ShortMarkIn, marker: Marker, caller: Caller) -> ShortMarkOut:
    """A quick check; when it is unsure, a careful second check decides."""
    try:
        first = await marker.ask(prompt.SHORT_SYSTEM, prompt.short(request, careful=False),
                                 prompt.SHORT_FIRST_SCHEMA, "low", SHORT_QUICK_TIMEOUT)
        verdict, reason, model, twice = first.data.get("verdict"), first.data.get("reason"), first.model, False
        if verdict not in ("correct", "incorrect"):
            second = await marker.ask(prompt.SHORT_SYSTEM, prompt.short(request, careful=True),
                                      prompt.SHORT_FINAL_SCHEMA, "high", SHORT_CAREFUL_TIMEOUT)
            verdict, reason, model, twice = second.data.get("verdict"), second.data.get("reason"), second.model, True
    except MarkRefused:
        return ShortMarkOut(correct=False, reason=REFUSED_REASON[request.language], checked_twice=False, model="")

    log.info("short answer marked for institution %s: %s%s", caller.institution_id, verdict, " (checked twice)" if twice else "")
    return ShortMarkOut(correct=verdict == "correct", reason=_clip(reason, REASON_MAX), checked_twice=twice, model=model)


async def write_rubric(request: RubricIn, marker: Marker) -> RubricOut:
    reply = await marker.ask(prompt.RUBRIC_SYSTEM, prompt.rubric(request), prompt.RUBRIC_SCHEMA, "medium", RUBRIC_TIMEOUT)
    raw = reply.data.get("points")
    points = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        text = _clip(item.get("text"), 500)
        marks = item.get("marks")
        if text and isinstance(marks, int | float) and not isinstance(marks, bool) and marks > 0:
            points.append((text, float(marks)))
    return RubricOut(points=_fit(points[:6], request.marks, request.language), total=request.marks, model=reply.model)


async def mark_long(request: LongMarkIn, marker: Marker) -> LongMarkOut:
    """Two independent markings; a third decides when they differ by more than a mark."""
    maximum = round(sum(p.marks for p in request.points), 2)
    try:
        first, second = await asyncio.gather(_one_marking(request, marker), _one_marking(request, marker))
        markings = [first, second]
        if abs(first[0] - second[0]) <= AGREE_WITHIN:
            points = [
                AwardedPoint(index=i, awarded=_half(min(p.marks, (a.awarded + b.awarded) / 2)),
                             evidence=a.evidence or b.evidence)
                for i, (p, a, b) in enumerate(zip(request.points, first[1], second[1]))
            ]
            total = round(sum(p.awarded for p in points), 2)
            closer = first if abs(first[0] - total) <= abs(second[0] - total) else second
            feedback, model = closer[2], closer[3]
        else:
            third = await _one_marking(request, marker)
            markings.append(third)
            median = sorted(markings, key=lambda m: m[0])[1]
            total, points, feedback, model = median[0], median[1], median[2], median[3]
    except MarkRefused:
        return LongMarkOut(points=[AwardedPoint(index=i, awarded=0, evidence="") for i in range(len(request.points))],
                           total=0, max=maximum, feedback=REFUSED_FEEDBACK[request.language], markings=0, model="")

    return LongMarkOut(points=points, total=total, max=maximum, feedback=feedback, markings=len(markings), model=model)


async def _one_marking(request: LongMarkIn, marker: Marker) -> tuple[float, list[AwardedPoint], str, str]:
    reply = await marker.ask(prompt.LONG_SYSTEM, prompt.long(request), prompt.LONG_SCHEMA, "high", LONG_TIMEOUT)
    by_index: dict[int, dict] = {}
    raw = reply.data.get("points")
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, dict) and isinstance(item.get("index"), int) and item["index"] not in by_index:
            by_index[item["index"]] = item

    points = []
    for i, scheme in enumerate(request.points):
        item = by_index.get(i, {})
        value = item.get("awarded")
        awarded = float(value) if isinstance(value, int | float) and not isinstance(value, bool) else 0.0
        awarded = _half(max(0.0, min(scheme.marks, awarded)))
        evidence = _clip(item.get("evidence"), EVIDENCE_MAX)
        # Evidence must be the student's own words; anything else is dropped.
        if evidence and _plain(evidence) not in _plain(request.answer):
            evidence = ""
        points.append(AwardedPoint(index=i, awarded=awarded, evidence=evidence))
    total = round(sum(p.awarded for p in points), 2)
    return total, points, _clip(reply.data.get("feedback"), FEEDBACK_MAX), reply.model


def _fit(points: list[tuple[str, float]], total: float, language: str) -> list[RubricPoint]:
    """Points whose marks are multiples of 0.5 and add up to exactly `total`."""
    fallback = [RubricPoint(text="A complete and correct answer." if language == "en" else "إجابة كاملة وصحيحة.", marks=total)]
    if not points:
        return fallback
    given = sum(m for _, m in points)
    marks = [_half(m * total / given) for _, m in points]
    # Put the rounding difference on the biggest point.
    biggest = max(range(len(marks)), key=lambda i: marks[i])
    marks[biggest] = round(marks[biggest] + total - sum(marks), 2)
    if any(m <= 0 for m in marks):
        return fallback
    return [RubricPoint(text=text, marks=m) for (text, _), m in zip(points, marks)]


def _half(value: float) -> float:
    """To the nearest half mark, halves up."""
    return int(value * 2 + 0.5) / 2


def _clip(value: object, limit: int) -> str:
    text = " ".join(value.split()) if isinstance(value, str) else ""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _plain(text: str) -> str:
    return " ".join(text.casefold().split())
