"""Who may call Theta, on whose behalf, and over which classes and subjects.

Theta trusts exactly one caller: the OnlineCampus API. That API has already
authenticated the user, decided their role and resolved their Teaching Scope,
and forwards the result in X-Theta-* headers. Nothing here ever sees a browser
token.
"""

import hmac
from dataclasses import dataclass
from typing import Annotated, Literal

from fastapi import Depends, Header, HTTPException, status

from app.core.config import Settings, get_settings

Role = Literal["admin", "teacher", "student", "parent"]


@dataclass(frozen=True)
class Caller:
    institution_id: int
    user_id: int
    role: Role

    @property
    def is_staff(self) -> bool:
        return self.role in ("admin", "teacher")


@dataclass(frozen=True)
class TeachingScope:
    """The (class, subject) pairs the caller may work with.

    Sent as X-Theta-Scope: "all" for an unrestricted login (admin, office staff),
    otherwise "classId:subjectId" pairs separated by commas. An empty value is a
    teacher who teaches nothing, which is legitimate and allows nothing. The
    header is required on every scoped endpoint, so a caller that forgets it is
    refused rather than silently treated as unrestricted.
    """

    unrestricted: bool
    pairs: frozenset[tuple[int, int]]

    def allows(self, class_id: int, subject_id: int) -> bool:
        return self.unrestricted or (class_id, subject_id) in self.pairs

    @classmethod
    def parse(cls, raw: str) -> "TeachingScope":
        value = raw.strip()
        if value.lower() == "all":
            return cls(True, frozenset())
        pairs: set[tuple[int, int]] = set()
        for part in filter(None, (p.strip() for p in value.split(","))):
            class_part, sep, subject_part = part.partition(":")
            if not sep or not class_part.isdigit() or not subject_part.isdigit():
                raise ValueError(f"Invalid scope entry {part!r}; expected classId:subjectId.")
            pairs.add((int(class_part), int(subject_part)))
        return cls(False, frozenset(pairs))


def require_service_token(
    settings: Annotated[Settings, Depends(get_settings)],
    x_theta_token: Annotated[str | None, Header()] = None,
) -> None:
    expected = settings.service_token
    if not expected or not x_theta_token or not hmac.compare_digest(x_theta_token, expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing or invalid service token.")


def get_caller(
    _: Annotated[None, Depends(require_service_token)],
    x_theta_institution_id: Annotated[int, Header(gt=0)],
    x_theta_user_id: Annotated[int, Header(gt=0)],
    x_theta_role: Annotated[Role, Header()],
) -> Caller:
    return Caller(x_theta_institution_id, x_theta_user_id, x_theta_role)


def require_staff(caller: Annotated[Caller, Depends(get_caller)]) -> Caller:
    if not caller.is_staff:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only staff can manage the question bank.")
    return caller


@dataclass(frozen=True)
class Student:
    institution_id: int
    user_id: int
    # OnlineCampus StudentAcademicYearID, taken by OnlineCampus from the student's own
    # login token (never from a header the browser could change).
    student_id: int


def require_student(
    caller: Annotated[Caller, Depends(get_caller)],
    x_theta_student_id: Annotated[int | None, Header(gt=0)] = None,
) -> Student:
    if caller.role != "student":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only a student's own login can take a test.")
    if x_theta_student_id is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "X-Theta-Student-Id header is required.")
    return Student(caller.institution_id, caller.user_id, x_theta_student_id)


def get_scope(x_theta_scope: Annotated[str | None, Header()] = None) -> TeachingScope:
    if x_theta_scope is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "X-Theta-Scope header is required.")
    try:
        return TeachingScope.parse(x_theta_scope)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
