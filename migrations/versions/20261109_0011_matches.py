"""Matches: a user's watched person appearing as a guest at a convention.

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from casttrail.db import UtcDateTime

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the matches table."""
    op.create_table(
        "matches",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("appearance_id", sa.Uuid(), nullable=False),
        sa.Column("person_id", sa.Uuid(), nullable=False),
        sa.Column("watch_id", sa.Uuid(), nullable=True),
        sa.Column("match_key", sa.String(length=100), nullable=False),
        sa.Column("kind", sa.String(length=9), nullable=False),
        sa.Column("reason", sa.String(length=200), nullable=False),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.Column("updated_at", UtcDateTime(), nullable=False),
        sa.CheckConstraint(
            "kind IN ('confirmed', 'possible')", name=op.f("ck_matches_kind")
        ),
        sa.CheckConstraint("match_key <> ''", name=op.f("ck_matches_match_key")),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_matches_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["appearance_id"],
            ["appearances.id"],
            name=op.f("fk_matches_appearance_id_appearances"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["person_id"],
            ["people.id"],
            name=op.f("fk_matches_person_id_people"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["watch_id"],
            ["watches.id"],
            name=op.f("fk_matches_watch_id_watches"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_matches")),
        sa.UniqueConstraint(
            "user_id", "appearance_id", "match_key", name=op.f("uq_matches_user_id")
        ),
    )
    with op.batch_alter_table("matches", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_matches_user_id"), ["user_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_matches_appearance_id"), ["appearance_id"], unique=False
        )


def downgrade() -> None:
    """Drop the matches table."""
    with op.batch_alter_table("matches", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_matches_appearance_id"))
        batch_op.drop_index(batch_op.f("ix_matches_user_id"))

    op.drop_table("matches")
