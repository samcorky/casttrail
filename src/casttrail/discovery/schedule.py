from typing import TYPE_CHECKING, Any, Protocol

import structlog
from sqlalchemy import select

from casttrail.discovery.comiconomicon_refresh import JOB as COMICONOMICON_JOB
from casttrail.discovery.models import JobRun
from casttrail.discovery.refresh import JOB as ROSTERCON_JOB, RosterconRefresh

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from casttrail.discovery.source_refresh import RefreshReport, SourceRefresh
    from casttrail.providers.polite import PoliteFetcher

logger = structlog.get_logger(__name__)

# The refresh jobs, by the name each run is stored under, and how a person sees them.
SOURCE_LABELS = {ROSTERCON_JOB: "Rostercon", COMICONOMICON_JOB: "Comiconomicon"}


class Refresher(Protocol):
    """Something that can run one refresh."""

    async def run(self) -> RefreshReport | None:
        """Run a refresh, or return ``None`` if one is already running."""
        ...


class RefreshJob:
    """A refresh of one source with its own database session, for a timer or a button.

    The fetcher is kept between runs, so the source's spacing and cooldown carry over
    from one run to the next.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        fetcher: PoliteFetcher,
        refresh: type[SourceRefresh[Any]] = RosterconRefresh,
    ) -> None:
        """Create the job.

        Args:
            session_factory: Opens a database session for each run.
            fetcher: Fetches the source's pages politely.
            refresh: The refresh to run, which must take this fetcher.
        """
        self._session_factory = session_factory
        self._fetcher = fetcher
        self.job = refresh.job
        self._refresh = refresh

    async def run(self) -> RefreshReport | None:
        """Run one refresh.

        Returns:
            What was done, or ``None`` if a refresh was already running.
        """
        async with self._session_factory() as session:
            return await self._refresh(session, self._fetcher).run()


class RefreshAll:
    """Refreshes every source in turn, so one button and one timer cover them all.

    The sources are separate sites with separate locks and cooldowns, so one asking us
    to wait, or failing, never stops the next. Stopping the app stops them all.
    """

    def __init__(self, *jobs: Refresher) -> None:
        """Create the combined refresh.

        Args:
            jobs: The refresh of each source, in the order to run them.
        """
        self.jobs = jobs

    async def run(self) -> RefreshReport | None:
        """Run every source's refresh.

        Returns:
            The first report there was (each source's own report is stored with its
            run), or ``None`` if every source was already being refreshed.

        Raises:
            Exception: The first failure, after the other sources have had their turn.
        """
        first: RefreshReport | None = None
        failure: Exception | None = None
        for job in self.jobs:
            try:
                report = await job.run()
            except Exception as error:
                # The refresh has already logged its own traceback.
                name = getattr(job, "job", type(job).__name__)
                logger.error("refresh_source_failed", job=name)
                failure = failure or error
                continue
            first = first or report
        if failure is not None:
            raise failure
        return first


async def run_in_background(job: Refresher) -> None:
    """Run a refresh nobody is waiting for, logging how it went.

    A crash is logged and swallowed, so it cannot kill the timer or a background task.

    Args:
        job: The refresh to run.
    """
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(task="refresh")
    try:
        await job.run()
    except Exception:
        # refresh_crashed has already been logged, with its traceback, by the refresh.
        logger.error("background_refresh_failed")
    finally:
        structlog.contextvars.clear_contextvars()


async def recent_runs(session: AsyncSession, limit: int = 10) -> list[JobRun]:
    """List the latest refresh runs of every source, newest first.

    Args:
        session: The database session.
        limit: The most runs to return.

    Returns:
        The runs.
    """
    rows = await session.scalars(
        select(JobRun)
        .where(JobRun.job.in_(SOURCE_LABELS))
        .order_by(JobRun.started_at.desc())
        .limit(limit)
    )
    return list(rows)
