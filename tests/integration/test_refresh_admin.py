import asyncio
import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import httpx2
import pytest
import structlog
import structlog.testing
from alembic import command
from alembic.config import Config
from apscheduler.triggers.cron import CronTrigger
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker
from starlette.testclient import TestClient

from casttrail.auth.service import AuthService
from casttrail.config import Settings
from casttrail.db import create_engine, create_session_factory
from casttrail.discovery.models import JobRun
from casttrail.discovery.refresh import RefreshReport
from casttrail.discovery.schedule import RefreshJob, recent_runs, run_in_background
from casttrail.main import _start_scheduler, create_app
from casttrail.providers.rostercon_client import (
    PeopleIndex,
    RosterconFetcher,
    create_client,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

PASSWORD = "correct horse battery"
SAME_ORIGIN = {"Sec-Fetch-Site": "same-origin"}
ALEMBIC_INI = Path(__file__).parents[2] / "alembic.ini"


class FakeRefresher:
    """Stands in for the real refresh, and can be held up until released."""

    def __init__(self, *, hold: bool = False) -> None:
        self.calls = 0
        self.started = threading.Event()
        self.release = threading.Event()
        if not hold:
            self.release.set()

    async def run(self) -> RefreshReport | None:
        self.calls += 1
        self.started.set()
        while not self.release.is_set():
            await asyncio.sleep(0.01)
        return RefreshReport()


def _seed(database_url: str, runs: list[JobRun] | None = None) -> None:
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")

    async def run() -> None:
        engine = create_engine(database_url)
        async with create_session_factory(engine)() as session:
            auth = AuthService(session, session_ttl=timedelta(hours=1))
            await auth.create_user(
                email="admin@example.com",
                display_name="Admin",
                password=PASSWORD,
                is_admin=True,
            )
            await auth.create_user(
                email="user@example.com", display_name="User", password=PASSWORD
            )
            session.add_all(runs or [])
            await session.commit()
        await engine.dispose()

    asyncio.run(run())


def _sign_in(client: TestClient, email: str) -> None:
    client.cookies.clear()
    response = client.post("/login", data={"email": email, "password": PASSWORD})
    assert response.status_code == 303


def _clients(settings: Settings, refresher: FakeRefresher) -> Iterator[TestClient]:
    with TestClient(
        create_app(settings, refresher=refresher),
        follow_redirects=False,
        headers=SAME_ORIGIN,
    ) as client:
        yield client


def _state(client: TestClient) -> object:
    return client.app.state  # type: ignore[attr-defined]


@pytest.fixture
def refresher() -> FakeRefresher:
    return FakeRefresher()


@pytest.fixture
def client(
    settings: Settings, database_url: str, refresher: FakeRefresher
) -> Iterator[TestClient]:
    _seed(database_url)
    yield from _clients(settings, refresher)


def _run(status: str, day: int, summary: str | None = None) -> JobRun:
    return JobRun(
        job="rostercon-refresh",
        status=status,
        started_at=datetime(2026, 11, day, 4, 0, tzinfo=UTC),
        summary=summary,
    )


# --- access -----------------------------------------------------------------------


def test_the_refresh_page_needs_a_sign_in(client: TestClient) -> None:
    for response in (client.get("/admin/refresh"), client.post("/admin/refresh")):
        assert response.status_code == 303
        assert response.headers["location"] == "/login"


def test_a_non_admin_gets_a_404_and_starts_nothing(
    client: TestClient, refresher: FakeRefresher
) -> None:
    _sign_in(client, "user@example.com")

    assert client.get("/admin/refresh").status_code == 404
    assert client.post("/admin/refresh").status_code == 404
    assert refresher.calls == 0


def test_a_cross_site_post_is_refused(
    client: TestClient, refresher: FakeRefresher
) -> None:
    _sign_in(client, "admin@example.com")

    response = client.post("/admin/refresh", headers={"Sec-Fetch-Site": "cross-site"})

    assert response.status_code == 403
    assert refresher.calls == 0


# --- the page ---------------------------------------------------------------------


def test_an_admin_sees_the_page_with_no_runs_yet(client: TestClient) -> None:
    _sign_in(client, "admin@example.com")

    page = client.get("/admin/refresh").text

    assert "Nothing has run yet" in page
    assert 'action="/admin/refresh"' in page
    assert 'href="/admin/refresh" aria-current="page"' in page


def test_the_page_says_it_does_not_run_by_itself_when_the_timer_is_off(
    client: TestClient,
) -> None:
    _sign_in(client, "admin@example.com")

    assert "does not run by itself" in client.get("/admin/refresh").text


def test_the_page_names_the_hour_when_the_timer_is_on(
    settings: Settings, database_url: str, refresher: FakeRefresher
) -> None:
    _seed(database_url)
    on = settings.model_copy(update={"refresh_enabled": True, "refresh_hour": 5})

    for client in _clients(on, refresher):
        _sign_in(client, "admin@example.com")
        page = client.get("/admin/refresh").text

    assert "every day at 05:00" in page
    assert "does not run by itself" not in page


def test_recent_runs_are_listed_newest_first_with_their_counts(
    settings: Settings, database_url: str, refresher: FakeRefresher
) -> None:
    counts = {
        "people_watched": 4,
        "people_matched": 3,
        "events_fetched": 11,
        "events_changed": 2,
        "stopped": None,
    }
    stopped = {**counts, "stopped": "Rostercon asked us to wait"}
    other = JobRun(
        job="some-other-job",
        status="failed",
        started_at=datetime(2026, 11, 9, 4, 0, tzinfo=UTC),
    )
    _seed(
        database_url,
        [
            _run("succeeded", 7, json.dumps(counts)),
            _run("failed", 8, json.dumps(stopped)),
            other,
        ],
    )

    for client in _clients(settings, refresher):
        _sign_in(client, "admin@example.com")
        page = client.get("/admin/refresh").text

    assert page.index("2026-11-08 04:00") < page.index("2026-11-07 04:00")
    assert "2026-11-09" not in page
    assert "3 of 4" in page
    assert "Rostercon asked us to wait" in page
    assert "Failed" in page
    assert "Succeeded" in page


@pytest.mark.parametrize("stored", [None, "not json", "[1, 2]", '"text"', "{}"])
def test_a_missing_or_damaged_record_still_shows_the_run(
    settings: Settings, database_url: str, refresher: FakeRefresher, stored: str | None
) -> None:
    _seed(database_url, [_run("failed", 8, stored)])

    for client in _clients(settings, refresher):
        _sign_in(client, "admin@example.com")
        response = client.get("/admin/refresh")

    assert response.status_code == 200
    assert "2026-11-08 04:00" in response.text


def test_an_unknown_notice_shows_nothing(client: TestClient) -> None:
    _sign_in(client, "admin@example.com")

    page = client.get("/admin/refresh?notice=%3Cscript%3E").text

    assert "<script>" not in page
    assert 'role="status"' not in page


# --- the button -------------------------------------------------------------------


def test_the_button_starts_a_refresh_and_returns_at_once(
    client: TestClient, refresher: FakeRefresher
) -> None:
    _sign_in(client, "admin@example.com")

    response = client.post("/admin/refresh")

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/refresh?notice=started"
    assert refresher.started.wait(timeout=5)
    assert refresher.calls == 1
    assert "has started" in client.get("/admin/refresh?notice=started").text


def test_a_second_press_while_one_runs_starts_nothing(
    settings: Settings, database_url: str
) -> None:
    _seed(database_url)
    slow = FakeRefresher(hold=True)

    for client in _clients(settings, slow):
        _sign_in(client, "admin@example.com")
        client.post("/admin/refresh")
        assert slow.started.wait(timeout=5)
        again = client.post("/admin/refresh")
        page = client.get(again.headers["location"]).text
        slow.release.set()

    assert again.headers["location"] == "/admin/refresh?notice=running"
    assert "already running" in page
    assert slow.calls == 1


def test_the_button_works_again_once_the_last_run_has_finished(
    client: TestClient, refresher: FakeRefresher
) -> None:
    _sign_in(client, "admin@example.com")
    client.post("/admin/refresh")
    assert refresher.started.wait(timeout=5)
    for _ in range(500):
        if _state(client).refresh_task.done():  # type: ignore[attr-defined]
            break
        threading.Event().wait(0.01)

    response = client.post("/admin/refresh")

    assert response.headers["location"] == "/admin/refresh?notice=started"


# --- the timer --------------------------------------------------------------------


async def test_the_timer_is_not_started_unless_switched_on(settings: Settings) -> None:
    assert _start_scheduler(settings, FakeRefresher()) is None


async def test_the_timer_runs_the_refresh_once_a_day_at_the_chosen_hour(
    settings: Settings,
) -> None:
    on = settings.model_copy(update={"refresh_enabled": True, "refresh_hour": 5})
    refresher = FakeRefresher()

    scheduler = _start_scheduler(on, refresher)

    assert scheduler is not None
    try:
        jobs = scheduler.get_jobs()
        assert len(jobs) == 1
        job = jobs[0]
        assert job.id == "rostercon-refresh"
        assert job.func is run_in_background
        assert job.args == (refresher,)
        assert (job.max_instances, job.coalesce, job.misfire_grace_time) == (
            1,
            True,
            3600,
        )
        assert isinstance(job.trigger, CronTrigger)
        fields = {field.name: str(field) for field in job.trigger.fields}
        assert (fields["hour"], fields["minute"]) == ("5", "0")
        assert fields["day"] == "*"
    finally:
        scheduler.shutdown(wait=False)


def test_the_app_starts_and_stops_the_timer(
    settings: Settings, database_url: str, refresher: FakeRefresher
) -> None:
    _seed(database_url)
    on = settings.model_copy(update={"refresh_enabled": True})
    with TestClient(create_app(on, refresher=refresher)) as client:
        scheduler = _state(client).scheduler  # type: ignore[attr-defined]
        assert scheduler.running
    assert not scheduler.running


def test_the_app_has_no_timer_by_default(
    settings: Settings, database_url: str, refresher: FakeRefresher
) -> None:
    _seed(database_url)
    with TestClient(create_app(settings, refresher=refresher)) as client:
        assert _state(client).scheduler is None  # type: ignore[attr-defined]


def test_leaving_the_app_stops_a_refresh_still_running(
    settings: Settings, database_url: str
) -> None:
    _seed(database_url)
    slow = FakeRefresher(hold=True)
    with TestClient(
        create_app(settings, refresher=slow),
        headers=SAME_ORIGIN,
        follow_redirects=False,
    ) as client:
        _sign_in(client, "admin@example.com")
        client.post("/admin/refresh")
        assert slow.started.wait(timeout=5)
        task = _state(client).refresh_task  # type: ignore[attr-defined]

    assert task.cancelled()


def test_the_real_refresh_is_built_when_none_is_given(
    settings: Settings, database_url: str
) -> None:
    _seed(database_url)
    with TestClient(create_app(settings)) as client:
        assert isinstance(_state(client).refresher, RefreshJob)  # type: ignore[attr-defined]


@pytest.mark.parametrize("hour", [-1, 24, 100])
def test_an_hour_outside_the_day_is_refused(hour: int) -> None:
    with pytest.raises(ValidationError):
        Settings(refresh_hour=hour)


def test_the_timer_settings_come_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CASTTRAIL_REFRESH_ENABLED", "true")
    monkeypatch.setenv("CASTTRAIL_REFRESH_HOUR", "23")

    settings = Settings()

    assert (settings.refresh_enabled, settings.refresh_hour) == (True, 23)


# --- running without anyone waiting -----------------------------------------------


class Scripted:
    def __init__(self, outcome: RefreshReport | Exception | None) -> None:
        self.outcome = outcome

    async def run(self) -> RefreshReport | None:
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


async def test_a_crashing_refresh_is_logged_and_does_not_escape() -> None:
    with structlog.testing.capture_logs() as logs:
        await run_in_background(Scripted(RuntimeError("boom")))

    assert [entry["event"] for entry in logs] == ["background_refresh_failed"]
    assert logs[0]["log_level"] == "error"


async def test_a_refresh_that_returns_quietly_logs_nothing_itself() -> None:
    # The job logs its own start, stop and finish; the wrapper only reports failure.
    with structlog.testing.capture_logs() as logs:
        await run_in_background(Scripted(RefreshReport()))

    assert logs == []


async def test_the_refresh_job_opens_its_own_session_for_each_run(
    db_session: AsyncSession,
) -> None:
    def nothing(request: httpx2.Request) -> httpx2.Response:
        raise AssertionError(f"nothing is watched, so nothing is asked: {request.url}")

    assert db_session.bind is not None
    factory = async_sessionmaker(db_session.bind, expire_on_commit=False)
    fetcher = RosterconFetcher(create_client(transport=httpx2.MockTransport(nothing)))
    job = RefreshJob(factory, fetcher, PeopleIndex(fetcher))

    first = await job.run()
    second = await job.run()

    assert first is not None
    assert second is not None
    assert len(await recent_runs(db_session)) == 2


async def test_recent_runs_stop_at_the_limit(db_session: AsyncSession) -> None:
    for day in range(1, 6):
        db_session.add(_run("succeeded", day))
    await db_session.commit()

    runs = await recent_runs(db_session, limit=3)

    assert [run.started_at.day for run in runs] == [5, 4, 3]
