"""Source lookups: what a source's search said about a watched person.

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from casttrail.db import UtcDateTime

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the source_lookups table."""
    op.create_table(
        "source_lookups",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("person_id", sa.Uuid(), nullable=False),
        sa.Column("outcome", sa.String(length=9), nullable=False),
        sa.Column("source_key", sa.String(length=255), nullable=True),
        sa.Column("source_url", sa.String(length=500), nullable=True),
        sa.Column("looked_up_at", UtcDateTime(), nullable=False),
        sa.CheckConstraint(
            "(outcome = 'found') = (source_key IS NOT NULL AND source_url IS NOT NULL)",
            name=op.f("ck_source_lookups_found_has_a_key"),
        ),
        sa.CheckConstraint(
            "outcome IN ('found', 'not_found', 'ambiguous')",
            name=op.f("ck_source_lookups_outcome"),
        ),
        sa.ForeignKeyConstraint(
            ["person_id"],
            ["people.id"],
            name=op.f("fk_source_lookups_person_id_people"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_source_lookups")),
        sa.UniqueConstraint(
            "provider", "person_id", name=op.f("uq_source_lookups_provider")
        ),
    )


def downgrade() -> None:
    """Drop the source_lookups table."""
    op.drop_table("source_lookups")
