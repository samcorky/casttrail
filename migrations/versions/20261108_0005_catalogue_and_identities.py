"""People, productions, credits and external identities.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from casttrail.db import UtcDateTime

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the catalogue and identity tables."""
    op.create_table(
        "people",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("known_for_department", sa.String(length=100), nullable=True),
        sa.Column("profile_path", sa.String(length=255), nullable=True),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_people")),
    )
    op.create_table(
        "productions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("media_type", sa.String(length=5), nullable=False),
        sa.Column("tmdb_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("release_year", sa.Integer(), nullable=True),
        sa.Column("poster_path", sa.String(length=255), nullable=True),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.CheckConstraint(
            "media_type IN ('movie', 'tv')", name=op.f("ck_productions_media_type")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_productions")),
        sa.UniqueConstraint(
            "media_type", "tmdb_id", name=op.f("uq_productions_media_type")
        ),
    )
    op.create_table(
        "credits",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("person_id", sa.Uuid(), nullable=False),
        sa.Column("production_id", sa.Uuid(), nullable=False),
        sa.Column("character", sa.String(length=300), nullable=True),
        sa.Column("billing_order", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["person_id"],
            ["people.id"],
            name=op.f("fk_credits_person_id_people"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["production_id"],
            ["productions.id"],
            name=op.f("fk_credits_production_id_productions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_credits")),
        sa.UniqueConstraint(
            "person_id", "production_id", name=op.f("uq_credits_person_id")
        ),
    )
    op.create_index(op.f("ix_credits_production_id"), "credits", ["production_id"])
    op.create_table(
        "external_identities",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=False),
        sa.Column("person_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["person_id"],
            ["people.id"],
            name=op.f("fk_external_identities_person_id_people"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_external_identities")),
        sa.UniqueConstraint(
            "provider", "external_id", name=op.f("uq_external_identities_provider")
        ),
    )
    op.create_index(
        op.f("ix_external_identities_person_id"), "external_identities", ["person_id"]
    )


def downgrade() -> None:
    """Drop the catalogue and identity tables."""
    op.drop_index(
        op.f("ix_external_identities_person_id"), table_name="external_identities"
    )
    op.drop_table("external_identities")
    op.drop_index(op.f("ix_credits_production_id"), table_name="credits")
    op.drop_table("credits")
    op.drop_table("productions")
    op.drop_table("people")
