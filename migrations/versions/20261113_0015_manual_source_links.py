"""Manual source links: a person can set a source lookup by hand.

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add source_lookups.manual, which only a found row may have set."""
    with op.batch_alter_table("source_lookups", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "manual", sa.Boolean(), server_default=sa.text("0"), nullable=False
            )
        )
        batch_op.create_check_constraint(
            op.f("ck_source_lookups_manual_is_found"),
            "NOT manual OR outcome = 'found'",
        )


def downgrade() -> None:
    """Remove source_lookups.manual."""
    with op.batch_alter_table("source_lookups", schema=None) as batch_op:
        batch_op.drop_constraint(
            op.f("ck_source_lookups_manual_is_found"), type_="check"
        )
        batch_op.drop_column("manual")
