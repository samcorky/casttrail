import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, String, UniqueConstraint
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
    person_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("people.id", ondelete="SET NULL"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)
