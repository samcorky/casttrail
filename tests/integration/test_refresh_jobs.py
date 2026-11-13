import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import httpx2
import pytest
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from casttrail.auth.service import AuthService
from casttrail.discovery.comiconomicon_refresh import (
    JOB as COMICONOMICON_JOB,
    ComiconomiconRefresh,
)
from casttrail.discovery.models import JobRun
from casttrail.discovery.refresh import JOB as ROSTERCON_JOB
from casttrail.discovery.schedule import RefreshAll, RefreshJob, recent_runs
from casttrail.discovery.source_refresh import RefreshReport
from casttrail.providers.base import PersonDetail
from casttrail.providers.comiconomicon_client import ComiconomiconFetcher
from casttrail.providers.polite import create_client
from casttrail.providers.rostercon_client import RosterconFetcher
from casttrail.watching.service import WatchService

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


class Scripted:
    """A refresh that does nothing but say what it was told to."""

    def __init__(
        self, name: str, outcome: RefreshReport | BaseException | None, calls: list[str]
    ) -> None:
        self.name = name
        self.outcome = outcome
        self.calls = calls

    async def run(self) -> RefreshReport | None:
        self.calls.append(self.name)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


# --- one run per source ---------------------------------------------------------------


async def test_a_refresh_job_can_run_the_comiconomicon_refresh(
    db_session: AsyncSession,
) -> None:
    def nothing(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(404)

    assert db_session.bind is not None
    factory = async_sessionmaker(db_session.bind, expire_on_commit=False)
    client = create_client(transport=httpx2.MockTransport(nothing))
    job = RefreshJob(factory, ComiconomiconFetcher(client), ComiconomiconRefresh)

    report = await job.run()

    assert report is not None
    jobs = list(await db_session.scalars(select(JobRun.job)))
    assert jobs == [COMICONOMICON_JOB]


async def test_a_refresh_job_runs_the_rostercon_refresh_by_default(
    db_session: AsyncSession,
) -> None:
    def nothing(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(404)

    assert db_session.bind is not None
    factory = async_sessionmaker(db_session.bind, expire_on_commit=False)
    client = create_client(transport=httpx2.MockTransport(nothing))

    await RefreshJob(factory, RosterconFetcher(client)).run()

    assert list(await db_session.scalars(select(JobRun.job))) == [ROSTERCON_JOB]


# --- every source -------------------------------------------------------------------


async def test_every_source_is_refreshed_in_turn() -> None:
    calls: list[str] = []
    first = RefreshReport(people_watched=1)
    both = RefreshAll(
        Scripted("rostercon", first, calls),
        Scripted("comiconomicon", RefreshReport(people_watched=2), calls),
    )

    report = await both.run()

    assert calls == ["rostercon", "comiconomicon"]
    # One answer is all a caller needs: the first source's. Each source's own report
    # is stored with its run.
    assert report is first


async def test_a_source_that_is_already_running_is_skipped_and_the_rest_still_run() -> (
    None
):
    calls: list[str] = []
    second = RefreshReport(people_watched=2)
    both = RefreshAll(
        Scripted("rostercon", None, calls), Scripted("comiconomicon", second, calls)
    )

    assert await both.run() is second
    assert calls == ["rostercon", "comiconomicon"]


async def test_nothing_is_returned_when_every_source_was_already_running() -> None:
    calls: list[str] = []
    both = RefreshAll(Scripted("a", None, calls), Scripted("b", None, calls))

    assert await both.run() is None
    assert calls == ["a", "b"]


async def test_a_crash_in_one_source_does_not_stop_the_next_and_is_raised_after() -> (
    None
):
    calls: list[str] = []
    both = RefreshAll(
        Scripted("rostercon", RuntimeError("boom"), calls),
        Scripted("comiconomicon", RefreshReport(), calls),
    )

    with (
        structlog.testing.capture_logs() as logs,
        pytest.raises(RuntimeError, match="boom"),
    ):
        await both.run()

    assert calls == ["rostercon", "comiconomicon"]
    assert [e["event"] for e in logs] == ["refresh_source_failed"]
    assert logs[0]["job"] == "Scripted"


async def test_stopping_the_app_stops_the_rest_straight_away() -> None:
    import asyncio

    calls: list[str] = []
    both = RefreshAll(
        Scripted("rostercon", asyncio.CancelledError(), calls),
        Scripted("comiconomicon", RefreshReport(), calls),
    )

    with pytest.raises(asyncio.CancelledError):
        await both.run()

    assert calls == ["rostercon"]


async def test_the_first_crash_is_the_one_raised() -> None:
    calls: list[str] = []
    both = RefreshAll(
        Scripted("a", RuntimeError("first"), calls),
        Scripted("b", RuntimeError("second"), calls),
    )

    with pytest.raises(RuntimeError, match="first"):
        await both.run()

    assert calls == ["a", "b"]


# --- the list of runs -----------------------------------------------------------------


def _run(job: str, day: int) -> JobRun:
    return JobRun(
        job=job,
        status="succeeded",
        started_at=datetime(2026, 11, day, 4, 0, tzinfo=UTC),
    )


async def test_recent_runs_include_every_source_newest_first(
    db_session: AsyncSession,
) -> None:
    db_session.add_all(
        [
            _run(ROSTERCON_JOB, 5),
            _run(COMICONOMICON_JOB, 6),
            _run(ROSTERCON_JOB, 7),
            _run("some-other-job", 8),
        ]
    )
    await db_session.commit()

    runs = await recent_runs(db_session)

    assert [(r.job, r.started_at.day) for r in runs] == [
        (ROSTERCON_JOB, 7),
        (COMICONOMICON_JOB, 6),
        (ROSTERCON_JOB, 5),
    ]


# --- one source failing does not stop the other ---------------------------------------


async def _both_sources(
    db_session: AsyncSession,
    rostercon: httpx2.MockTransport,
) -> RefreshAll:
    """The real combined refresh; the watched person is unknown on Comiconomicon."""
    auth = AuthService(db_session, session_ttl=timedelta(hours=1))
    user = await auth.create_user(
        email="a@example.com", display_name="A", password="correct horse battery"
    )
    await WatchService(db_session).watch_person(
        user.id, PersonDetail(tmdb_id=1, name="Zed Nobody")
    )
    assert db_session.bind is not None
    factory = async_sessionmaker(db_session.bind, expire_on_commit=False)

    def empty_search(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, text="[]")

    comiconomicon = create_client(transport=httpx2.MockTransport(empty_search))
    return RefreshAll(
        RefreshJob(
            factory,
            RosterconFetcher(create_client(transport=rostercon), min_interval=0),
        ),
        RefreshJob(
            factory,
            ComiconomiconFetcher(comiconomicon, min_interval=0),
            ComiconomiconRefresh,
        ),
    )


async def _runs(db_session: AsyncSession) -> dict[str, tuple[str, dict[str, object]]]:
    rows = await db_session.execute(select(JobRun.job, JobRun.status, JobRun.summary))
    return {job: (status, json.loads(summary or "{}")) for job, status, summary in rows}


async def test_when_rostercon_asks_us_to_wait_comiconomicon_still_runs(
    db_session: AsyncSession,
) -> None:
    def refuse(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(503)

    both = await _both_sources(db_session, httpx2.MockTransport(refuse))

    report = await both.run()

    assert report is not None
    runs = await _runs(db_session)
    status, summary = runs[ROSTERCON_JOB]
    assert (status, summary["stopped"]) == ("failed", "Rostercon asked us to wait")
    status, summary = runs[COMICONOMICON_JOB]
    assert (status, summary["stopped"], summary["people_unmatched"]) == (
        "succeeded",
        None,
        1,
    )


async def test_when_rostercon_crashes_comiconomicon_still_runs_and_the_crash_is_raised(
    db_session: AsyncSession,
) -> None:
    def explode(request: httpx2.Request) -> httpx2.Response:
        raise RuntimeError("boom")

    both = await _both_sources(db_session, httpx2.MockTransport(explode))

    with pytest.raises(RuntimeError, match="boom"):
        await both.run()

    runs = await _runs(db_session)
    status, summary = runs[ROSTERCON_JOB]
    assert (status, summary["stopped"]) == ("failed", "The refresh crashed")
    status, summary = runs[COMICONOMICON_JOB]
    assert (status, summary["people_unmatched"]) == ("succeeded", 1)


async def test_a_running_rostercon_refresh_does_not_block_comiconomicon(
    db_session: AsyncSession,
) -> None:
    def refuse(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(503)

    both = await _both_sources(db_session, httpx2.MockTransport(refuse))
    db_session.add(
        JobRun(
            job=ROSTERCON_JOB,
            status="running",
            started_at=datetime.now(UTC),
        )
    )
    await db_session.commit()

    report = await both.run()

    # Rostercon was turned away, Comiconomicon ran, and its report is the one returned.
    assert report is not None
    runs = await _runs(db_session)
    assert runs[ROSTERCON_JOB][0] == "running"
    assert runs[COMICONOMICON_JOB][0] == "succeeded"
