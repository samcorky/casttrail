from datetime import timedelta
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import select

from casttrail.db import utc_now
from casttrail.discovery.models import SourceLookup
from casttrail.providers.comiconomicon import PROVIDER_ID
from casttrail.providers.comiconomicon_client import find_guests

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from casttrail.providers.comiconomicon_client import ComiconomiconFetcher

logger = structlog.get_logger(__name__)

# How long each answer is trusted before the site is asked again. A guest's id does not
# change, so a find is kept for a long time. A guest the site has not listed yet may be
# added once their appearances are announced, so that is asked again soon. Several
# guests under one name is asked again less often, because only a change on the site
# (or a person's decision) is likely to settle it.
FRESH_FOR = {
    "found": timedelta(days=180),
    "not_found": timedelta(days=7),
    "ambiguous": timedelta(days=30),
}


def is_fresh(entry: SourceLookup) -> bool:
    """Say whether a remembered answer is still young enough to trust.

    Args:
        entry: A stored lookup.

    Returns:
        True if the site need not be asked again yet.
    """
    return utc_now() - entry.looked_up_at < FRESH_FOR[entry.outcome]


class GuestLookup:
    """Turns a watched person's name into their Comiconomicon guest, and remembers it.

    The site's search is asked once per person and then only again when the stored
    answer has gone stale. Only an exact whole-name match is taken: no match, and
    several matches, are stored as such and never guessed at.
    """

    def __init__(self, session: AsyncSession, fetcher: ComiconomiconFetcher) -> None:
        """Create the lookup.

        Args:
            session: The database session. It is committed before the site is asked,
                so the write lock is never held while waiting on it.
            fetcher: Fetches Comiconomicon pages politely.
        """
        self._session = session
        self._fetcher = fetcher

    async def find(self, person_id: uuid.UUID, name: str) -> SourceLookup:
        """Find a person's Comiconomicon guest, from memory if the answer is fresh.

        Args:
            person_id: The watched person.
            name: The person's name as TMDB gives it.

        Returns:
            The stored answer: ``found`` with the guest's key and address,
            ``not_found``, or ``ambiguous``.

        Raises:
            ProviderError: The site could not be read. Nothing is stored, and an older
                answer for this person is kept.
        """
        entry = await self._stored(person_id)
        if entry is not None and is_fresh(entry):
            self._log(person_id, entry, cached=True)
            return entry
        # Release the write lock before waiting on the site.
        await self._session.commit()
        guests = await find_guests(self._fetcher, name)
        entry = await self._stored(person_id) or SourceLookup(
            provider=PROVIDER_ID, person_id=person_id
        )
        if len(guests) == 1:
            entry.outcome = "found"
            entry.source_key = guests[0].source_key
            entry.source_url = guests[0].source_url
        else:
            entry.outcome = "ambiguous" if guests else "not_found"
            entry.source_key = entry.source_url = None
        entry.looked_up_at = utc_now()
        self._session.add(entry)
        await self._session.commit()
        self._log(person_id, entry, cached=False)
        return entry

    async def _stored(self, person_id: uuid.UUID) -> SourceLookup | None:
        return await self._session.scalar(
            select(SourceLookup).where(
                SourceLookup.provider == PROVIDER_ID,
                SourceLookup.person_id == person_id,
            )
        )

    @staticmethod
    def _log(person_id: uuid.UUID, entry: SourceLookup, *, cached: bool) -> None:
        logger.info(
            "guest_lookup",
            source=PROVIDER_ID,
            person_id=str(person_id),
            outcome=entry.outcome,
            source_key=entry.source_key,
            cached=cached,
        )
