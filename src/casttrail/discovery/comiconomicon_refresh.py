from typing import TYPE_CHECKING

import structlog

from casttrail.discovery.lookup import GuestLookup
from casttrail.discovery.source_refresh import (
    ParsedEventRef,
    RefreshReport,
    SourceRefresh,
)
from casttrail.providers.base import ProviderError
from casttrail.providers.comiconomicon import (
    PROVIDER_ID,
    ListedEvent,
    parse_event_page,
    parse_guest_page,
)
from casttrail.providers.comiconomicon_client import (
    ComiconomiconFetcher,
    ComiconomiconNotFoundError,
    ComiconomiconUnavailableError,
)

if TYPE_CHECKING:
    import uuid

    from casttrail.discovery.models import SourceLookup

logger = structlog.get_logger(__name__)

JOB = "comiconomicon-refresh"


class ComiconomiconRefresh(SourceRefresh[ComiconomiconFetcher]):
    """Brings the stored conventions up to date from Comiconomicon.

    Only watched people are looked up: each is turned into a Comiconomicon guest by the
    site's own search (once, then remembered), the guest's page lists their events, and
    the page of each upcoming event is fetched and stored. A person is followed only
    when exactly one guest has their whole name; no match and several matches are
    counted and left for a person to settle. Two refreshes of this source never run at
    once, and it does not wait for or block a Rostercon refresh.
    """

    job = JOB
    provider = PROVIDER_ID
    label = "Comiconomicon"
    unavailable_error = ComiconomiconUnavailableError

    def _parse_event(self, text: str, etag: str | None) -> ParsedEventRef:
        return parse_event_page(text, etag=etag)

    async def _refresh(self, report: RefreshReport) -> None:
        people = await self._watched_people(report)
        if not people:
            return
        lookup = GuestLookup(self._session, self._fetcher)
        events: dict[str, ListedEvent] = {}
        for person_id, name in people:
            await self._collect(lookup, person_id, name, events, report)
        await self._fetch_events(events, report)

    async def _collect(
        self,
        lookup: GuestLookup,
        person_id: uuid.UUID,
        name: str,
        events: dict[str, ListedEvent],
        report: RefreshReport,
    ) -> None:
        guest = await self._guest_for(lookup, person_id, name, report)
        if guest is None:
            return
        assert guest.source_url is not None
        try:
            page = await self._fetcher.get(guest.source_url)
        except ComiconomiconUnavailableError:
            raise
        except ProviderError as error:
            report.people_failed += 1
            logger.warning(
                "person_page_failed",
                source=PROVIDER_ID,
                person=name,
                manual=guest.manual,
                error=str(error),
            )
            if isinstance(error, ComiconomiconNotFoundError):
                # A guest page that was found by search and is now gone: search again
                # next time. A page set by hand is kept, so it can be fixed.
                await lookup.forget(person_id)
            return
        listed = parse_guest_page(page.text).events
        report.people_matched += 1
        logger.info(
            "person_matched",
            source=PROVIDER_ID,
            person=name,
            guest=guest.source_key,
            events_listed=len(listed),
        )
        for event in listed:
            events.setdefault(event.source_key, event)

    async def _guest_for(
        self,
        lookup: GuestLookup,
        person_id: uuid.UUID,
        name: str,
        report: RefreshReport,
    ) -> SourceLookup | None:
        """Find the person's guest; count and log it unless exactly one is theirs."""
        try:
            guest = await lookup.find(person_id, name)
        except ComiconomiconUnavailableError:
            raise
        except ProviderError as error:
            report.people_failed += 1
            logger.warning(
                "person_failed", source=PROVIDER_ID, person=name, error=str(error)
            )
            return None
        # Reading the remembered answer began a transaction; release the write lock
        # before the fetching.
        await self._session.commit()
        if guest.outcome == "not_found":
            report.people_unmatched += 1
            logger.info("person_unmatched", source=PROVIDER_ID, person=name)
            return None
        if guest.outcome == "ambiguous":
            report.people_ambiguous += 1
            logger.warning("person_ambiguous", source=PROVIDER_ID, person=name)
            return None
        return guest
