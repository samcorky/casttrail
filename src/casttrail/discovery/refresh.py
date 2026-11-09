import json
import time
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from casttrail.catalogue.models import Person
from casttrail.db import utc_now
from casttrail.discovery.models import JobRun
from casttrail.discovery.service import DiscoveryService
from casttrail.matching.service import MatchService
from casttrail.providers.base import ProviderError
from casttrail.providers.rostercon import (
    PROVIDER_ID,
    ListedEvent,
    match_people,
    parse_all_events_link,
    parse_event_page,
    parse_event_rows,
)
from casttrail.providers.rostercon_client import RosterconUnavailableError
from casttrail.watching.models import Watch

if TYPE_CHECKING:
    import uuid
    from collections.abc import Callable
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

    from casttrail.providers.rostercon import PersonPage
    from casttrail.providers.rostercon_client import PeopleIndex, RosterconFetcher

logger = structlog.get_logger(__name__)

JOB = "rostercon-refresh"
# A run still marked running after this long is taken to have died.
STALE_AFTER = timedelta(hours=2)
# Event pages fetched in one run. At 10 s apart this is about 10 minutes, and anything
# beyond it is picked up by the next run, soonest events first.
MAX_EVENT_PAGES = 60
# Events that began a day or two ago may still be running.
_GRACE = timedelta(days=2)


@dataclass
class RefreshReport:
    """What a refresh did. Stored with the run, so it holds counts only."""

    people_watched: int = 0
    people_matched: int = 0
    people_unmatched: int = 0
    people_ambiguous: int = 0
    people_failed: int = 0
    events_found: int = 0
    events_fetched: int = 0
    events_changed: int = 0
    events_unreadable: int = 0
    events_failed: int = 0
    events_deferred: int = 0
    matches_added: int = 0
    matches_removed: int = 0
    # Why the run stopped early, if it did.
    stopped: str | None = None


