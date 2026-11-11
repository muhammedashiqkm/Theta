from enum import StrEnum


class TopicLevel(StrEnum):
    CHAPTER = "chapter"
    UNIT = "unit"


class QuestionType(StrEnum):
    MCQ_SINGLE = "mcq_single"
    MCQ_MULTI = "mcq_multi"
    TRUE_FALSE = "true_false"
    NUMERIC = "numeric"
    SHORT_ANSWER = "short_answer"
    LONG_ANSWER = "long_answer"


class Difficulty(StrEnum):
    EASY = "easy"
    MEDIUM = "medium"
    HARD = "hard"


class BloomLevel(StrEnum):
    REMEMBER = "remember"
    UNDERSTAND = "understand"
    APPLY = "apply"
    ANALYZE = "analyze"
    EVALUATE = "evaluate"
    CREATE = "create"


class QuestionStatus(StrEnum):
    DRAFT = "draft"      # being written; never shown to a student
    PILOT = "pilot"      # approved; collects answers, not yet scored
    ACTIVE = "active"    # calibrated from real answers; scored and selectable
    RETIRED = "retired"  # kept for history; never shown again


class QuestionSource(StrEnum):
    MANUAL = "manual"
    IMPORT = "import"
    AI = "ai"


OPTION_TYPES = frozenset({QuestionType.MCQ_SINGLE, QuestionType.MCQ_MULTI, QuestionType.TRUE_FALSE})
