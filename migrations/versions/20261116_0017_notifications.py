"""Alerts in the app: notification events, each user's alerts, and watch baselines.

Revision ID: 0017
Revises: 0016
Create Date: 2026-11-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from casttrail.db import UtcDateTime

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the notification tables and start every existing watch's alerts now."""
    op.create_table(
        "notification_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("event_key", sa.String(length=100), nullable=False),
        sa.Column("convention_id", sa.Uuid(), nullable=False),
        sa.Column("person_id", sa.Uuid(), nullable=False),
        sa.Column("rev", sa.Integer(), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=True),
        sa.Column("end_date", sa.Date(), nullable=True),
        sa.Column("cancelled", sa.Boolean(), nullable=False),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.Column("updated_at", UtcDateTime(), nullable=False),
        sa.CheckConstraint("rev >= 1", name=op.f("ck_notification_events_rev")),
        sa.ForeignKeyConstraint(
            ["convention_id"],
            ["conventions.id"],
            name=op.f("fk_notification_events_convention_id_conventions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["person_id"],
            ["people.id"],
            name=op.f("fk_notification_events_person_id_people"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notification_events")),
        sa.UniqueConstraint("event_key", name=op.f("uq_notification_events_event_key")),
    )
    op.create_index(
        op.f("ix_notification_events_convention_id"),
        "notification_events",
        ["convention_id"],
    )
    op.create_index(
        op.f("ix_notification_events_person_id"), "notification_events", ["person_id"]
    )
    op.create_table(
        "user_notifications",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("rev", sa.Integer(), nullable=False),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.Column("raised_at", UtcDateTime(), nullable=False),
        sa.Column("read_at", UtcDateTime(), nullable=True),
        sa.CheckConstraint("rev >= 1", name=op.f("ck_user_notifications_rev")),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["notification_events.id"],
            name=op.f("fk_user_notifications_event_id_notification_events"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_user_notifications_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_notifications")),
        sa.UniqueConstraint(
            "user_id", "event_id", name=op.f("uq_user_notifications_user_id")
        ),
    )
    op.create_index(
        op.f("ix_user_notifications_event_id"), "user_notifications", ["event_id"]
    )
    # Existing watches start their alerts at this upgrade, so turning alerts on does
    # not raise one for everything already found (the baseline).
    with op.batch_alter_table("watches") as batch:
        batch.add_column(
            sa.Column(
                "alerts_from",
                UtcDateTime(),
                nullable=False,
                server_default=sa.func.current_timestamp(),
            )
        )
    with op.batch_alter_table("watches") as batch:
        batch.alter_column("alerts_from", server_default=None)


def downgrade() -> None:
    """Drop the notification tables and the watch baselines."""
    with op.batch_alter_table("watches") as batch:
        batch.drop_column("alerts_from")
    op.drop_index(op.f("ix_user_notifications_event_id"), "user_notifications")
    op.drop_table("user_notifications")
    op.drop_index(op.f("ix_notification_events_person_id"), "notification_events")
    op.drop_index(op.f("ix_notification_events_convention_id"), "notification_events")
    op.drop_table("notification_events")
