import random

import pytest

from app.engine import irt, marking
from app.engine.irt import ItemParams
from app.engine.marking import InvalidAnswer, OptionKey
from app.engine.selection import Candidate, choose_next, stop_reason

# ----- IRT maths --------------------------------------------------------------


def test_probability_matches_the_worked_example():
    item = ItemParams(a=1.2, b=0.5, c=0.2)
    assert irt.p_correct(1.0, item) == pytest.approx(0.717, abs=0.001)
    assert irt.p_correct(-1.0, item) == pytest.approx(0.313, abs=0.001)


def test_probability_stays_within_the_guessing_floor_and_one():
    item = ItemParams(a=2.0, b=0.0, c=0.25)
    assert irt.p_correct(-400, item) == pytest.approx(0.25)
    assert irt.p_correct(400, item) == pytest.approx(1.0)


def test_information_peaks_at_the_difficulty():
    item = ItemParams(a=1.0, b=0.7, c=0.0)
    at_b = irt.information(0.7, item)
    assert at_b == pytest.approx(0.25)
    assert at_b > irt.information(-0.5, item) and at_b > irt.information(2.0, item)


def test_estimate_starts_neutral_and_moves_with_answers():
    assert irt.estimate([]) == irt.NEUTRAL
    items = [ItemParams(1.0, b, 0.25) for b in (-1.0, 0.0, 1.0, 0.0, 1.0)]
    strong = irt.estimate([(item, True) for item in items])
    weak = irt.estimate([(item, False) for item in items])
    assert 0.5 < strong.theta < 4 and -4 < weak.theta < -0.5
    assert strong.se < 1.0 and weak.se < 1.0


def test_scaled_score_and_levels():
    assert irt.scaled_score(0.77) == 577
    assert irt.scaled_score(9) == 800 and irt.scaled_score(-9) == 200
    assert [irt.level_for(t) for t in (-1.5, -0.2, 0.4, 1.3)] == ["beginning", "developing", "proficient", "advanced"]


# ----- selection --------------------------------------------------------------


def pool_of(*specs: tuple[int, int, float]) -> list[Candidate]:
    return [Candidate(question_id=qid, chapter_id=ch, params=ItemParams(1.0, b, 0.25)) for qid, ch, b in specs]


def test_picks_the_most_informative_unused_question():
    pool = pool_of((1, 10, -2.0), (2, 10, 0.0), (3, 10, 1.0))
    rng = random.Random(1)
    assert choose_next(1.0, pool, set(), {10: 1.0}, {}, rng, top_k=1).question_id == 3
    assert choose_next(1.0, pool, {3}, {10: 1.0}, {}, rng, top_k=1).question_id == 2


def test_randomness_never_picks_a_clearly_worse_question():
    """A small chapter: every question is 'top five', but only the near-best may be drawn."""
    pool = pool_of((1, 10, -1.0), (2, 10, -1.0), (3, 10, 0.0), (4, 10, 1.0), (5, 10, 1.0), (6, 10, 0.0))
    rng = random.Random(7)
    # A strong estimate draws hard (or near-equally informative medium) questions, never easy ones...
    picks = {choose_next(1.2, pool, set(), {10: 1.0}, {}, rng).question_id for _ in range(200)}
    assert {4, 5} <= picks and not picks & {1, 2}
    # ...and a weak estimate never draws hard ones.
    picks = {choose_next(-1.2, pool, set(), {10: 1.0}, {}, rng).question_id for _ in range(200)}
    assert {1, 2} <= picks and not picks & {4, 5}


def test_balances_chapters_by_share():
    pool = pool_of((1, 10, 0.0), (2, 10, 0.1), (3, 20, 0.0), (4, 20, 0.1))
    pick = choose_next(0.0, pool, {1, 2}, {10: 0.5, 20: 0.5}, {10: 2}, random.Random(3), top_k=1)
    assert pick.chapter_id == 20


def test_skips_a_chapter_with_nothing_left_and_ends_when_empty():
    pool = pool_of((1, 10, 0.0), (2, 20, 0.0))
    pick = choose_next(0.0, pool, {1}, {10: 0.9, 20: 0.1}, {10: 1}, random.Random(5))
    assert pick.question_id == 2
    assert choose_next(0.0, pool, {1, 2}, {10: 0.5, 20: 0.5}, {}, random.Random(5)) is None


