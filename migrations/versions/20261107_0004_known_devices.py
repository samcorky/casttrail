"""Known devices, for the sign-in throttle.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from casttrail.db import UtcDateTime

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the known_devices table."""
    op.create_table(
        "known_devices",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_known_devices_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_known_devices")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_known_devices_token_hash")),
    )
    op.create_index(op.f("ix_known_devices_user_id"), "known_devices", ["user_id"])


def downgrade() -> None:
    """Drop the known_devices table."""
    op.drop_index(op.f("ix_known_devices_user_id"), table_name="known_devices")
    op.drop_table("known_devices")
