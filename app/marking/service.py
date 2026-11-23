"""Marking with Claude: a short answer's verdict; plus the AI-written report texts.

OnlineCampus asks only when its own rules cannot decide (an exact or numeric match is
never sent here), keeps every verdict, and re-asks later when Theta or Claude
could not be reached.
"""

import logging
import re

from app.core.security import Caller
from app.marking import prompt
from app.marking.marker import Marker, MarkRefused
from app.marking.schemas import (
    ShortMarkIn,
    ShortMarkOut,
    StudentCommentIn,
    StudentCommentOut,
    TestSummaryIn,
    TestSummaryOut,
)

log = logging.getLogger(__name__)

REASON_MAX = 500

# Seconds. The quick check runs while the student waits for the next question.
SHORT_QUICK_TIMEOUT = 20.0
# Arabic text is two to three times the tokens of the same English, so it gets longer.
SHORT_CAREFUL_TIMEOUT = {"en": 45.0, "ar": 60.0}

REFUSED_REASON = {
    "en": "The answer could not be checked automatically.",
    "ar": "تعذّر التحقق من الإجابة تلقائياً.",
}
async def mark_short(request: ShortMarkIn, marker: Marker, caller: Caller) -> ShortMarkOut:
    """A quick check on the fast model; when it is unsure (or declines), a careful second
    check on the marking model decides."""
    try:
        try:
            first = await marker.ask(prompt.SHORT_SYSTEM, prompt.short(request, careful=False),
                                     prompt.SHORT_FIRST_SCHEMA, "low", SHORT_QUICK_TIMEOUT, quick=True)
            verdict, reason, model, twice = first.data.get("verdict"), first.data.get("reason"), first.model, False
        except MarkRefused:
            verdict, reason, model, twice = None, None, "", False
        if verdict not in ("correct", "incorrect"):
            second = await marker.ask(prompt.SHORT_SYSTEM, prompt.short(request, careful=True),
                                      prompt.SHORT_FINAL_SCHEMA, "high", SHORT_CAREFUL_TIMEOUT[request.language])
            verdict, reason, model, twice = second.data.get("verdict"), second.data.get("reason"), second.model, True
    except MarkRefused:
        return ShortMarkOut(correct=False, reason=REFUSED_REASON[request.language], checked_twice=False, model="")

    log.info("short answer marked for institution %s: %s%s", caller.institution_id, verdict, " (checked twice)" if twice else "")
    return ShortMarkOut(correct=verdict == "correct", reason=_clip(reason, REASON_MAX), checked_twice=twice, model=model)


def _clip(value: object, limit: int) -> str:
    text = " ".join(value.split()) if isinstance(value, str) else ""
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ----- a test's results in plain words --------------------------------------------------------

SUMMARY_TIMEOUT = 60.0
SUMMARY_MAX = 1500


async def write_test_summary(request: TestSummaryIn, marker: Marker) -> TestSummaryOut:
    """3-5 sentences on how the class did and what to do next, from the figures only."""
    try:
        reply = await marker.ask(prompt.TEST_SUMMARY_SYSTEM, prompt.test_summary(request),
                                 prompt.TEST_SUMMARY_SCHEMA, "low", SUMMARY_TIMEOUT)
    except MarkRefused:
        return TestSummaryOut(summary="", model="")
    data = reply.data
    headline = _prose(_clip(data.get("headline"), 300))
    good = _points(data.get("went_well"))
    watch = _points(data.get("needs_attention"))
    tip = _prose(_clip(data.get("tip"), 300))
    summary = " ".join(x for x in [headline, *good, *watch, tip] if x)
    return TestSummaryOut(headline=headline, went_well=good, needs_attention=watch, tip=tip,
                          summary=_clip(summary, SUMMARY_MAX), model=reply.model)


def _points(raw: object) -> list[str]:
    """At most two short points, plain text, empty ones dropped."""
    items = raw if isinstance(raw, list) else []
    points = [_prose(_clip(p, 200)) for p in items if isinstance(p, str)]
    return [p for p in points if p][:2]


def _prose(text: str) -> str:
    """One paragraph of plain text: no markdown marks, single spaces (case kept)."""
    return " ".join(re.sub(r"[*_#`]+", "", text).split())

# ----- one student's progress in a teacher's words ---------------------------------------------

COMMENT_TIMEOUT = 60.0
COMMENT_MAX = 1200


async def write_student_comment(request: StudentCommentIn, marker: Marker) -> StudentCommentOut:
    """3-4 sentences for the printed report; the student is [NAME] (no name is sent)."""
    try:
        reply = await marker.ask(prompt.STUDENT_COMMENT_SYSTEM, prompt.student_comment(request),
                                 prompt.STUDENT_COMMENT_SCHEMA, "low", COMMENT_TIMEOUT)
    except MarkRefused:
        return StudentCommentOut(comment="", model="")
    return StudentCommentOut(comment=_prose(_clip(reply.data.get("comment"), COMMENT_MAX)), model=reply.model)