class RosterconRefresh:
    """Brings the stored conventions up to date for the people users watch.

    Only watched people are looked up on Rostercon: their page, their list of events,
    and the page of each upcoming event. A person is looked up only when exactly one
    Rostercon page has their name; no match and several matches are counted and left
    for a person to settle. Two refreshes never run at once.
    """

    def __init__(
        self,
        session: AsyncSession,
        fetcher: RosterconFetcher,
        index: PeopleIndex,
        *,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        """Create the refresh.

        Args:
            session: The database session to work in.
            fetcher: Fetches Rostercon pages politely.
            index: Rostercon's list of person pages.
            now: The clock, replaceable in tests.
        """
        self._session = session
        self._fetcher = fetcher
        self._index = index
        self._now = now

    async def run(self) -> RefreshReport | None:
        """Refresh once, unless a refresh is already running.

        Returns:
            What was done, or ``None`` if another refresh holds the lock. A run that
            Rostercon or an error cut short returns normally with ``stopped`` set and
            is recorded as failed.
        """
        job = await self._acquire()
        if job is None:
            logger.info("refresh_skipped", reason="already_running")
            return None
        # Read now: a rollback expires the object, and its id could not be reloaded.
        job_id = job.id
        structlog.contextvars.bind_contextvars(job_id=str(job_id))
        started = time.perf_counter()
        logger.info("refresh_started")
        report = RefreshReport()
        try:
            await self._refresh(report)
        except RosterconUnavailableError as error:
            report.stopped = "Rostercon asked us to wait"
            wait = error.retry_after
            logger.warning(
                "refresh_stopped",
                reason="cooldown",
                retry_after_s=wait.total_seconds() if wait else None,
            )
        except ProviderError as error:
            report.stopped = "Rostercon could not be read"
            # The message holds no address or secret by design, so it is safe to log.
            logger.warning(
                "refresh_stopped",
                reason="provider_error",
                error=str(error),
                cause=type(error.__cause__).__name__ if error.__cause__ else None,
            )
        except BaseException:
            await self._session.rollback()
            report.stopped = "The refresh crashed"
            logger.exception("refresh_crashed")
            await self._finish(job_id, report)
            raise
        await self._rematch(report)
        await self._finish(job_id, report)
        logger.info(
            "refresh_finished",
            duration_s=round(time.perf_counter() - started, 1),
            **asdict(report),
        )
        return report

    async def _rematch(self, report: RefreshReport) -> None:
        """Bring matches up to date with whatever was stored, even by a short run."""
        result = await MatchService(self._session, now=self._now).rematch()
        report.matches_added = result.added
        report.matches_removed = result.removed
        logger.info(
            "matches_updated",
            added=result.added,
            updated=result.updated,
            removed=result.removed,
        )

    async def _acquire(self) -> JobRun | None:
        now = self._now()
        await self._session.execute(
            update(JobRun)
            .where(
                JobRun.job == JOB,
                JobRun.status == "running",
                JobRun.started_at < now - STALE_AFTER,
            )
            .values(
                status="failed",
                finished_at=now,
                summary=json.dumps({"stopped": "Taken over after it stopped"}),
            )
        )
        job = JobRun(job=JOB, status="running", started_at=now)
        self._session.add(job)
        try:
            await self._session.commit()
        except IntegrityError:
            await self._session.rollback()
            return None
        return job

    async def _finish(self, job_id: uuid.UUID, report: RefreshReport) -> None:
        failed = report.stopped is not None
        await self._session.execute(
            update(JobRun)
            .where(JobRun.id == job_id)
            .values(
                status="failed" if failed else "succeeded",
                finished_at=self._now(),
                summary=json.dumps(asdict(report)),
            )
        )
        await self._session.commit()

    async def _refresh(self, report: RefreshReport) -> None:
        people = list(
            await self._session.execute(
                select(Person.id, Person.name)
                .join(Watch, Watch.person_id == Person.id)
                .distinct()
                .order_by(Person.name, Person.id)
            )
        )
        # Reading began a transaction, and with it SQLite's one write lock. Release it:
        # what follows is minutes of fetching, and must not hold the lock while it does.
        await self._session.commit()
        report.people_watched = len(people)
        logger.info("refresh_people", watched=len(people))
        if not people:
            return
        known = await self._index.people()
        events: dict[str, ListedEvent] = {}
        for _, name in people:
            await self._collect(name, known, events, report)
        await self._fetch_events(events, report)

    async def _collect(
        self,
        name: str,
        known: list[PersonPage],
        events: dict[str, ListedEvent],
        report: RefreshReport,
    ) -> None:
        matches = match_people(name, known)
        if not matches:
            report.people_unmatched += 1
            logger.info("person_unmatched", person=name)
            return
        if len(matches) > 1:
            report.people_ambiguous += 1
            logger.warning(
                "person_ambiguous", person=name, candidates=[m.slug for m in matches]
            )
            return
        try:
            page = await self._fetcher.get(matches[0].url)
            rows = parse_event_rows(page.text).events
            link = parse_all_events_link(page.text)
            if link is not None:
                rows += parse_event_rows((await self._fetcher.get(link)).text).events
        except RosterconUnavailableError:
            raise
        except ProviderError as error:
            report.people_failed += 1
            logger.warning("person_failed", person=name, error=str(error))
            return
        report.people_matched += 1
        logger.info(
            "person_matched", person=name, slug=matches[0].slug, events_listed=len(rows)
        )
        for event in rows:
            events.setdefault(event.source_key, event)

    async def _fetch_events(
        self, events: dict[str, ListedEvent], report: RefreshReport
    ) -> None:
        cutoff = self._now().date() - _GRACE
        report.events_found = len(events)
        wanted = sorted(
            (
                event
                for event in events.values()
                if event.start_date is None or event.start_date >= cutoff
            ),
            key=lambda event: (event.start_date or date.max, event.source_key),
        )
        report.events_deferred = max(0, len(wanted) - MAX_EVENT_PAGES)
        service = DiscoveryService(self._session, now=self._now)
        for event in wanted[:MAX_EVENT_PAGES]:
            try:
                page = await self._fetcher.get(event.source_url)
            except RosterconUnavailableError:
                raise
            except ProviderError as error:
                report.events_failed += 1
                logger.warning(
                    "event_fetch_failed", event_key=event.source_key, error=str(error)
                )
                continue
            report.events_fetched += 1
            parsed = parse_event_page(page.text, etag=page.etag)
            if parsed.convention is None:
                report.events_unreadable += 1
                # The page no longer parses: Rostercon may have changed its layout.
                logger.warning(
                    "event_unreadable", event_key=event.source_key, bytes=len(page.text)
                )
                continue
            result = await service.ingest(PROVIDER_ID, parsed.convention, parsed.guests)
            if result.changed:
                report.events_changed += 1
            logger.debug(
                "event_ingested",
                event_key=event.source_key,
                guests=len(parsed.guests.items),
                changed=result.changed,
                created=result.convention_created,
                merge_suggestions=result.merge_suggestions,
            )
