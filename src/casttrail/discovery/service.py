import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import and_, delete, func, or_, select

from casttrail.db import utc_now
from casttrail.discovery.dedupe import EventFacts, judge, normalise_event_name
from casttrail.discovery.models import (
    Appearance,
    AppearanceSource,
    Convention,
    ConventionMerge,
    ConventionSource,
    SourceObservation,
)
from casttrail.identity.models import ExternalIdentity

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

    from casttrail.providers.base import ConventionInfo, ListingResult

logger = structlog.get_logger(__name__)

# Which source is trusted most for a convention's details, best first. A source that
# is not the best one listing a convention only fills in what is still unknown.
_PRIORITY = ("rostercon", "comiconomicon")

# Observations kept per page. Older ones are pruned on every ingest.
KEEP_OBSERVATIONS = 3

# Details that update a stored convention when a source states them. A value the source
# did not give (``None``) never clears what is already known.
_DETAIL_FIELDS = (
    "start_date",
    "end_date",
    "venue",
    "city",
    "region",
    "country",
    "organizer",
    "status",
)


@dataclass(frozen=True)
class IngestResult:
    """What one ingest changed."""

    convention_id: uuid.UUID
    convention_created: bool
    guests_added: int
    guests_unlisted: int
    # Whether what was parsed differs from the previous observation of this page.
    changed: bool
    # Pairs of conventions newly noted as possibly the same event, for a person.
    merge_suggestions: int = 0


def _record_view(source: ConventionSource, info: ConventionInfo) -> None:
    """Keep what this source itself says, whatever the merged convention shows."""
    source.seen_name = info.name
    source.seen_start = info.start_date
    source.seen_end = info.end_date
    source.seen_venue = info.venue


def _rank(provider: str) -> int:
    """Return how far down the trust order a provider is (0 is the most trusted)."""
    return _PRIORITY.index(provider) if provider in _PRIORITY else len(_PRIORITY)


