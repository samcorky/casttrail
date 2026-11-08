"""Watches: a user following a person or a production.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from casttrail.db import UtcDateTime

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the watches table."""
    op.create_table(
        "watches",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("person_id", sa.Uuid(), nullable=True),
        sa.Column("production_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.CheckConstraint(
            "(person_id IS NOT NULL) + (production_id IS NOT NULL) = 1",
            name=op.f("ck_watches_one_target"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_watches_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["person_id"],
            ["people.id"],
            name=op.f("fk_watches_person_id_people"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["production_id"],
            ["productions.id"],
            name=op.f("fk_watches_production_id_productions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_watches")),
        sa.UniqueConstraint(
            "user_id", "person_id", name=op.f("uq_watches_user_person")
        ),
        sa.UniqueConstraint(
            "user_id", "production_id", name=op.f("uq_watches_user_production")
        ),
    )
    op.create_index(op.f("ix_watches_person_id"), "watches", ["person_id"])
    op.create_index(op.f("ix_watches_production_id"), "watches", ["production_id"])


def downgrade() -> None:
    """Drop the watches table."""
    op.drop_index(op.f("ix_watches_production_id"), table_name="watches")
    op.drop_index(op.f("ix_watches_person_id"), table_name="watches")
    op.drop_table("watches")
