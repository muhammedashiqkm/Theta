import pytest

from app.bank import rules
from app.bank.enums import QuestionStatus as S
from app.bank.enums import QuestionType as T
from app.core.errors import Conflict
from app.core.security import TeachingScope


@pytest.mark.parametrize(
    ("question_type", "count", "expected"),
    [(T.MCQ_SINGLE, 4, 0.25), (T.MCQ_SINGLE, 3, 0.333), (T.TRUE_FALSE, 2, 0.5), (T.MCQ_MULTI, 4, 0.0), (T.NUMERIC, 0, 0.0)],
)
def test_guessing_floor(question_type, count, expected):
    assert rules.guessing_floor(question_type, count) == expected


@pytest.mark.parametrize(
    ("question_type", "options", "message"),
    [
        (T.MCQ_SINGLE, [("A", True)], "2 to 6 options"),
        (T.MCQ_SINGLE, [("A", True), ("B", True)], "exactly one"),
        (T.MCQ_SINGLE, [("A", False), ("B", False)], "exactly one"),
        (T.MCQ_SINGLE, [("Paris", True), (" paris ", False)], "same text"),
        (T.MCQ_SINGLE, [("A", True), ("  ", False)], "needs text"),
        (T.MCQ_MULTI, [("A", True), ("B", True)], "at least one incorrect"),
        (T.TRUE_FALSE, [("True", True), ("False", False), ("Maybe", False)], "exactly 2"),
        (T.NUMERIC, [("5", True)], "has no options"),
    ],
)
def test_check_options_rejects(question_type, options, message):
    with pytest.raises(ValueError, match=message):
        rules.check_options(question_type, options)


def test_check_options_accepts_valid_sets():
    rules.check_options(T.MCQ_SINGLE, [("A", False), ("B", True), ("C", False)])
    rules.check_options(T.MCQ_MULTI, [("A", True), ("B", True), ("C", False)])
    rules.check_options(T.TRUE_FALSE, [("True", False), ("False", True)])
    rules.check_options(T.LONG_ANSWER, [])


def test_answer_key_required_where_it_matters():
    with pytest.raises(ValueError, match="correct value"):
        rules.check_answer_key(T.NUMERIC, {"tolerance": 0.1})
    with pytest.raises(ValueError, match="accepted answer"):
        rules.check_answer_key(T.SHORT_ANSWER, {"accepted": []})
    with pytest.raises(ValueError, match="keyed by its options"):
        rules.check_answer_key(T.MCQ_SINGLE, {"value": 1})
    rules.check_answer_key(T.LONG_ANSWER, None)


@pytest.mark.parametrize(
    ("current", "target", "n_responses"),
    [(S.DRAFT, S.PILOT, 0), (S.PILOT, S.RETIRED, 9), (S.PILOT, S.DRAFT, 0), (S.ACTIVE, S.RETIRED, 400)],
)
def test_allowed_transitions(current, target, n_responses):
    rules.check_transition(current, target, n_responses)


@pytest.mark.parametrize(
    ("current", "target", "n_responses", "message"),
    [
        (S.DRAFT, S.RETIRED, 0, "cannot become"),
        (S.DRAFT, S.ACTIVE, 0, "cannot become"),
        (S.RETIRED, S.PILOT, 0, "cannot become"),
        (S.PILOT, S.DRAFT, 3, "retire it instead"),
        (S.PILOT, S.PILOT, 0, "already"),
    ],
)
def test_refused_transitions(current, target, n_responses, message):
    with pytest.raises(Conflict, match=message):
        rules.check_transition(current, target, n_responses)


def test_edit_mode_protects_answered_questions():
    assert rules.edit_mode(S.DRAFT, 0) == "in_place"
    assert rules.edit_mode(S.PILOT, 0) == "in_place"
    assert rules.edit_mode(S.PILOT, 1) == "new_version"
    assert rules.edit_mode(S.ACTIVE, 0) == "new_version"
    with pytest.raises(Conflict):
        rules.edit_mode(S.RETIRED, 0)


def test_teaching_scope_parsing():
    assert TeachingScope.parse("all").unrestricted
    scope = TeachingScope.parse(" 12:5, 12:6 ,")
    assert not scope.unrestricted
    assert scope.allows(12, 5) and scope.allows(12, 6) and not scope.allows(13, 5)
    nothing = TeachingScope.parse("")
    assert not nothing.unrestricted and not nothing.allows(12, 5)
    with pytest.raises(ValueError):
        TeachingScope.parse("12-5")
