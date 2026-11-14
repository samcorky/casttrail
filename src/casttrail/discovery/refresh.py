from typing import TYPE_CHECKING

import structlog

from casttrail.discovery.links import ROSTERCON, KnownPage, SourceLinks
from casttrail.discovery.source_refresh import (
    STALE_AFTER,
    ParsedEventRef,
    RefreshReport,
    SourceRefresh,
)
from casttrail.providers.base import LinkTarget, ProviderError
from casttrail.providers.rostercon import (
    PROVIDER_ID,
    ListedEvent,
    PersonPage,
    parse_all_events_link,
    parse_event_page,
    parse_event_rows,
)
from casttrail.providers.rostercon_client import (
    FoundPerson,
    RosterconFetcher,
    RosterconNotFoundError,
    RosterconUnavailableError,
    find_people,
)

if TYPE_CHECKING:
    import uuid

__all__ = [
    "JOB",
    "STALE_AFTER",
    "RefreshReport",
    "RosterconRefresh",
]

logger = structlog.get_logger(__name__)

JOB = "rostercon-refresh"


class _PageMoved(Exception):  # noqa: N818  (a signal inside this module, not an error)
    """A page found by name earlier is gone, so the name must be looked up again."""


class RosterconRefresh(SourceRefresh[RosterconFetcher]):
    """Brings the stored conventions up to date for the people users watch.

    Only watched people are looked up on Rostercon: their page, their list of events,
    and the page of each upcoming event. A person is looked up only when exactly one
    Rostercon page has their name; no match and several matches are counted and left
    for a person to settle. Two refreshes never run at once.
    """

    job = JOB
    provider = PROVIDER_ID
    label = "Rostercon"
    unavailable_error = RosterconUnavailableError
    # At 75 s apart this is about 90 minutes, inside the 2 hours after which a run is
    # taken to have died.
    max_requests = 70

    def _parse_event(self, text: str, etag: str | None) -> ParsedEventRef:
        return parse_event_page(text, etag=etag)

    async def _find(
        self, person_id: uuid.UUID, name: str, report: RefreshReport
    ) -> FoundPerson | None:
        """Get the person's page: one already known, else the one their name has.

        A page set by hand is used as it is and never guessed around. A page found by
        name earlier is asked for directly, which is one request instead of the four a
        lookup costs; only if it has gone is the name looked up again. What a name
        lookup finds is remembered, so the admin can see who could not be placed.
        """
        links = SourceLinks(self._session, ROSTERCON)
        known = await links.known(person_id)
        # Reading began a transaction; release the write lock before the fetching.
        await self._session.commit()
        if known is not None:
            try:
                return await self._use_known(name, known, report)
            except _PageMoved:
                logger.info("person_page_gone", person=name, slug=known.page.key)
        return await self._find_by_name(links, person_id, name, report)

    async def _use_known(
        self, name: str, known: KnownPage, report: RefreshReport
    ) -> FoundPerson | None:
        """Fetch a page already known. Raises ``_PageMoved`` if it is gone."""
        try:
            async with self._step("person_page", report):
                page = await self._fetcher.get(known.page.url)
        except RosterconUnavailableError:
            raise
        except ProviderError as error:
            if isinstance(error, RosterconNotFoundError) and not known.manual:
                raise _PageMoved from error
            report.people_failed += 1
            logger.warning(
                "person_link_failed",
                person=name,
                slug=known.page.key,
                manual=known.manual,
                error=str(error),
            )
            return None
        logger.debug("person_page_reused", person=name, slug=known.page.key)
        found = PersonPage(slug=known.page.key, url=known.page.url)
        return FoundPerson(found, page.text)

    async def _find_by_name(
        self,
        links: SourceLinks,
        person_id: uuid.UUID,
        name: str,
        report: RefreshReport,
    ) -> FoundPerson | None:
        try:
            async with self._step("lookup", report):
                matches = await find_people(self._fetcher, name)
        except RosterconUnavailableError:
            raise
        except ProviderError as error:
            report.people_failed += 1
            logger.warning("person_failed", person=name, error=str(error))
            return None
        if not matches:
            report.people_unmatched += 1
            logger.info("person_unmatched", person=name)
            await links.remember(person_id, "not_found")
            return None
        if len(matches) > 1:
            report.people_ambiguous += 1
            slugs = [m.page.slug for m in matches]
            logger.warning("person_ambiguous", person=name, candidates=slugs)
            await links.remember(person_id, "ambiguous")
            return None
        page = matches[0].page
        await links.remember(person_id, "found", LinkTarget(page.slug, page.url))
        return matches[0]

    async def _events_for(
        self, person_id: uuid.UUID, name: str, report: RefreshReport
    ) -> dict[str, ListedEvent]:
        found = await self._find(person_id, name, report)
        if found is None:
            return {}
        try:
            rows = parse_event_rows(found.text).events
            link = parse_all_events_link(found.text)
            if link is not None:
                async with self._step("event_list", report):
                    listing = await self._fetcher.get(link)
                rows += parse_event_rows(listing.text).events
        except RosterconUnavailableError:
            raise
        except ProviderError as error:
            report.people_failed += 1
            logger.warning("person_failed", person=name, error=str(error))
            return {}
        report.people_matched += 1
        slug = found.page.slug
        logger.info("person_matched", person=name, slug=slug, events_listed=len(rows))
        events: dict[str, ListedEvent] = {}
        for event in rows:
            events.setdefault(event.source_key, event)
        return events
