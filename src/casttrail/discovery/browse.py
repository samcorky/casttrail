from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import func, or_, select

from casttrail.discovery.dedupe import normalise_event_name
from casttrail.discovery.models import (
    Appearance,
    AppearanceSource,
    Convention,
    ConventionSource,
)
from casttrail.identity.models import ExternalIdentity

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

UK = "united-kingdom"
# An event that began a day or two ago may still be on.
_GRACE = timedelta(days=2)
PAGE_SIZE = 50

_LABELS = {"rostercon": "Rostercon", "comiconomicon": "Comiconomicon"}


def source_label(provider: str) -> str:
    """Return the name to show for a source.

    Args:
        provider: The provider id.

    Returns:
        A readable name, or the id itself for a source without one.
    """
    return _LABELS.get(provider, provider)


@dataclass(frozen=True)
class EventRow:
    """One convention in the list."""

    convention: Convention
    guests: int


@dataclass(frozen=True)
class SourceView:
    """What one source says about a convention."""

    provider: str
    label: str
    url: str
    name: str | None
    start_date: date | None
    end_date: date | None
    venue: str | None


@dataclass(frozen=True)
class GuestSource:
    """One source's word about a guest."""

    provider: str
    label: str
    url: str
    listed: bool
    status: str | None


@dataclass
class Guest:
    """A guest at a convention, with what each source says about them."""

    name: str
    sources: list[GuestSource] = field(default_factory=list)

    @property
    def verdict(self) -> str:
        """The overall status, from what the sources said and nothing more."""
        statuses = {s.status for s in self.sources if s.listed}
        if any(s.status == "cancelled" for s in self.sources):
            return "cancelled"
        if not any(s.listed for s in self.sources):
            return "unlisted"
        if "postponed" in statuses:
            return "postponed"
        return "confirmed" if "scheduled" in statuses else "listed"

    @property
    def disagree(self) -> bool:
        """Whether one source says cancelled while another still lists the guest."""
        cancelled = any(s.status == "cancelled" for s in self.sources)
        plain = any(s.listed and s.status != "cancelled" for s in self.sources)
        return cancelled and plain


@dataclass(frozen=True)
class EventDetail:
    """A convention, what each source says about it, and its guests."""

    convention: Convention
    sources: list[SourceView]
    guests: list[Guest]
    # Which of "name", "start" and "end" the sources disagree on.
    conflicts: frozenset[str]


def _conflicts(views: list[SourceView]) -> frozenset[str]:
    """Find the facts that sources state differently. Unstated facts never conflict."""
    found: set[str] = set()
    names = {normalise_event_name(v.name) for v in views if v.name}
    starts = {v.start_date for v in views if v.start_date}
    ends = {v.end_date for v in views if v.end_date}
    for label, values in (("name", names), ("start", starts), ("end", ends)):
        if len(values) > 1:
            found.add(label)
    return frozenset(found)


def _who(entries: list[tuple[str | None, str, uuid.UUID | None]]) -> list[object]:
    """Say who each guest listing is, so one person listed twice counts once.

    A listing linked to a person is that person. An unlinked listing with the same name
    as a linked one at the same event is that person too. Otherwise a listing is known
    by its name alone, compared without case, accents or punctuation.

    Args:
        entries: Each listing's name, its source key and the person it is linked to.

    Returns:
        One identity per listing, in the same order.
    """
    linked = {
        normalise_event_name(name or key): person_id
        for name, key, person_id in entries
        if person_id
    }
    return [
        person_id
        or linked.get(normalise_event_name(name or key))
        or normalise_event_name(name or key)
        for name, key, person_id in entries
    ]


async def _guest_counts(
    session: AsyncSession, convention_ids: list[uuid.UUID]
) -> dict[uuid.UUID, int]:
    """Count distinct listed guests per convention, one person once across sources."""
    if not convention_ids:
        return {}
    rows = await session.execute(
        select(
            Appearance.convention_id,
            Appearance.person_id,
            ExternalIdentity.display_name,
            ExternalIdentity.external_id,
        )
        .join(ExternalIdentity, ExternalIdentity.id == Appearance.external_identity_id)
        .join(AppearanceSource, AppearanceSource.appearance_id == Appearance.id)
        .where(Appearance.convention_id.in_(convention_ids), AppearanceSource.listed)
    )
    by_convention: dict[uuid.UUID, list[tuple[str | None, str, uuid.UUID | None]]] = {}
    for convention_id, person_id, name, key in rows:
        by_convention.setdefault(convention_id, []).append((name, key, person_id))
    seen = {cid: set(_who(entries)) for cid, entries in by_convention.items()}
    return {cid: len(people) for cid, people in seen.items()}


async def upcoming_events(
    session: AsyncSession, *, today: date, uk_only: bool = True, page: int = 1
) -> tuple[list[EventRow], bool]:
    """List conventions that have not finished, soonest first.

    Events with no date come last, so a missing date is shown and not hidden.

    Args:
        session: The database session.
        today: The date to count from.
        uk_only: Only conventions placed in the United Kingdom. Ones with no known
            country are left out of this view.
        page: The page, from 1.

    Returns:
        The rows of that page, and whether there is a later page.
    """
    cutoff = today - _GRACE
    query = select(Convention).where(
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
    found = list(await session.scalars(query))
    counts = await _guest_counts(session, [c.id for c in found[:PAGE_SIZE]])
    rows = [EventRow(c, counts.get(c.id, 0)) for c in found[:PAGE_SIZE]]
    return rows, len(found) > PAGE_SIZE


async def event_detail(
    session: AsyncSession, convention_id: uuid.UUID
) -> EventDetail | None:
    """Load one convention with what each source says and its guests.

    A guest who is the same person in several sources is shown once, with each
    source's word. Guests are joined by the person they were linked to or, failing
    that, by the same name at this convention.

    Args:
        session: The database session.
        convention_id: The convention.

    Returns:
        The detail, or ``None`` if there is no such convention.
    """
    convention = await session.get(Convention, convention_id)
    if convention is None:
        return None
    views = [
        SourceView(
            provider=source.provider,
            label=source_label(source.provider),
            url=source.source_url,
            name=source.seen_name,
            start_date=source.seen_start,
            end_date=source.seen_end,
            venue=source.seen_venue,
        )
        for source in await session.scalars(
            select(ConventionSource)
            .where(ConventionSource.convention_id == convention_id)
            .order_by(ConventionSource.provider)
        )
    ]
    rows = await session.execute(
        select(
            ExternalIdentity.display_name,
            ExternalIdentity.external_id,
            Appearance.person_id,
            AppearanceSource.provider,
            AppearanceSource.source_url,
            AppearanceSource.listed,
            AppearanceSource.status,
        )
        .join(Appearance, Appearance.external_identity_id == ExternalIdentity.id)
        .join(AppearanceSource, AppearanceSource.appearance_id == Appearance.id)
        .where(Appearance.convention_id == convention_id)
        .order_by(AppearanceSource.provider, AppearanceSource.id)
    )
    entries = list(rows)
    people = _who([(name, key, person_id) for name, key, person_id, *_ in entries])
    grouped: dict[object, Guest] = {}
    for who, (name, key, _, provider, url, listed, status) in zip(
        people, entries, strict=True
    ):
        guest = grouped.setdefault(who, Guest(name or key))
        guest.sources.append(
            GuestSource(provider, source_label(provider), url, listed, status)
        )
    guests = sorted(
        grouped.values(),
        key=lambda g: (g.verdict == "unlisted", g.name.casefold()),
    )
    return EventDetail(convention, views, guests, _conflicts(views))
