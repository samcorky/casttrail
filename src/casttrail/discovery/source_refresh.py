import asyncio
import json
import time
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, ClassVar, Protocol

import structlog
from sqlalchemy import and_, select, update
from sqlalchemy.exc import IntegrityError

from casttrail.catalogue.models import Person
from casttrail.db import utc_now
from casttrail.discovery.models import JobRun, RefreshProgress
from casttrail.discovery.service import DiscoveryService
from casttrail.matching.service import MatchService
from casttrail.providers.base import ConventionInfo, ListingResult, ProviderError
from casttrail.watching.models import Watch

if TYPE_CHECKING:
    import uuid
    from collections.abc import Callable, Mapping

    from sqlalchemy.ext.asyncio import AsyncSession

    from casttrail.providers.polite import PoliteFetcher

logger = structlog.get_logger(__name__)

# A run still marked running after this long is taken to have died.
STALE_AFTER = timedelta(hours=2)
# Events that began a day or two ago may still be running.
_GRACE = timedelta(days=2)


@dataclass
class RefreshReport:
    """What a refresh did. Stored with the run, so it holds counts only."""

    people_watched: int = 0
    # People finished this run, and people left for the next run by the request cap.
    people_done: int = 0
    people_deferred: int = 0
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


class ListedEventRef(Protocol):
    """An event a source lists: enough to decide whether and where to fetch it."""

    @property
    def source_key(self) -> str:
        """The source's own id for the event."""
        ...

    @property
    def source_url(self) -> str:
        """The event page's address."""
        ...

    @property
    def start_date(self) -> date | None:
        """The first day, if the source says."""
        ...


class ParsedEventRef(Protocol):
    """What reading one event page gave."""

    @property
    def convention(self) -> ConventionInfo | None:
        """The event, or ``None`` if the page held no readable event."""
        ...

    @property
    def guests(self) -> ListingResult:
        """The guests the page lists."""
        ...


