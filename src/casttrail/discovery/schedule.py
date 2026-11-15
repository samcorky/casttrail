import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol

import structlog
from sqlalchemy import func, select

from casttrail.discovery.comiconomicon_refresh import JOB as COMICONOMICON_JOB
from casttrail.discovery.models import JobRun
from casttrail.discovery.refresh import JOB as ROSTERCON_JOB, RosterconRefresh
from casttrail.discovery.source_refresh import INTERRUPTED

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
    """Refreshes every source side by side, so one button and one timer cover them all.

    The sources are separate sites, each with its own fetcher (so its own spacing and
    cooldown), its own lock and its own database session. Running them together asks
    neither site any faster; a run just takes as long as the slowest source instead of
    all of them added up. One asking us to wait, or failing, never stops the others.
    Stopping the app stops them all.
    """

    def __init__(self, *jobs: Refresher) -> None:
        """Create the combined refresh.

        Args:
            jobs: The refresh of each source, in the order to run them.
        """
        self.jobs = jobs

    async def run(self) -> RefreshReport | None:
        """Run every source's refresh at the same time, and wait for them all.

        Returns:
            The first report there was, in the order the sources were given (each
            source's own report is stored with its run), or ``None`` if every source
            was already being refreshed.

        Raises:
            Exception: The first failure, in that order, once every source is done.
            asyncio.CancelledError: The app is stopping; every source is stopped.
        """
        # A failure is handed back rather than raised, so it does not cancel the
        # others. Cancelling this run (the app stopping) still cancels every source.
        outcomes = await asyncio.gather(
            *(job.run() for job in self.jobs), return_exceptions=True
        )
        first: RefreshReport | None = None
        failure: Exception | None = None
        for job, outcome in zip(self.jobs, outcomes, strict=True):
            if isinstance(outcome, Exception):
                # The refresh has already logged its own traceback.
                name = getattr(job, "job", type(job).__name__)
                logger.error("refresh_source_failed", job=name)
                failure = failure or outcome
            elif isinstance(outcome, BaseException):
                # Not an error: a source was cancelled (the app is stopping) or similar.
                raise outcome
            else:
                first = first or outcome
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


def _last_due(hour: int) -> datetime:
    """When the daily refresh was last due: today at ``hour`` if that has passed."""
    # The timer counts in the server's local time, so this does too.
    now = datetime.now().astimezone()
    due = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    return due if due <= now else due - timedelta(days=1)


async def refresh_is_due(session: AsyncSession, hour: int) -> bool:
    """Whether the daily refresh was missed, so startup should run it now.

    It was missed if no refresh has begun since it was last due (the app was off, or
    the PC asleep). A run cut short by the app stopping does not count, so it is tried
    again; a run that died without saying why does, so an app that keeps crashing does
    not keep asking the sources.

    Args:
        session: The database session.
        hour: The hour the refresh runs each day, in the server's local time.

    Returns:
        ``True`` if a refresh should run now.
    """
    since = _last_due(hour).astimezone(UTC)
    ran = await session.scalar(
        select(JobRun.id)
        .where(
            JobRun.job.in_(SOURCE_LABELS),
            JobRun.started_at >= since,
            # NULL (no summary yet) counts as distinct, so a run that died counts.
            func.json_extract(JobRun.summary, "$.stopped").is_distinct_from(
                INTERRUPTED
            ),
        )
        .limit(1)
    )
    return ran is None


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
