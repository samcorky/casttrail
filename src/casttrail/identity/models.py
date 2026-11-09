import uuid
from datetime import datetime

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


class ExternalIdentity(Base):
    """A person as one outside source knows them: a TMDB id, an IMDb id, a guest slug.

    ``person_id`` is empty until the identity has been linked to a person, so a guest
    nobody has recognised yet is stored without being guessed at. If the person is
    deleted the identity stays and goes back to being unlinked.
    """

    __tablename__ = "external_identities"
    __table_args__ = (UniqueConstraint("provider", "external_id"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    provider: Mapped[str] = mapped_column(String(50))
    external_id: Mapped[str] = mapped_column(String(255))
    # The name the source gives, so an unmatched guest can still be shown by name.
    display_name: Mapped[str | None] = mapped_column(String(200))
    person_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("people.id", ondelete="SET NULL"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)


class IdentityDecision(Base):
    """What a person decided about whether a source's guest is a particular person.

    A decision is never changed by a sync. At most one person can be confirmed for a
    guest, and a rejected person is not offered for that guest again.
    """

    __tablename__ = "identity_decisions"
    __table_args__ = (
        UniqueConstraint("external_identity_id", "person_id"),
        CheckConstraint("decision IN ('confirmed', 'rejected')", name="decision"),
        Index(
            "uq_identity_decisions_one_confirmed",
            "external_identity_id",
            unique=True,
            sqlite_where=text("decision = 'confirmed'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    external_identity_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("external_identities.id", ondelete="CASCADE")
    )
    person_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("people.id", ondelete="CASCADE"), index=True
    )
    decision: Mapped[str] = mapped_column(String(9))
    decided_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    decided_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)
