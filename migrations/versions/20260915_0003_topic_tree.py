"""topic tree

Topics become a two-level tree (chapter → unit) that can be imported from the
campus syllabus. Existing topics become chapters. Topic names are now unique
among siblings instead of across the whole class and subject.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-15
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("topics", sa.Column("parent_id", sa.BigInteger(), nullable=True))
    op.add_column("topics", sa.Column("level", sa.String(length=10), server_default="chapter", nullable=False))
    op.add_column("topics", sa.Column("name_nat", sa.String(length=200), nullable=True))
    op.add_column("topics", sa.Column("source_syllabus_detail_id", sa.Integer(), nullable=True))
    op.create_foreign_key(op.f("fk_topics_parent_id_topics"), "topics", "topics", ["parent_id"], ["id"])
    op.create_index("ix_topics_parent_id", "topics", ["parent_id"], unique=False)

    # Not detected by autogenerate (Alembic does not compare CHECK constraints).
    op.create_check_constraint(op.f("ck_topics_level"), "topics", "level IN ('chapter', 'unit')")
    op.create_check_constraint(op.f("ck_topics_level_parent"), "topics", "(level = 'chapter') = (parent_id IS NULL)")

    op.drop_index("uq_topics_active_name", table_name="topics", postgresql_where=sa.text("is_active"))
    op.create_index(
        "uq_topics_active_name",
        "topics",
        ["institution_id", "class_id", "subject_id", sa.literal_column("coalesce(parent_id, 0)"), sa.literal_column("lower(name)")],
        unique=True,
        postgresql_where=sa.text("is_active"),
    )


def downgrade() -> None:
    op.drop_index("uq_topics_active_name", table_name="topics", postgresql_where=sa.text("is_active"))
    op.create_index(
        "uq_topics_active_name",
        "topics",
        ["institution_id", "class_id", "subject_id", sa.literal_column("lower(name)")],
        unique=True,
        postgresql_where=sa.text("is_active"),
    )
    op.drop_constraint(op.f("ck_topics_level_parent"), "topics", type_="check")
    op.drop_constraint(op.f("ck_topics_level"), "topics", type_="check")
    op.drop_index("ix_topics_parent_id", table_name="topics")
    op.drop_constraint(op.f("fk_topics_parent_id_topics"), "topics", type_="foreignkey")
    op.drop_column("topics", "source_syllabus_detail_id")
    op.drop_column("topics", "name_nat")
    op.drop_column("topics", "level")
    op.drop_column("topics", "parent_id")
