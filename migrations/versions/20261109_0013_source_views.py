"""What each source says about a convention, so disagreements can be shown.

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the columns holding each source's own values."""
    with op.batch_alter_table("convention_sources", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("seen_name", sa.String(length=300), nullable=True)
        )
        batch_op.add_column(sa.Column("seen_start", sa.Date(), nullable=True))
        batch_op.add_column(sa.Column("seen_end", sa.Date(), nullable=True))
        batch_op.add_column(
            sa.Column("seen_venue", sa.String(length=500), nullable=True)
        )


def downgrade() -> None:
    """Remove the columns."""
    with op.batch_alter_table("convention_sources", schema=None) as batch_op:
        batch_op.drop_column("seen_venue")
        batch_op.drop_column("seen_end")
        batch_op.drop_column("seen_start")
        batch_op.drop_column("seen_name")
