import uuid
from datetime import date, datetime

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from casttrail.db import Base, UtcDateTime, utc_now

STATUSES = ("scheduled", "cancelled", "postponed")


class Convention(Base):
    """A convention as the app knows it, filled in from whichever source lists it.

    ``None`` means no source said. Only ``status = 'cancelled'``, stated by a source,
    is a cancellation: an event that vanishes from a listing keeps its last status.
    """

    __tablename__ = "conventions"
    __table_args__ = (
        CheckConstraint(
            "status IS NULL OR status IN ('scheduled', 'cancelled', 'postponed')",
            name="status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(300))
    start_date: Mapped[date | None]
    end_date: Mapped[date | None]
    venue: Mapped[str | None] = mapped_column(String(500))
    city: Mapped[str | None] = mapped_column(String(100))
    region: Mapped[str | None] = mapped_column(String(100))
    # The source's own country slug, such as "united-kingdom".
    country: Mapped[str | None] = mapped_column(String(100), index=True)
    organizer: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str | None] = mapped_column(String(10))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)


class ConventionSource(Base):
    """Which source lists a convention, and under what key.

    A convention can be listed by several sources once duplicates are merged; one
    source's key points at exactly one convention.
    """

    __tablename__ = "convention_sources"
    __table_args__ = (UniqueConstraint("provider", "source_key"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    convention_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conventions.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[str] = mapped_column(String(50))
    source_key: Mapped[str] = mapped_column(String(255))
    source_url: Mapped[str] = mapped_column(String(500))
    # What this source itself says, kept apart from the merged convention so that
    # disagreements between sources can be shown. ``None`` means it did not say.
    seen_name: Mapped[str | None] = mapped_column(String(300))
    seen_start: Mapped[date | None]
    seen_end: Mapped[date | None]
    seen_venue: Mapped[str | None] = mapped_column(String(500))
    first_seen_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)
    last_seen_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)


class SourceObservation(Base):
    """A record that a source's page for one convention was fetched.

    Only a hash of what was parsed is kept, not the page or its parsed data: the hash
    is enough to see that nothing changed, and saved fixtures cover debugging. Old
    rows are pruned to the last few per ``(provider, source_key)``.
    """

    __tablename__ = "source_observations"
    __table_args__ = (
        Index(
            "ix_source_observations_key_time", "provider", "source_key", "fetched_at"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    provider: Mapped[str] = mapped_column(String(50))
    source_key: Mapped[str] = mapped_column(String(255))
    content_hash: Mapped[str] = mapped_column(String(64))
    # Whether the guest list was whole and understood (``ListingResult.complete``).
    complete: Mapped[bool]
    fetched_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)


class Appearance(Base):
    """One guest at one convention.

    ``external_identity_id`` is the guest as a source names them. ``person_id`` is the
    person they were matched to, and stays empty until someone or something links it.
    """

    __tablename__ = "appearances"
    __table_args__ = (UniqueConstraint("convention_id", "external_identity_id"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    convention_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conventions.id", ondelete="CASCADE")
    )
    external_identity_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("external_identities.id", ondelete="CASCADE"), index=True
    )
    person_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("people.id", ondelete="SET NULL"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)


class AppearanceSource(Base):
    """Which source lists an appearance, and whether it still does.

    ``listed`` goes false only when a complete listing leaves the guest out, so a
    guest dropping off a page is shown as "no longer listed" and never as cancelled.
    ``status`` is the source's own word for the guest, and is empty unless it gave one.
    """

    __tablename__ = "appearance_sources"
    __table_args__ = (
        UniqueConstraint("appearance_id", "provider"),
        CheckConstraint(
            "status IS NULL OR status IN ('scheduled', 'cancelled', 'postponed')",
            name="status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    appearance_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("appearances.id", ondelete="CASCADE")
    )
    provider: Mapped[str] = mapped_column(String(50))
    source_url: Mapped[str] = mapped_column(String(500))
    listed: Mapped[bool] = mapped_column(default=True)
    status: Mapped[str | None] = mapped_column(String(10))
    first_seen_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)
    last_seen_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)


class JobRun(Base):
    """One run of a background job, and the lock that stops two running at once.

    At most one row per job can be ``running``: the database refuses a second, so the
    lock holds across processes. A run that is still ``running`` long after it began
    is taken to have died and is replaced.
    """

    __tablename__ = "job_runs"
    __table_args__ = (
        CheckConstraint("status IN ('running', 'succeeded', 'failed')", name="status"),
        Index(
            "uq_job_runs_one_running",
            "job",
            unique=True,
            sqlite_where=text("status = 'running'"),
        ),
        Index("ix_job_runs_job_started", "job", "started_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    job: Mapped[str] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(String(10), default="running")
    started_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    # What the run did, as JSON of counts. Never holds credentials or page text.
    summary: Mapped[str | None] = mapped_column(String(2000))


class ConventionMerge(Base):
    """A pair of conventions that might be the same event, and what was decided.

    The pair is stored once, with the smaller id first. A ``suggested`` pair waits for
    a person; ``rejected`` pairs are never suggested again; ``merged`` records that the
    two were joined.
    """

    __tablename__ = "convention_merges"
    __table_args__ = (
        UniqueConstraint("first_id", "second_id"),
        CheckConstraint("first_id <> second_id", name="distinct_pair"),
        CheckConstraint("status IN ('suggested', 'rejected', 'merged')", name="status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    first_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conventions.id", ondelete="CASCADE")
    )
    second_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conventions.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(String(9), default="suggested")
    reason: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)
    decided_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
