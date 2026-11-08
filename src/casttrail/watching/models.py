import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from casttrail.catalogue.models import Person, Production
from casttrail.db import Base, UtcDateTime, utc_now


class Watch(Base):
    """One user following one person or one film or series.

    Exactly one of ``person_id`` and ``production_id`` is set. A user can follow the
    same thing only once. Watches belong to a user and are only ever read or removed
    together with that user's id.
    """

    __tablename__ = "watches"
    __table_args__ = (
        CheckConstraint(
            "(person_id IS NOT NULL) + (production_id IS NOT NULL) = 1",
            name="one_target",
        ),
        UniqueConstraint("user_id", "person_id", name="uq_watches_user_person"),
        UniqueConstraint("user_id", "production_id", name="uq_watches_user_production"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE")
    )
    person_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("people.id", ondelete="CASCADE"), index=True
    )
    production_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("productions.id", ondelete="CASCADE"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)

    person: Mapped[Person | None] = relationship(lazy="joined")
    production: Mapped[Production | None] = relationship(lazy="joined")
