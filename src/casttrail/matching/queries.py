from dataclasses import dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import func, or_, select

from casttrail.catalogue.models import Person
from casttrail.discovery.models import Appearance, Convention
from casttrail.matching.models import Match

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

_GRACE = timedelta(days=2)


@dataclass(frozen=True)
class RelevantAppearance:
    """A watched person at a convention, and how sure we are."""

    person_name: str
    convention: Convention
    kind: str
    reason: str


async def relevant_appearances(
    session: AsyncSession, user_id: uuid.UUID, *, today: date
) -> list[RelevantAppearance]:
    """List where the people a user watches will be, soonest first.

    Only this user's matches are read. Finished conventions are left out, and ones with
    no date come last.

    Args:
        session: The database session.
        user_id: The signed-in user.
        today: The date to count from.

    Returns:
        The matches, confirmed before possible on the same day.
    """
    cutoff = today - _GRACE
    rows = await session.execute(
        select(Person.id, Person.name, Convention, Match.kind, Match.reason)
        .join(Match, Match.person_id == Person.id)
        .join(Appearance, Appearance.id == Match.appearance_id)
        .join(Convention, Convention.id == Appearance.convention_id)
        .where(
            Match.user_id == user_id,
            or_(
                Convention.start_date.is_(None),
                func.coalesce(Convention.end_date, Convention.start_date) >= cutoff,
            ),
        )
        .order_by(
            Convention.start_date.is_(None),
            Convention.start_date,
            Match.kind,
            Person.name,
            Convention.id,
        )
    )
    # A person at an event two sources list has two matches. Rows come soonest first
    # and, within one event, confirmed before possible, so the first one is the surest.
    best: dict[tuple[uuid.UUID, uuid.UUID], RelevantAppearance] = {}
    for person_id, name, convention, kind, reason in rows:
        best.setdefault(
            (person_id, convention.id),
            RelevantAppearance(name, convention, kind, reason),
        )
    return list(best.values())


@dataclass(frozen=True)
class WatchedGuest:
    """A person the user watches who is a guest at a convention."""

    name: str
    kind: str


async def watched_guests(
    session: AsyncSession, user_id: uuid.UUID, convention_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[WatchedGuest]]:
    """List the people a user watches at each of some conventions.

    Only this user's matches are read. A person two sources list counts once, as
    confirmed if either match is.

    Args:
        session: The database session.
        user_id: The signed-in user.
        convention_ids: The conventions to look at.

    Returns:
        For each convention with any, its watched guests, confirmed first, then by name.
    """
    if not convention_ids:
        return {}
    rows = await session.execute(
        select(Appearance.convention_id, Person.id, Person.name, Match.kind)
        .join(Match, Match.appearance_id == Appearance.id)
        .join(Person, Person.id == Match.person_id)
        .where(Match.user_id == user_id, Appearance.convention_id.in_(convention_ids))
        .order_by(Match.kind, Person.name, Person.id)
    )
    # "confirmed" sorts before "possible", so the first row per person is the surest.
    best: dict[tuple[uuid.UUID, uuid.UUID], WatchedGuest] = {}
    for convention_id, person_id, name, kind in rows:
        best.setdefault((convention_id, person_id), WatchedGuest(name, kind))
    found: dict[uuid.UUID, list[WatchedGuest]] = {}
    for (convention_id, _), guest in best.items():
        found.setdefault(convention_id, []).append(guest)
    return found
