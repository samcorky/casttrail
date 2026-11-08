import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from casttrail.db import Base, UtcDateTime, utc_now

MEDIA_TYPES = ("movie", "tv")


class Person(Base):
    """Someone who appears in films or series, or at a convention.

    Which outside ids they have (TMDB, IMDb, a convention site's) lives in
    ``ExternalIdentity``, not here.
    """

    __tablename__ = "people"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200))
    known_for_department: Mapped[str | None] = mapped_column(String(100))
    profile_path: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)


class Production(Base):
    """A film or a series.

    TMDB numbers films and series separately, so the same id can mean a film and a
    series. The pair ``(media_type, tmdb_id)`` is what identifies one.
    """

    __tablename__ = "productions"
    __table_args__ = (
        UniqueConstraint("media_type", "tmdb_id"),
        CheckConstraint("media_type IN ('movie', 'tv')", name="media_type"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    media_type: Mapped[str] = mapped_column(String(5))
    tmdb_id: Mapped[int]
    title: Mapped[str] = mapped_column(String(300))
    release_year: Mapped[int | None]
    poster_path: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)


class Credit(Base):
    """A person's part in a production. A lower ``billing_order`` is higher billing."""

    __tablename__ = "credits"
    __table_args__ = (UniqueConstraint("person_id", "production_id"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    person_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("people.id", ondelete="CASCADE")
    )
    production_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("productions.id", ondelete="CASCADE"), index=True
    )
    character: Mapped[str | None] = mapped_column(String(300))
    billing_order: Mapped[int | None]
