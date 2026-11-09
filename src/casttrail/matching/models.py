import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from casttrail.db import Base, UtcDateTime, utc_now


class Match(Base):
    """One user's watched person turning up as a guest at a convention.

    ``kind`` is ``confirmed`` when a person linked the guest or an id is shared, and
    ``possible`` when only the name matched. Alerts are for confirmed matches only.
    ``match_key`` says what was matched (``person:<id>`` for a watched person) and is
    never empty, so ``(user_id, appearance_id, match_key)`` is unique even where
    ``watch_id`` is empty.
    """

    __tablename__ = "matches"
    __table_args__ = (
        UniqueConstraint("user_id", "appearance_id", "match_key"),
        CheckConstraint("kind IN ('confirmed', 'possible')", name="kind"),
        CheckConstraint("match_key <> ''", name="match_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    appearance_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("appearances.id", ondelete="CASCADE"), index=True
    )
    person_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("people.id", ondelete="CASCADE")
    )
    watch_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("watches.id", ondelete="SET NULL")
    )
    match_key: Mapped[str] = mapped_column(String(100))
    kind: Mapped[str] = mapped_column(String(9))
    reason: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)
