"""Conventions, their sources, observations and appearances.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from casttrail.db import UtcDateTime

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the discovery tables."""
    op.create_table(
        "conventions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=300), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=True),
        sa.Column("end_date", sa.Date(), nullable=True),
        sa.Column("venue", sa.String(length=500), nullable=True),
        sa.Column("city", sa.String(length=100), nullable=True),
        sa.Column("region", sa.String(length=100), nullable=True),
        sa.Column("country", sa.String(length=100), nullable=True),
        sa.Column("organizer", sa.String(length=200), nullable=True),
        sa.Column("status", sa.String(length=10), nullable=True),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.Column("updated_at", UtcDateTime(), nullable=False),
        sa.CheckConstraint(
            "status IS NULL OR status IN ('scheduled', 'cancelled', 'postponed')",
            name=op.f("ck_conventions_status"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_conventions")),
    )
    with op.batch_alter_table("conventions", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_conventions_country"), ["country"], unique=False
        )

    op.create_table(
        "source_observations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("source_key", sa.String(length=255), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("complete", sa.Boolean(), nullable=False),
        sa.Column("fetched_at", UtcDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_source_observations")),
    )
    with op.batch_alter_table("source_observations", schema=None) as batch_op:
        batch_op.create_index(
            "ix_source_observations_key_time",
            ["provider", "source_key", "fetched_at"],
            unique=False,
        )

    op.create_table(
        "convention_sources",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("convention_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("source_key", sa.String(length=255), nullable=False),
        sa.Column("source_url", sa.String(length=500), nullable=False),
        sa.Column("first_seen_at", UtcDateTime(), nullable=False),
        sa.Column("last_seen_at", UtcDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["convention_id"],
            ["conventions.id"],
            name=op.f("fk_convention_sources_convention_id_conventions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_convention_sources")),
        sa.UniqueConstraint(
            "provider", "source_key", name=op.f("uq_convention_sources_provider")
        ),
    )
    with op.batch_alter_table("convention_sources", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_convention_sources_convention_id"),
            ["convention_id"],
            unique=False,
        )

    op.create_table(
        "appearances",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("convention_id", sa.Uuid(), nullable=False),
        sa.Column("external_identity_id", sa.Uuid(), nullable=False),
        sa.Column("person_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["convention_id"],
            ["conventions.id"],
            name=op.f("fk_appearances_convention_id_conventions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["external_identity_id"],
            ["external_identities.id"],
            name=op.f("fk_appearances_external_identity_id_external_identities"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["person_id"],
            ["people.id"],
            name=op.f("fk_appearances_person_id_people"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_appearances")),
        sa.UniqueConstraint(
            "convention_id",
            "external_identity_id",
            name=op.f("uq_appearances_convention_id"),
        ),
    )
    with op.batch_alter_table("appearances", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_appearances_external_identity_id"),
            ["external_identity_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_appearances_person_id"), ["person_id"], unique=False
        )

    op.create_table(
        "appearance_sources",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("appearance_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("source_url", sa.String(length=500), nullable=False),
        sa.Column("listed", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=10), nullable=True),
        sa.Column("first_seen_at", UtcDateTime(), nullable=False),
        sa.Column("last_seen_at", UtcDateTime(), nullable=False),
        sa.CheckConstraint(
            "status IS NULL OR status IN ('scheduled', 'cancelled', 'postponed')",
            name=op.f("ck_appearance_sources_status"),
        ),
        sa.ForeignKeyConstraint(
            ["appearance_id"],
            ["appearances.id"],
            name=op.f("fk_appearance_sources_appearance_id_appearances"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_appearance_sources")),
        sa.UniqueConstraint(
            "appearance_id",
            "provider",
            name=op.f("uq_appearance_sources_appearance_id"),
        ),
    )


def downgrade() -> None:
    """Drop the discovery tables."""
    op.drop_table("appearance_sources")
    with op.batch_alter_table("appearances", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_appearances_person_id"))
        batch_op.drop_index(batch_op.f("ix_appearances_external_identity_id"))

    op.drop_table("appearances")
    with op.batch_alter_table("convention_sources", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_convention_sources_convention_id"))

    op.drop_table("convention_sources")
    with op.batch_alter_table("source_observations", schema=None) as batch_op:
        batch_op.drop_index("ix_source_observations_key_time")

    op.drop_table("source_observations")
    with op.batch_alter_table("conventions", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_conventions_country"))

    op.drop_table("conventions")
