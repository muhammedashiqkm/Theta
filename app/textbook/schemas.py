from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints

from app.bank.enums import QuestionType

# Pages read in one call: Gemini answers well inside OnlineCampus's wait at this size.
MAX_PAGES_PER_READ = 10
# Texts embedded in one call (the provider's batch limit is higher; this keeps calls short).
MAX_EMBED_TEXTS = 100
EMBED_TEXT_MAX = 8000
# Questions checked in one call, and the passages each may carry.
MAX_CHECK_ITEMS = 10
MAX_PASSAGES = 12
PASSAGE_MAX = 4000

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
ModelName = Annotated[str | None, StringConstraints(strip_whitespace=True, max_length=100)]


class SourcePassage(BaseModel):
    """A passage of the school's textbook: what OnlineCampus found closest to the request."""

    page: int = Field(ge=1, le=5000)
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=PASSAGE_MAX)]


# ----- upload -----------------------------------------------------------------------------

class UploadOut(BaseModel):
    """The PDF, held by Gemini for about two days: read it page by page before then."""

    file_name: str
    file_uri: str
    size_bytes: int
    expires_at: str | None


# ----- read ---------------------------------------------------------------------------------

class ReadIn(BaseModel):
    file_uri: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
    from_page: int = Field(ge=1, le=5000)
    to_page: int = Field(ge=1, le=5000)
    # The heading in force at the end of the pages read before (ReadOut.last_heading), so a
    # book read in parts gets the same passages as one read at once.
    heading: Annotated[str | None, StringConstraints(strip_whitespace=True, max_length=200)] = None


class PageOut(BaseModel):
    page: int
    # The number printed on the page, when there is one (teachers know the book by it).
    printed_page: str | None
    text: str


class ChunkOut(BaseModel):
    page: int
    # 1, 2, … within the page.
    order: int
    # The heading the passage sits under, when the page shows one.
    heading: str | None
    text: str
    # Unit length, so OnlineCampus compares two with a plain dot product.
    embedding: list[float]


class ReadOut(BaseModel):
    pages: list[PageOut]
    chunks: list[ChunkOut]
    # Pages in the whole PDF, as the reader saw it (0 = not known).
    document_pages: int
    # True when `to_page` reached the end of the book.
    at_end: bool
    # Send as `heading` with the next pages.
    last_heading: str | None
    model: str
    input_tokens: int
    output_tokens: int


# ----- embed --------------------------------------------------------------------------------

class EmbedIn(BaseModel):
    texts: list[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=EMBED_TEXT_MAX)]] = Field(
        min_length=1, max_length=MAX_EMBED_TEXTS)
    # 'query': something to search the book with (a topic, a question); 'document': a passage.
    kind: Literal["query", "document"] = "query"


class EmbedOut(BaseModel):
    vectors: list[list[float]]
    model: str
    dimensions: int


# ----- check --------------------------------------------------------------------------------

class CheckOption(BaseModel):
    text: Annotated[str, StringConstraints(max_length=2000)]
    is_correct: bool = False


class CheckItem(BaseModel):
    """One uploaded question, with the passages of the book closest to it."""

    key: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=50)]
    type: QuestionType
    stem: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=10000)]
    options: list[CheckOption] = Field(default_factory=list, max_length=10)
    accepted_answers: list[Annotated[str, StringConstraints(max_length=500)]] = Field(default_factory=list, max_length=10)
    passages: list[SourcePassage] = Field(default_factory=list, max_length=MAX_PASSAGES)


class CheckIn(BaseModel):
    class_name: Name
    subject_name: Name
    language: Literal["en", "ar"] = "en"
    model: ModelName = None
    items: list[CheckItem] = Field(min_length=1, max_length=MAX_CHECK_ITEMS)


Verdict = Literal["agrees", "disagrees", "no_key", "not_in_book"]


class CheckResult(BaseModel):
    key: str
    # agrees: the key matches the book · disagrees: the book says otherwise ·
    # no_key: the question has no answer marked, one is suggested · not_in_book: the
    # passages do not cover it.
    verdict: Verdict
    # For option questions: the options (0-based) the book supports.
    correct_options: list[int]
    # For short answers: what the book supports.
    accepted_answers: list[str]
    # The page the answer is on (0 = none).
    page: int
    # One plain sentence for the teacher.
    note: str


class CheckOut(BaseModel):
    items: list[CheckResult]
    model: str
    input_tokens: int
    output_tokens: int
