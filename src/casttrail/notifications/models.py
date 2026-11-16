import uuid
from datetime import date, datetime

from sqlalchemy import CheckConstraint, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from casttrail.db import Base, UtcDateTime, utc_now


def event_key_for(convention_id: uuid.UUID, person_id: uuid.UUID) -> str:
    """Name the news that a person will be at a convention.

    It is keyed on the convention, not the appearance: two sources listing the same
    person at one merged event give two appearances but one piece of news.

    Args:
        convention_id: The convention.
        person_id: The person.

    Returns:
        A stable, never-empty key.
    """
    return f"appear:{convention_id}:{person_id}"


class NotificationEvent(Base):
    """A watched person at a convention, worth telling users about.

    Shared by every user it concerns. ``rev`` goes up when something that matters
    changes (the dates, or a cancellation), and the stored dates and flag are what it
    was last compared with.
    """

    __tablename__ = "notification_events"
    __table_args__ = (
        UniqueConstraint("event_key"),
        CheckConstraint("rev >= 1", name="rev"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    event_key: Mapped[str] = mapped_column(String(100))
    convention_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conventions.id", ondelete="CASCADE"), index=True
    )
    person_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("people.id", ondelete="CASCADE"), index=True
    )
    rev: Mapped[int] = mapped_column(default=1)
    start_date: Mapped[date | None]
    end_date: Mapped[date | None]
    cancelled: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)


class UserNotification(Base):
    """One user's alert about a notification event.

    ``rev`` is the event's revision this user was last told about; when the event
    moves on, the alert is raised again and ``read_at`` is cleared. Alerts belong to a
    user and are only ever read or changed together with that user's id.
    """

    __tablename__ = "user_notifications"
    __table_args__ = (
        UniqueConstraint("user_id", "event_id"),
        CheckConstraint("rev >= 1", name="rev"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE")
    )
    event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("notification_events.id", ondelete="CASCADE"), index=True
    )
    rev: Mapped[int] = mapped_column(default=1)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)
    # When the alert was last raised: first made, or raised again by a new ``rev``.
    raised_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)
    read_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
