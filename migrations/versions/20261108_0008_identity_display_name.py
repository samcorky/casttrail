"""A display name on external identities, so unmatched guests have a name.

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the display name column."""
    with op.batch_alter_table("external_identities") as batch_op:
        batch_op.add_column(
            sa.Column("display_name", sa.String(length=200), nullable=True)
        )


def downgrade() -> None:
    """Drop the display name column."""
    with op.batch_alter_table("external_identities") as batch_op:
        batch_op.drop_column("display_name")
