"""Convention merges: pairs of conventions that might be the same event.

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from casttrail.db import UtcDateTime

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the convention_merges table."""
    op.create_table(
        "convention_merges",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("first_id", sa.Uuid(), nullable=False),
        sa.Column("second_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=9), nullable=False),
        sa.Column("reason", sa.String(length=200), nullable=False),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.Column("decided_at", UtcDateTime(), nullable=True),
        sa.CheckConstraint(
            "first_id <> second_id", name=op.f("ck_convention_merges_distinct_pair")
        ),
        sa.CheckConstraint(
            "status IN ('suggested', 'rejected', 'merged')",
            name=op.f("ck_convention_merges_status"),
        ),
        sa.ForeignKeyConstraint(
            ["first_id"],
            ["conventions.id"],
            name=op.f("fk_convention_merges_first_id_conventions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["second_id"],
            ["conventions.id"],
            name=op.f("fk_convention_merges_second_id_conventions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_convention_merges")),
        sa.UniqueConstraint(
            "first_id", "second_id", name=op.f("uq_convention_merges_first_id")
        ),
    )
    with op.batch_alter_table("convention_merges", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_convention_merges_second_id"), ["second_id"], unique=False
        )


def downgrade() -> None:
    """Drop the convention_merges table."""
    with op.batch_alter_table("convention_merges", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_convention_merges_second_id"))

    op.drop_table("convention_merges")
