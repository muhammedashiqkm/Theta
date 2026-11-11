"""Choosing the next question, and deciding when a test is over."""

import random
from dataclasses import dataclass

from app.engine.irt import ItemParams, information

# The next question is drawn at random from up to this many of the most informative
# candidates, so neighbours rarely see the same sequence and no question gets
# worn out by being "the best" for everyone...
EXPOSURE_TOP_K = 5
# ...but only among those nearly as informative as the best one. Without this floor a
# small chapter (say six questions) makes almost every question "top five", and an
# easy question can follow three right answers: variety would cost adaptivity.
NEAR_BEST_SHARE = 0.8


@dataclass(frozen=True)
class Candidate:
    question_id: int
    chapter_id: int
    params: ItemParams


def choose_next(
    theta: float,
    pool: list[Candidate],
    used: set[int],
    chapter_shares: dict[int, float],
    served_per_chapter: dict[int, int],
    rng: random.Random,
    top_k: int = EXPOSURE_TOP_K,
) -> Candidate | None:
    """The next question, or None when the pool is exhausted.

    1. Topic balance: take the chapter furthest behind its share of the test so far.
    2. Within it, rank unused questions by information at the current θ.
    3. Pick at random among the top few that are nearly as informative as the best
       (exposure control that never trades away adaptivity).
    """
    available = [c for c in pool if c.question_id not in used]
    if not available:
        return None

    served_total = sum(served_per_chapter.values())
    chapters_with_questions = {c.chapter_id for c in available}
    eligible = {ch: share for ch, share in chapter_shares.items() if ch in chapters_with_questions and share > 0}

    if eligible:
        def deficit(chapter_id: int) -> float:
            served_share = served_per_chapter.get(chapter_id, 0) / served_total if served_total else 0.0
            return eligible[chapter_id] - served_share

        # Ties (for example at the very start) are broken at random, not by chapter order.
        best = max(deficit(ch) for ch in eligible)
        chapter = rng.choice(sorted(ch for ch in eligible if abs(deficit(ch) - best) < 1e-9))
        candidates = [c for c in available if c.chapter_id == chapter]
    else:
        candidates = available

    ranked = sorted(((information(theta, c.params), c) for c in candidates), key=lambda pair: pair[0], reverse=True)
    best = ranked[0][0]
    shortlist = [c for info, c in ranked[: max(top_k, 1)] if info >= best * NEAR_BEST_SHARE]
    return rng.choice(shortlist)


def stop_reason(
    answered: int,
    se: float,
    min_questions: int,
    max_questions: int,
    target_se: float,
    seconds_left: float,
    pool_left: int,
) -> str | None:
    """Why the test should end now, or None to keep going."""
    if seconds_left <= 0:
        return "time_up"
    if answered >= max_questions:
        return "max_questions"
    if pool_left <= 0:
        return "pool_exhausted"
    if answered >= min_questions and se <= target_se:
        return "precision"
    return None
