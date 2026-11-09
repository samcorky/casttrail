from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

from sqlalchemy import func, or_, select

from casttrail.discovery.models import (
    Appearance,
    AppearanceSource,
    Convention,
    ConventionSource,
)
from casttrail.identity.models import ExternalIdentity

if TYPE_CHECKING:
    import uuid
    from datetime import date

    from sqlalchemy.ext.asyncio import AsyncSession

UK = "united-kingdom"
# An event that began a day or two ago may still be on.
_GRACE = timedelta(days=2)
PAGE_SIZE = 50


@dataclass(frozen=True)
class EventRow:
    """One convention in the list."""

    convention: Convention
    guests: int


@dataclass(frozen=True)
class GuestRow:
    """One guest at a convention, as a source lists them."""

    name: str
    source_url: str
    listed: bool
    status: str | None


@dataclass(frozen=True)
class EventDetail:
    """A convention, where it was found, and its guests."""

    convention: Convention
    source_urls: list[str]
    guests: list[GuestRow]


async def upcoming_events(
    session: AsyncSession, *, today: date, uk_only: bool = True, page: int = 1
) -> tuple[list[EventRow], bool]:
    """List conventions that have not finished, soonest first.

    Events with no date come last, so a missing date is shown and not hidden.

    Args:
        session: The database session.
        today: The date to count from.
        uk_only: Only conventions Rostercon places in the United Kingdom. Ones with no
            known country are left out of this view.
        page: The page, from 1.

    Returns:
        The rows of that page, and whether there is a later page.
    """
    cutoff = today - _GRACE
    guests = (
        select(func.count(AppearanceSource.id))
        .join(Appearance, Appearance.id == AppearanceSource.appearance_id)
        .where(Appearance.convention_id == Convention.id, AppearanceSource.listed)
        .scalar_subquery()
    )
    query = select(Convention, guests).where(
        or_(
            Convention.start_date.is_(None),
            func.coalesce(Convention.end_date, Convention.start_date) >= cutoff,
        )
    )
    if uk_only:
        query = query.where(Convention.country == UK)
    query = (
        query.order_by(
            Convention.start_date.is_(None),
            Convention.start_date,
            Convention.name,
            Convention.id,
        )
        .offset((max(page, 1) - 1) * PAGE_SIZE)
        .limit(PAGE_SIZE + 1)
    )
    rows = [EventRow(c, int(n or 0)) for c, n in await session.execute(query)]
    return rows[:PAGE_SIZE], len(rows) > PAGE_SIZE


async def event_detail(
    session: AsyncSession, convention_id: uuid.UUID
) -> EventDetail | None:
    """Load one convention with its sources and guests.

    Args:
        session: The database session.
        convention_id: The convention.

    Returns:
        The detail, or ``None`` if there is no such convention.
    """
    convention = await session.get(Convention, convention_id)
    if convention is None:
        return None
    urls = await session.scalars(
        select(ConventionSource.source_url)
        .where(ConventionSource.convention_id == convention_id)
        .order_by(ConventionSource.provider)
    )
    shown = func.lower(
        func.coalesce(ExternalIdentity.display_name, ExternalIdentity.external_id)
    )
    rows = await session.execute(
        select(
            ExternalIdentity.display_name,
            ExternalIdentity.external_id,
            AppearanceSource.source_url,
            AppearanceSource.listed,
            AppearanceSource.status,
        )
        .join(Appearance, Appearance.external_identity_id == ExternalIdentity.id)
        .join(AppearanceSource, AppearanceSource.appearance_id == Appearance.id)
        .where(Appearance.convention_id == convention_id)
        .order_by(AppearanceSource.listed.desc(), shown, AppearanceSource.id)
    )
    guests = [
        GuestRow(name=name or key, source_url=url, listed=listed, status=status)
        for name, key, url, listed, status in rows
    ]
    return EventDetail(convention, list(urls), guests)