def test_stop_rules_in_order_of_precedence():
    args = dict(min_questions=5, max_questions=20, target_se=0.4, pool_left=10)
    assert stop_reason(3, 0.9, seconds_left=0, **args) == "time_up"
    assert stop_reason(20, 0.9, seconds_left=60, **args) == "max_questions"
    assert stop_reason(4, 0.3, seconds_left=60, **args) is None  # precise, but under the minimum
    assert stop_reason(6, 0.39, seconds_left=60, **args) == "precision"
    assert stop_reason(6, 0.9, seconds_left=60, **{**args, "pool_left": 0}) == "pool_exhausted"


def test_adaptive_tests_separate_strong_and_weak_students():
    """A simulated class on a pool built only from teacher tags (the pre-calibration state)."""
    rng = random.Random(20260915)
    pool = [
        Candidate(question_id=i, chapter_id=10 + i % 3, params=ItemParams(1.0, (-1.0, 0.0, 1.0)[i % 3], 0.25))
        for i in range(90)
    ]
    shares = {10: 1 / 3, 11: 1 / 3, 12: 1 / 3}

    def sit(true_theta: float) -> float:
        used: set[int] = set()
        served: dict[int, int] = {}
        responses: list[tuple[ItemParams, bool]] = []
        est = irt.NEUTRAL
        while stop_reason(len(responses), est.se, 10, 20, 0.45, 60, len(pool) - len(used)) is None:
            item = choose_next(est.theta, pool, used, shares, served, rng)
            used.add(item.question_id)
            served[item.chapter_id] = served.get(item.chapter_id, 0) + 1
            responses.append((item.params, rng.random() < irt.p_correct(true_theta, item.params)))
            est = irt.estimate(responses)
        return est.theta

    strong = [sit(1.5) for _ in range(40)]
    weak = [sit(-1.5) for _ in range(40)]
    assert sum(strong) / len(strong) - sum(weak) / len(weak) > 1.5
    assert sum(s > w for s, w in zip(sorted(strong), sorted(weak))) >= 36


# ----- marking ----------------------------------------------------------------

OPTIONS = [OptionKey(1, False), OptionKey(2, True), OptionKey(3, False), OptionKey(4, True)]


def test_marks_choice_questions():
    assert marking.is_correct("mcq_single", [OptionKey(1, False), OptionKey(2, True)], None, {"positions": [2]})
    assert not marking.is_correct("true_false", [OptionKey(1, True), OptionKey(2, False)], None, {"positions": [2]})
    assert marking.is_correct("mcq_multi", OPTIONS, None, {"positions": [4, 2]})
    assert not marking.is_correct("mcq_multi", OPTIONS, None, {"positions": [2]})  # all-or-nothing


def test_marks_numeric_and_short_answers():
    assert marking.is_correct("numeric", [], {"value": 9.8, "tolerance": 0.1}, {"value": "9.75"})
    assert not marking.is_correct("numeric", [], {"value": 9.8, "tolerance": 0.1}, {"value": 9.95})
    key = {"accepted": ["Photosynthesis", "photo synthesis"], "case_sensitive": False}
    assert marking.is_correct("short_answer", [], key, {"text": "  PHOTOSYNTHESIS "})
    assert marking.is_correct("short_answer", [], key, {"text": "photo   synthesis"})
    assert not marking.is_correct("short_answer", [], {**key, "case_sensitive": True}, {"text": "photosynthesis"})


@pytest.mark.parametrize(
    ("question_type", "answer"),
    [
        ("mcq_single", {"positions": [1, 2]}),
        ("mcq_single", {"positions": [9]}),
        ("mcq_multi", {"positions": []}),
        ("mcq_single", {"text": "B"}),
        ("numeric", {"value": "about ten"}),
        ("short_answer", {"text": "   "}),
        ("long_answer", {"text": "An essay"}),
    ],
)
def test_rejects_answers_that_do_not_fit(question_type, answer):
    with pytest.raises(InvalidAnswer):
        marking.is_correct(question_type, OPTIONS, {"value": 1, "accepted": ["x"]}, answer)