class SourceRefresh[F: PoliteFetcher](ABC):
    """Brings the stored conventions up to date from one source.

    The part every source shares lives here: taking the run's lock so two never run at
    once, recording how the run went, stopping politely when the source asks us to wait,
    fetching and storing the event pages, and bringing matches up to date afterwards.
    A source says how to find one watched person's events (``_events_for``) and how to
    read one event page (``_parse_event``).

    People are taken one at a time, those never done first and then those done longest
    ago, and each is recorded as done once their events are stored. So a run cut short
    (a refusal, a restart, the request cap) keeps what it did, and the next run carries
    on from there. The run's counts are saved as it goes, for the Refresh page.
    """

    #: The run's name in ``job_runs``, and so the name of its lock.
    job: ClassVar[str]
    #: The provider id stored with conventions this source supplies.
    provider: ClassVar[str]
    #: The source's name for messages.
    label: ClassVar[str]
    #: Raised by the source's fetcher when the source tells us to wait.
    unavailable_error: ClassVar[type[ProviderError]]
    #: The most requests one run sends. Whoever is left waits for the next run, and is
    #: taken first then.
    max_requests: ClassVar[int]

    def __init__(
        self,
        session: AsyncSession,
        fetcher: F,
        *,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        """Create the refresh.

        Args:
            session: The database session to work in.
            fetcher: Fetches this source's pages politely.
            now: The clock, replaceable in tests.
        """
        self._session = session
        self._fetcher: F = fetcher
        self._now = now
        self._job_id: uuid.UUID | None = None
        self._first_request = 0
        # Event keys seen and event pages asked for in this run, across all people.
        self._found: set[str] = set()
        self._asked: set[str] = set()

    @abstractmethod
    async def _events_for(
        self, person_id: uuid.UUID, name: str, report: RefreshReport
    ) -> Mapping[str, ListedEventRef]:
        """Find the events the source lists for one watched person.

        A person who cannot be placed gives nothing. A failure worth trying again soon
        is counted in ``people_failed``, which keeps the person first in line.
        """

    @abstractmethod
    def _parse_event(self, text: str, etag: str | None) -> ParsedEventRef:
        """Read one event page."""

    async def run(self) -> RefreshReport | None:
        """Refresh once, unless a refresh of this source is already running.

        Returns:
            What was done, or ``None`` if another refresh holds the lock. A run that
            the source or an error cut short returns normally with ``stopped`` set and
            is recorded as failed.
        """
        job = await self._acquire()
        if job is None:
            logger.info(
                "refresh_skipped", source=self.provider, reason="already_running"
            )
            return None
        # Read now: a rollback expires the object, and its id could not be reloaded.
        job_id = job.id
        self._job_id = job_id
        self._first_request = self._fetcher.requests_sent
        self._found.clear()
        self._asked.clear()
        structlog.contextvars.bind_contextvars(job_id=str(job_id))
        started = time.perf_counter()
        logger.info("refresh_started", source=self.provider)
        report = RefreshReport()
        try:
            await self._refresh(report)
        except self.unavailable_error as error:
            report.stopped = f"{self.label} asked us to wait"
            wait = error.retry_after
            logger.warning(
                "refresh_stopped",
                source=self.provider,
                reason="cooldown",
                retry_after_s=wait.total_seconds() if wait else None,
            )
        except asyncio.CancelledError:
            # The app is stopping (a restart, or a new image): not a fault in the run.
            await self._session.rollback()
            report.stopped = "Interrupted: the app stopped"
            logger.warning("refresh_interrupted", source=self.provider)
            await self._finish(job_id, report)
            raise
        except BaseException:
            await self._session.rollback()
            report.stopped = "The refresh crashed"
            logger.exception("refresh_crashed", source=self.provider)
            await self._finish(job_id, report)
            raise
        await self._rematch(report)
        await self._finish(job_id, report)
        logger.info(
            "refresh_finished",
            source=self.provider,
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
                JobRun.job == self.job,
                JobRun.status == "running",
                JobRun.started_at < now - STALE_AFTER,
            )
            .values(
                status="failed",
                finished_at=now,
                summary=json.dumps({"stopped": "Taken over after it stopped"}),
            )
        )
        job = JobRun(job=self.job, status="running", started_at=now)
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
        """Refresh each watched person in turn until done or the request cap is hit."""
        people = await self._watched_people(report)
        for index, (person_id, name) in enumerate(people):
            if self._spent():
                self._defer(len(people) - index, report)
                return
            failures = report.people_failed
            events = await self._events_for(person_id, name, report)
            complete = await self._fetch_events(events, report)
            # The first person had the whole cap, so they count as done even if their
            # events did not all fit; anyone else waits and goes first next time.
            if not complete and index > 0:
                self._defer(len(people) - index, report)
                return
            if report.people_failed == failures:
                await self._session.merge(
                    RefreshProgress(
                        provider=self.provider,
                        person_id=person_id,
                        refreshed_at=self._now(),
                    )
                )
                report.people_done += 1
            await self._save_progress(report)

    def _spent(self) -> bool:
        """Whether this run has sent all the requests it may."""
        return self._fetcher.requests_sent - self._first_request >= self.max_requests

    def _defer(self, left: int, report: RefreshReport) -> None:
        report.people_deferred = left
        logger.info(
            "refresh_capped",
            source=self.provider,
            max_requests=self.max_requests,
            people_deferred=left,
        )

    async def _save_progress(self, report: RefreshReport) -> None:
        """Store the counts so far on the running run, and commit."""
        await self._session.execute(
            update(JobRun)
            .where(JobRun.id == self._job_id)
            .values(summary=json.dumps(asdict(report)))
        )
        await self._session.commit()

    async def _watched_people(
        self, report: RefreshReport
    ) -> list[tuple[uuid.UUID, str]]:
        """List the people anyone watches, least recently done first.

        People this source has never finished come first, then the ones it finished
        longest ago. The database is released before any fetching.
        """
        rows = await self._session.execute(
            select(Person.id, Person.name)
            .where(Person.id.in_(select(Watch.person_id)))
            .outerjoin(
                RefreshProgress,
                and_(
                    RefreshProgress.person_id == Person.id,
                    RefreshProgress.provider == self.provider,
                ),
            )
            .order_by(
                RefreshProgress.refreshed_at.is_not(None),
                RefreshProgress.refreshed_at,
                Person.name,
                Person.id,
            )
        )
        people = [(person_id, name) for person_id, name in rows]
        # Reading began a transaction, and with it SQLite's one write lock. Release it:
        # what follows is minutes of fetching, and must not hold the lock while it does.
        await self._session.commit()
        report.people_watched = len(people)
        logger.info("refresh_people", source=self.provider, watched=len(people))
        return people

    async def _fetch_events(
        self, events: Mapping[str, ListedEventRef], report: RefreshReport
    ) -> bool:
        """Fetch and store one person's events still to come, soonest first.

        An event already asked for in this run is not asked for again.

        Returns:
            ``True`` if every one was asked for, ``False`` if the request cap came
            first.
        """
        cutoff = self._now().date() - _GRACE
        self._found.update(events)
        report.events_found = len(self._found)
        wanted = sorted(
            (
                event
                for key, event in events.items()
                if key not in self._asked
                and (event.start_date is None or event.start_date >= cutoff)
            ),
            key=lambda event: (event.start_date or date.max, event.source_key),
        )
        service = DiscoveryService(self._session, now=self._now)
        for index, event in enumerate(wanted):
            if self._spent():
                report.events_deferred += len(wanted) - index
                return False
            self._asked.add(event.source_key)
            await self._fetch_event(service, event, report)
            await self._save_progress(report)
        return True

    async def _fetch_event(
        self, service: DiscoveryService, event: ListedEventRef, report: RefreshReport
    ) -> None:
        """Fetch and store one event page, counting what happened."""
        try:
            page = await self._fetcher.get(event.source_url)
        except self.unavailable_error:
            raise
        except ProviderError as error:
            report.events_failed += 1
            logger.warning(
                "event_fetch_failed",
                source=self.provider,
                event_key=event.source_key,
                error=str(error),
            )
            return
        report.events_fetched += 1
        parsed = self._parse_event(page.text, page.etag)
        if parsed.convention is None:
            report.events_unreadable += 1
            # The page no longer parses: the source may have changed its layout.
            logger.warning(
                "event_unreadable",
                source=self.provider,
                event_key=event.source_key,
                bytes=len(page.text),
            )
            return
        result = await service.ingest(self.provider, parsed.convention, parsed.guests)
        if result.changed:
            report.events_changed += 1
        logger.debug(
            "event_ingested",
            source=self.provider,
            event_key=event.source_key,
            guests=len(parsed.guests.items),
            changed=result.changed,
            created=result.convention_created,
            merge_suggestions=result.merge_suggestions,
        )
