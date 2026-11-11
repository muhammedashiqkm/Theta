"""Append-only record of who changed what. Written in the same transaction as the change."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.bank.models import AuditEntry
from app.core.security import Caller


def record(session: AsyncSession, caller: Caller, action: str, entity: str, entity_id: int, **detail) -> None:
    session.add(
        AuditEntry(
            institution_id=caller.institution_id,
            actor_user_id=caller.user_id,
            actor_role=caller.role,
            action=action,
            entity=entity,
            entity_id=entity_id,
            detail=detail,
        )
    )
