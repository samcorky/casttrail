"""Refresh progress: when a refresh last finished with each watched person.

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from casttrail.db import UtcDateTime

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the refresh_progress table."""
    op.create_table(
        "refresh_progress",
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("person_id", sa.Uuid(), nullable=False),
        sa.Column("refreshed_at", UtcDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["person_id"],
            ["people.id"],
            name=op.f("fk_refresh_progress_person_id_people"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "provider", "person_id", name=op.f("pk_refresh_progress")
        ),
    )


def downgrade() -> None:
    """Drop the refresh_progress table."""
    op.drop_table("refresh_progress")