def content_hash(convention: ConventionInfo, listing: ListingResult) -> str:
    """Fingerprint what a source said, ignoring the order of its guests.

    Args:
        convention: The convention as parsed.
        listing: The guest listing as parsed.

    Returns:
        A hex SHA-256 digest. The ``etag`` is left out: it is about the transfer, not
        the content.
    """
    payload = {
        "convention": convention.model_dump(mode="json"),
        "complete": listing.complete,
        "guests": sorted(
            (guest.model_dump(mode="json") for guest in listing.items),
            key=lambda guest: str(guest["source_key"]),
        ),
    }
    text = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class DiscoveryService:
    """Records what listing sources say about conventions and their guests.

    Ingesting the same thing twice changes nothing. Imported data is untrusted, so:
    a missing guest is never a cancellation, an incomplete listing never removes
    anyone, and a link between a guest and a person made by hand is never touched.
    """

    def __init__(
        self, session: AsyncSession, *, now: Callable[[], datetime] = utc_now
    ) -> None:
        """Create the service.

        Args:
            session: The database session to work in.
            now: The clock, replaceable in tests.
        """
        self._session = session
        self._now = now

    async def ingest(
        self, provider: str, convention: ConventionInfo, listing: ListingResult
    ) -> IngestResult:
        """Record one page's convention and guest list, all or nothing.

        Args:
            provider: The source the page came from.
            convention: The convention as the source describes it.
            listing: The guests the source lists for it.

        Returns:
            What changed.
        """
        try:
            result = await self._ingest(provider, convention, listing)
            await self._session.commit()
        except BaseException:
            await self._session.rollback()
            logger.exception(
                "ingest_failed", provider=provider, source_key=convention.source_key
            )
            raise
        logger.debug(
            "ingested",
            provider=provider,
            source_key=convention.source_key,
            convention_id=str(result.convention_id),
            created=result.convention_created,
            guests=len(listing.items),
            guests_added=result.guests_added,
            guests_unlisted=result.guests_unlisted,
            complete=listing.complete,
            changed=result.changed,
            merge_suggestions=result.merge_suggestions,
        )
        if result.merge_suggestions:
            logger.info(
                "merge_suggested",
                provider=provider,
                source_key=convention.source_key,
                suggestions=result.merge_suggestions,
            )
        return result

    async def _ingest(
        self, provider: str, convention: ConventionInfo, listing: ListingResult
    ) -> IngestResult:
        now = self._now()
        digest = content_hash(convention, listing)
        previous = await self._session.scalar(
            select(SourceObservation.content_hash)
            .where(
                SourceObservation.provider == provider,
                SourceObservation.source_key == convention.source_key,
            )
            .order_by(SourceObservation.fetched_at.desc())
            .limit(1)
        )
        stored, created, suggested = await self._upsert_convention(
            provider, convention, now
        )
        added, seen = await self._upsert_guests(provider, stored, listing, now)
        unlisted = await self._unlist_missing(provider, stored, listing, seen)
        self._session.add(
            SourceObservation(
                provider=provider,
                source_key=convention.source_key,
                content_hash=digest,
                complete=listing.complete,
                fetched_at=now,
            )
        )
        await self._session.flush()
        await self._prune(provider, convention.source_key)
        return IngestResult(
            convention_id=stored.id,
            convention_created=created,
            guests_added=added,
            guests_unlisted=unlisted,
            changed=previous != digest,
            merge_suggestions=suggested,
        )

    async def _upsert_convention(
        self, provider: str, info: ConventionInfo, now: datetime
    ) -> tuple[Convention, bool, int]:
        """Find or create the convention a page describes.

        Returns the convention, whether it was created, and how many possible
        duplicates were newly noted for a person to look at.
        """
        source = await self._session.scalar(
            select(ConventionSource).where(
                ConventionSource.provider == provider,
                ConventionSource.source_key == info.source_key,
            )
        )
        if source is not None:
            convention = await self._session.get_one(Convention, source.convention_id)
            source.source_url = info.source_url
            source.last_seen_at = now
            _record_view(source, info)
            await self._apply_details(convention, provider, info, now)
            return convention, False, 0
        twin, doubtful = await self._find_twins(provider, info)
        created = twin is None
        convention = twin or Convention(
            name=info.name,
            created_at=now,
            updated_at=now,
            **{field: getattr(info, field) for field in _DETAIL_FIELDS},
        )
        if created:
            self._session.add(convention)
            await self._session.flush()
        new_source = ConventionSource(
            convention_id=convention.id,
            provider=provider,
            source_key=info.source_key,
            source_url=info.source_url,
            first_seen_at=now,
            last_seen_at=now,
        )
        _record_view(new_source, info)
        self._session.add(new_source)
        await self._session.flush()
        if not created:
            await self._apply_details(convention, provider, info, now)
        suggested = 0
        for other, reason in doubtful:
            suggested += await self._suggest(convention, other, reason, now)
        return convention, created, suggested

    async def _apply_details(
        self,
        convention: Convention,
        provider: str,
        info: ConventionInfo,
        now: datetime,
    ) -> None:
        """Update a convention from one source's page.

        The best-ranked source listing the convention may change what is known; any
        other source only fills in what is still empty, so two sources never fight
        over a name or a date.
        """
        providers = list(
            await self._session.scalars(
                select(ConventionSource.provider).where(
                    ConventionSource.convention_id == convention.id
                )
            )
        )
        leads = _rank(provider) <= min(_rank(other) for other in providers)
        changed = False
        if leads and convention.name != info.name:
            convention.name = info.name
            changed = True
        for field in _DETAIL_FIELDS:
            value = getattr(info, field)
            current = getattr(convention, field)
            if value is None or current == value or (not leads and current is not None):
                continue
            setattr(convention, field, value)
            changed = True
        if changed:
            convention.updated_at = now

    async def _find_twins(
        self, provider: str, info: ConventionInfo
    ) -> tuple[Convention | None, list[tuple[Convention, str]]]:
        """Look for stored conventions this page might describe.

        Only conventions this provider has not already listed are considered. Exactly
        one strict match is the same event; anything weaker is returned, with why, for
        a person to decide.
        """
        listed_by_provider = select(ConventionSource.convention_id).where(
            ConventionSource.provider == provider
        )
        query = select(Convention).where(Convention.id.not_in(listed_by_provider))
        if info.start_date is not None:
            last = info.end_date or info.start_date
            query = query.where(
                or_(
                    Convention.start_date.is_(None),
                    and_(
                        Convention.start_date <= last,
                        func.coalesce(Convention.end_date, Convention.start_date)
                        >= info.start_date,
                    ),
                )
            )
        facts = EventFacts(info.name, info.start_date, info.end_date)
        wanted = normalise_event_name(info.name)
        same: list[Convention] = []
        maybe: list[tuple[Convention, str]] = []
        for candidate in await self._session.scalars(query):
            other = EventFacts(candidate.name, candidate.start_date, candidate.end_date)
            verdict = judge(facts, other)
            if verdict == "same":
                same.append(candidate)
            elif verdict == "maybe":
                exact = normalise_event_name(candidate.name) == wanted
                reason = (
                    "Same name, but the days do not match exactly."
                    if exact
                    else "Alike names on overlapping days."
                )
                maybe.append((candidate, reason))
        if len(same) == 1:
            return same[0], maybe
        ambiguous = [(c, "Several listings share this name and day.") for c in same]
        return None, ambiguous + maybe

    async def _suggest(
        self, one: Convention, other: Convention, reason: str, now: datetime
    ) -> int:
        """Note a possible duplicate once; a pair already decided is left alone."""
        first, second = sorted((one.id, other.id), key=str)
        known = await self._session.scalar(
            select(ConventionMerge.id).where(
                ConventionMerge.first_id == first, ConventionMerge.second_id == second
            )
        )
        if known is not None:
            return 0
        self._session.add(
            ConventionMerge(
                first_id=first,
                second_id=second,
                status="suggested",
                reason=reason,
                created_at=now,
            )
        )
        return 1

    async def _upsert_guests(
        self,
        provider: str,
        convention: Convention,
        listing: ListingResult,
        now: datetime,
    ) -> tuple[int, set[uuid.UUID]]:
        """Record each listed guest: how many were new, and which are listed."""
        keys = [guest.source_key for guest in listing.items]
        identities = {
            identity.external_id: identity
            for identity in await self._session.scalars(
                select(ExternalIdentity).where(
                    ExternalIdentity.provider == provider,
                    ExternalIdentity.external_id.in_(keys),
                )
            )
        }
        added = 0
        listed: set[uuid.UUID] = set()
        for guest in listing.items:
            identity = identities.get(guest.source_key)
            if identity is None:
                identity = ExternalIdentity(
                    provider=provider,
                    external_id=guest.source_key,
                    display_name=guest.name,
                )
                self._session.add(identity)
                await self._session.flush()
            elif identity.display_name != guest.name:
                # The name is the source's word. ``person_id`` is never touched here.
                identity.display_name = guest.name
            listed.add(identity.id)

            appearance = await self._session.scalar(
                select(Appearance).where(
                    Appearance.convention_id == convention.id,
                    Appearance.external_identity_id == identity.id,
                )
            )
            if appearance is None:
                appearance = Appearance(
                    convention_id=convention.id,
                    external_identity_id=identity.id,
                    # A link made by hand carries over to the guest's later events.
                    person_id=identity.person_id,
                    created_at=now,
                )
                self._session.add(appearance)
                await self._session.flush()
                added += 1

            line = await self._session.scalar(
                select(AppearanceSource).where(
                    AppearanceSource.appearance_id == appearance.id,
                    AppearanceSource.provider == provider,
                )
            )
            if line is None:
                self._session.add(
                    AppearanceSource(
                        appearance_id=appearance.id,
                        provider=provider,
                        source_url=guest.source_url,
                        listed=True,
                        status=guest.status,
                        first_seen_at=now,
                        last_seen_at=now,
                    )
                )
            else:
                line.source_url = guest.source_url
                line.listed = True
                line.last_seen_at = now
                if guest.status is not None:
                    line.status = guest.status
        return added, listed

    async def _unlist_missing(
        self,
        provider: str,
        convention: Convention,
        listing: ListingResult,
        listed: set[uuid.UUID],
    ) -> int:
        """Mark guests this source no longer lists, but only from a complete listing."""
        if not listing.complete:
            return 0
        lines = await self._session.scalars(
            select(AppearanceSource)
            .join(Appearance, Appearance.id == AppearanceSource.appearance_id)
            .where(
                Appearance.convention_id == convention.id,
                AppearanceSource.provider == provider,
                AppearanceSource.listed.is_(True),
                Appearance.external_identity_id.not_in(listed),
            )
        )
        count = 0
        for line in lines:
            line.listed = False
            count += 1
        return count

    async def _prune(self, provider: str, source_key: str) -> None:
        keep = (
            select(SourceObservation.id)
            .where(
                SourceObservation.provider == provider,
                SourceObservation.source_key == source_key,
            )
            .order_by(SourceObservation.fetched_at.desc(), SourceObservation.id)
            .limit(KEEP_OBSERVATIONS)
        )
        await self._session.execute(
            delete(SourceObservation).where(
                SourceObservation.provider == provider,
                SourceObservation.source_key == source_key,
                SourceObservation.id.not_in(keep),
            )
        )
