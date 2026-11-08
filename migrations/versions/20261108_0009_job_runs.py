"""Job runs, which also hold the lock that stops a job running twice at once.

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from casttrail.db import UtcDateTime

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the job_runs table."""
    op.create_table(
        "job_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job", sa.String(length=50), nullable=False),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("started_at", UtcDateTime(), nullable=False),
        sa.Column("finished_at", UtcDateTime(), nullable=True),
        sa.Column("summary", sa.String(length=2000), nullable=True),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'failed')",
            name=op.f("ck_job_runs_status"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_job_runs")),
    )
    with op.batch_alter_table("job_runs", schema=None) as batch_op:
        batch_op.create_index(
            "ix_job_runs_job_started", ["job", "started_at"], unique=False
        )
        batch_op.create_index(
            "uq_job_runs_one_running",
            ["job"],
            unique=True,
            sqlite_where=sa.text("status = 'running'"),
        )


def downgrade() -> None:
    """Drop the job_runs table."""
    with op.batch_alter_table("job_runs", schema=None) as batch_op:
        batch_op.drop_index("uq_job_runs_one_running")
        batch_op.drop_index("ix_job_runs_job_started")

    op.drop_table("job_runs")
