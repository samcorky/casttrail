from typing import TYPE_CHECKING, Protocol

import structlog
from sqlalchemy import select

from casttrail.discovery.models import JobRun
from casttrail.discovery.refresh import JOB, RefreshReport, RosterconRefresh

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from casttrail.providers.rostercon_client import PeopleIndex, RosterconFetcher

logger = structlog.get_logger(__name__)


class Refresher(Protocol):
    """Something that can run one refresh."""

    async def run(self) -> RefreshReport | None:
        """Run a refresh, or return ``None`` if one is already running."""
        ...


class RefreshJob:
    """A refresh with its own database session, for a timer or a button to call.

    The fetcher and people index are kept between runs, so Rostercon's spacing and the
    cached index carry over from one run to the next.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        fetcher: RosterconFetcher,
        index: PeopleIndex,
    ) -> None:
        """Create the job.

        Args:
            session_factory: Opens a database session for each run.
            fetcher: Fetches Rostercon pages politely.
            index: Rostercon's list of person pages.
        """
        self._session_factory = session_factory
        self._fetcher = fetcher
        self._index = index

    async def run(self) -> RefreshReport | None:
        """Run one refresh.

        Returns:
            What was done, or ``None`` if a refresh was already running.
        """
        async with self._session_factory() as session:
            return await RosterconRefresh(session, self._fetcher, self._index).run()


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
    """List the latest refresh runs, newest first.

    Args:
        session: The database session.
        limit: The most runs to return.

    Returns:
        The runs.
    """
    rows = await session.scalars(
        select(JobRun)
        .where(JobRun.job == JOB)
        .order_by(JobRun.started_at.desc())
        .limit(limit)
    )
    return list(rows)
