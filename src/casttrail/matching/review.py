import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import func, select

from casttrail.catalogue.models import Person
from casttrail.discovery.models import Appearance
from casttrail.identity.models import ExternalIdentity
from casttrail.matching.models import Match

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

SEARCH_LIMIT = 20
PEOPLE_LIMIT = 500


@dataclass(frozen=True)
class Suggestion:
    """A guest whose name matches a watched person, waiting to be confirmed."""

    identity_id: uuid.UUID
    guest_name: str
    person_id: uuid.UUID
    person_name: str
    events: int


@dataclass(frozen=True)
class GuestRow:
    """A guest and who they are linked to, if anyone."""

    identity_id: uuid.UUID
    guest_name: str
    person_id: uuid.UUID | None
    person_name: str | None


# A guest is an identity that appears at a convention. A person's own TMDB and IMDb
# ids are identities too, but they are not guests and must never be listed here.
IS_GUEST = (
    select(Appearance.id)
    .where(Appearance.external_identity_id == ExternalIdentity.id)
    .exists()
)

# The name to show for a guest: the source's name, or its key if it gave none.
SHOWN = func.coalesce(ExternalIdentity.display_name, ExternalIdentity.external_id)


async def suggestions(session: AsyncSession) -> list[Suggestion]:
    """List name matches nobody has confirmed or rejected yet.

    Args:
        session: The database session.

    Returns:
        One row per guest and person, ordered by guest name.
    """
    rows = await session.execute(
        select(
            ExternalIdentity.id,
            SHOWN,
            Person.id,
            Person.name,
            func.count(func.distinct(Appearance.id)),
        )
        .join(Appearance, Appearance.external_identity_id == ExternalIdentity.id)
        .join(Match, Match.appearance_id == Appearance.id)
        .join(Person, Person.id == Match.person_id)
        .where(Match.kind == "possible")
        .group_by(ExternalIdentity.id, Person.id)
        .order_by(func.lower(SHOWN), Person.name, Person.id)
    )
    return [Suggestion(i, str(g), p, n, int(c)) for i, g, p, n, c in rows]


async def linked_guests(session: AsyncSession) -> list[GuestRow]:
    """List guests that have been linked to a person.

    Args:
        session: The database session.

    Returns:
        The guests, ordered by name.
    """
    rows = await session.execute(
        select(ExternalIdentity.id, SHOWN, Person.id, Person.name)
        .join(Person, Person.id == ExternalIdentity.person_id)
        .where(IS_GUEST)
        .order_by(func.lower(SHOWN), ExternalIdentity.id)
    )
    return [GuestRow(i, str(g), p, n) for i, g, p, n in rows]


async def find_guests(session: AsyncSession, text: str) -> list[GuestRow]:
    """Find convention guests by part of their name.

    Args:
        session: The database session.
        text: Part of a name. Blank finds nothing.

    Returns:
        Up to ``SEARCH_LIMIT`` guests, with who they are linked to.
    """
    needle = text.strip()
    if not needle:
        return []
    escaped = needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    rows = await session.execute(
        select(ExternalIdentity.id, SHOWN, Person.id, Person.name)
        .outerjoin(Person, Person.id == ExternalIdentity.person_id)
        .where(
            IS_GUEST,
            SHOWN.ilike(f"%{escaped}%", escape="\\"),
        )
        .order_by(func.lower(SHOWN), ExternalIdentity.id)
        .limit(SEARCH_LIMIT)
    )
    return [GuestRow(i, str(g), p, n) for i, g, p, n in rows]


async def people_choices(session: AsyncSession) -> list[tuple[uuid.UUID, str]]:
    """List people a guest can be linked to.

    Args:
        session: The database session.

    Returns:
        Up to ``PEOPLE_LIMIT`` people, ordered by name.
    """
    rows = await session.execute(
        select(Person.id, Person.name)
        .order_by(func.lower(Person.name), Person.id)
        .limit(PEOPLE_LIMIT)
    )
    return [(i, n) for i, n in rows]
