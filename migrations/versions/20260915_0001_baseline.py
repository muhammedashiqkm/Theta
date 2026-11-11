"""baseline

The empty starting point. Running it proves the migration pipeline reaches the
database; the first real tables (question bank, tests, sessions, responses)
arrive in milestone 2 as the next revision.

Revision ID: 0001
Revises:
Create Date: 2026-09-15
"""
from typing import Sequence, Union

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
