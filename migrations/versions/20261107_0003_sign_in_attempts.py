"""Failed sign-in attempts, for throttling.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from casttrail.db import UtcDateTime

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the sign_in_attempts table."""
    op.create_table(
        "sign_in_attempts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("attempted_at", UtcDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sign_in_attempts")),
    )
    op.create_index(
        op.f("ix_sign_in_attempts_key_hash"), "sign_in_attempts", ["key_hash"]
    )


def downgrade() -> None:
    """Drop the sign_in_attempts table."""
    op.drop_index(op.f("ix_sign_in_attempts_key_hash"), table_name="sign_in_attempts")
    op.drop_table("sign_in_attempts")
