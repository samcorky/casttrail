"""Identity decisions: which guests a person confirmed or rejected as someone.

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from casttrail.db import UtcDateTime

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the identity_decisions table."""
    op.create_table(
        "identity_decisions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("external_identity_id", sa.Uuid(), nullable=False),
        sa.Column("person_id", sa.Uuid(), nullable=False),
        sa.Column("decision", sa.String(length=9), nullable=False),
        sa.Column("decided_by", sa.Uuid(), nullable=True),
        sa.Column("decided_at", UtcDateTime(), nullable=False),
        sa.CheckConstraint(
            "decision IN ('confirmed', 'rejected')",
            name=op.f("ck_identity_decisions_decision"),
        ),
        sa.ForeignKeyConstraint(
            ["external_identity_id"],
            ["external_identities.id"],
            name=op.f("fk_identity_decisions_external_identity_id_external_identities"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["person_id"],
            ["people.id"],
            name=op.f("fk_identity_decisions_person_id_people"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["decided_by"],
            ["users.id"],
            name=op.f("fk_identity_decisions_decided_by_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_identity_decisions")),
        sa.UniqueConstraint(
            "external_identity_id",
            "person_id",
            name=op.f("uq_identity_decisions_external_identity_id"),
        ),
    )
    with op.batch_alter_table("identity_decisions", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_identity_decisions_person_id"), ["person_id"], unique=False
        )
        batch_op.create_index(
            "uq_identity_decisions_one_confirmed",
            ["external_identity_id"],
            unique=True,
            sqlite_where=sa.text("decision = 'confirmed'"),
        )


def downgrade() -> None:
    """Drop the identity_decisions table."""
    with op.batch_alter_table("identity_decisions", schema=None) as batch_op:
        batch_op.drop_index("uq_identity_decisions_one_confirmed")
        batch_op.drop_index(batch_op.f("ix_identity_decisions_person_id"))

    op.drop_table("identity_decisions")
