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
        select(Person.name, Convention, Match.kind, Match.reason)
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
    return [RelevantAppearance(n, c, k, r) for n, c, k, r in rows]
