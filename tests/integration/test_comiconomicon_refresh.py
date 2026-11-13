import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx2
import pytest
import structlog
from sqlalchemy import func, select

from casttrail.auth.service import AuthService
from casttrail.catalogue.models import Person
from casttrail.discovery.comiconomicon_refresh import JOB, ComiconomiconRefresh
from casttrail.discovery.links import COMICONOMICON, SourceLinks
from casttrail.discovery.models import (
    Appearance,
    Convention,
    ConventionSource,
    JobRun,
    SourceLookup,
)
from casttrail.discovery.source_refresh import STALE_AFTER
from casttrail.matching.models import Match
from casttrail.providers.base import PersonDetail
from casttrail.providers.comiconomicon_client import ComiconomiconFetcher
from casttrail.providers.polite import create_client
from casttrail.watching.service import WatchService

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from casttrail.auth.models import User

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).parents[1] / "fixtures" / "comiconomicon"
START = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
PASSWORD = "correct horse battery"
SEARCH = "/xhr/indexautocomplete.php"
GUEST = "/guest/518/Andrew_Jackson"
EVENT = "/event/42676/Follow_The_Yellow_Gate_Road_Take_4_"


def _read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _guest_answer(guest_id: str, name: str) -> str:
    return json.dumps(
        [
            {
                "id": guest_id,
                "name": name,
                "type": "guest",
                "data1": None,
                "data2": None,
                "data3": None,
                "appearances": 1,
                "new": 0,
                "url": f"/guest/{guest_id}/{name.replace(' ', '_')}",
            }
        ]
    )


class Clock:
    """A clock that moves forward a minute each time it is read."""

    def __init__(self, start: datetime = START) -> None:
        self.current = start

    def __call__(self) -> datetime:
        self.current += timedelta(minutes=1)
        return self.current


async def _no_sleep(seconds: float) -> None:
    return None


class FakeComiconomicon:
    """Serves saved pages by path, and remembers what was asked for."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.searches: dict[str, tuple[int, str]] = {
            "andrew jackson": (200, _read("autocomplete_guest_andrew_jackson.json"))
        }
        self.pages: dict[str, tuple[int, str]] = {
            GUEST: (200, _read("guest_518_andrew_jackson.html")),
            EVENT: (200, _read("event_42676_follow_the_yellow_gate_road.html")),
        }
        self.requests: list[str] = []
        self.terms: list[str] = []
        self.open_during_request: list[bool] = []

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        path = request.url.path
        self.open_during_request.append(self.session.in_transaction())
        if path == SEARCH:
            term = request.url.params["term"].lower()
            self.terms.append(term)
            self.requests.append(SEARCH)
            status, text = self.searches.get(term, (200, "[]"))
        else:
            self.requests.append(path)
            status, text = self.pages.get(path, (404, ""))
        return httpx2.Response(status, text=text)

    def fetcher(self) -> ComiconomiconFetcher:
        client = create_client(transport=httpx2.MockTransport(self.handler))
        return ComiconomiconFetcher(client, min_interval=0, sleep=_no_sleep)


@pytest.fixture
def server(db_session: AsyncSession) -> FakeComiconomicon:
    return FakeComiconomicon(db_session)


@pytest.fixture
async def users(db_session: AsyncSession) -> tuple[User, User]:
    auth = AuthService(db_session, session_ttl=timedelta(hours=1))
    alice = await auth.create_user(
        email="alice@example.com", display_name="Alice", password=PASSWORD
    )
    bob = await auth.create_user(
        email="bob@example.com", display_name="Bob", password=PASSWORD
    )
    return alice, bob


async def _watch(
    session: AsyncSession, user: User, name: str = "Andrew Jackson", tmdb_id: int = 1
) -> None:
    await WatchService(session).watch_person(
        user.id, PersonDetail(tmdb_id=tmdb_id, name=name)
    )


def _refresh(
    session: AsyncSession, server: FakeComiconomicon, clock: Clock | None = None
) -> ComiconomiconRefresh:
    return ComiconomiconRefresh(session, server.fetcher(), now=clock or Clock())


async def _count(session: AsyncSession, model: Any) -> int:
    return await session.scalar(select(func.count()).select_from(model)) or 0


async def _person_id(session: AsyncSession, name: str) -> Any:
    return await session.scalar(select(Person.id).where(Person.name == name))


async def _only_run(session: AsyncSession) -> JobRun:
    run: JobRun | None = await session.scalar(select(JobRun))
    assert run is not None
    await session.refresh(run)
    return run


# --- what a refresh does ------------------------------------------------------------


async def test_a_refresh_stores_the_upcoming_events_of_a_watched_person(
    db_session: AsyncSession, server: FakeComiconomicon, users: tuple[User, User]
) -> None:
    await _watch(db_session, users[0])

    report = await _refresh(db_session, server).run()

    assert report is not None
    assert (report.people_watched, report.people_matched) == (1, 1)
    assert (report.events_found, report.events_fetched, report.events_changed) == (
        1,
        1,
        1,
    )
    assert report.stopped is None
    assert await _count(db_session, Convention) == 1
    assert await _count(db_session, Appearance) > 0
    source = await db_session.scalar(select(ConventionSource))
    assert source is not None
    assert (source.provider, source.source_key) == ("comiconomicon", "42676")


async def test_the_person_is_searched_for_then_their_page_then_their_events(
    db_session: AsyncSession, server: FakeComiconomicon, users: tuple[User, User]
) -> None:
    await _watch(db_session, users[0])

    await _refresh(db_session, server).run()

    assert server.requests == [SEARCH, GUEST, EVENT]
    assert server.terms == ["andrew jackson"]


async def test_the_second_run_does_not_search_again_and_changes_nothing(
    db_session: AsyncSession, server: FakeComiconomicon, users: tuple[User, User]
) -> None:
    await _watch(db_session, users[0])
    await _refresh(db_session, server).run()
    counts = [await _count(db_session, m) for m in (Convention, Appearance)]
    server.requests.clear()

    second = await _refresh(db_session, server).run()

    assert second is not None
    assert server.requests == [GUEST, EVENT]
    assert second.events_changed == 0
    assert [await _count(db_session, m) for m in (Convention, Appearance)] == counts
    assert await _count(db_session, JobRun) == 2


async def test_a_person_watched_by_two_users_is_looked_up_once(
    db_session: AsyncSession, server: FakeComiconomicon, users: tuple[User, User]
) -> None:
    await _watch(db_session, users[0])
    await _watch(db_session, users[1])

    report = await _refresh(db_session, server).run()

    assert report is not None
    assert report.people_watched == 1
    assert server.requests.count(SEARCH) == 1


async def test_two_people_at_the_same_event_fetch_it_once(
    db_session: AsyncSession, server: FakeComiconomicon, users: tuple[User, User]
) -> None:
    await _watch(db_session, users[0])
    await _watch(db_session, users[0], "Second Guest", tmdb_id=2)
    server.searches["second guest"] = (200, _guest_answer("519", "Second Guest"))
    server.pages["/guest/519/Second_Guest"] = server.pages[GUEST]

    report = await _refresh(db_session, server).run()

    assert report is not None
    assert (report.people_matched, report.events_found, report.events_fetched) == (
        2,
        1,
        1,
    )
    assert server.requests.count(EVENT) == 1


async def test_nothing_watched_means_nothing_is_asked_of_comiconomicon(
    db_session: AsyncSession, server: FakeComiconomicon
) -> None:
    report = await _refresh(db_session, server).run()

    assert report is not None
    assert (report.people_watched, report.events_fetched) == (0, 0)
    assert server.requests == []
    assert (await _only_run(db_session)).status == "succeeded"


async def test_only_events_still_to_come_are_fetched(
    db_session: AsyncSession, server: FakeComiconomicon, users: tuple[User, User]
) -> None:
    await _watch(db_session, users[0])
    after_the_event = Clock(datetime(2027, 6, 1, tzinfo=UTC))

    report = await _refresh(db_session, server, after_the_event).run()

    assert report is not None
    assert (report.events_found, report.events_fetched) == (1, 0)
    assert EVENT not in server.requests


async def test_matches_are_updated_from_the_guests_it_stored(
    db_session: AsyncSession, server: FakeComiconomicon, users: tuple[User, User]
) -> None:
    await _watch(db_session, users[0])

    report = await _refresh(db_session, server).run()

    assert report is not None
    assert report.matches_added > 0
    assert await _count(db_session, Match) == report.matches_added


# --- people it cannot place -----------------------------------------------------------


async def test_a_name_the_site_does_not_know_is_counted_and_nothing_else_is_asked(
    db_session: AsyncSession, server: FakeComiconomicon, users: tuple[User, User]
) -> None:
    await _watch(db_session, users[0], "Zed Nobody")

    report = await _refresh(db_session, server).run()

    assert report is not None
    assert (report.people_unmatched, report.people_matched) == (1, 0)
    assert server.requests == [SEARCH]


async def test_a_name_with_two_guests_is_ambiguous_and_not_guessed(
    db_session: AsyncSession, server: FakeComiconomicon, users: tuple[User, User]
) -> None:
    await _watch(db_session, users[0], "John Smith")
    both = json.loads(_guest_answer("1", "John Smith")) + json.loads(
        _guest_answer("2", "John Smith")
    )
    server.searches["john smith"] = (200, json.dumps(both))

    report = await _refresh(db_session, server).run()

    assert report is not None
    assert (report.people_ambiguous, report.people_matched) == (1, 0)
    assert server.requests == [SEARCH]


async def test_a_page_linked_by_hand_is_used_without_searching(
    db_session: AsyncSession, server: FakeComiconomicon, users: tuple[User, User]
) -> None:
    await _watch(db_session, users[0], "Andy J")
    person_id = await _person_id(db_session, "Andy J")
    await SourceLinks(db_session, COMICONOMICON).link(
        person_id, "https://comiconomicon.com" + GUEST
    )

    report = await _refresh(db_session, server).run()

    assert report is not None
    assert (report.people_matched, report.people_unmatched) == (1, 0)
    assert server.requests == [GUEST, EVENT]


# --- things going wrong ---------------------------------------------------------------


async def test_a_search_that_errors_fails_that_person_and_not_the_run(
    db_session: AsyncSession, server: FakeComiconomicon, users: tuple[User, User]
) -> None:
    await _watch(db_session, users[0], "Aaron Gone", tmdb_id=5)
    await _watch(db_session, users[0])
    server.searches["aaron gone"] = (500, "")

    report = await _refresh(db_session, server).run()

    assert report is not None
    assert (report.people_failed, report.people_matched) == (1, 1)
    assert report.stopped is None
    # An outage is not remembered as "not found".
    names = await db_session.scalars(select(SourceLookup.outcome))
    assert list(names) == ["found"]


async def test_a_guest_page_that_is_missing_fails_that_person(
    db_session: AsyncSession, server: FakeComiconomicon, users: tuple[User, User]
) -> None:
    await _watch(db_session, users[0])
    del server.pages[GUEST]

    report = await _refresh(db_session, server).run()

    assert report is not None
    assert (report.people_failed, report.people_matched) == (1, 0)
    assert (await _only_run(db_session)).status == "succeeded"


async def test_a_found_guest_page_that_has_gone_is_searched_for_again_next_time(
    db_session: AsyncSession, server: FakeComiconomicon, users: tuple[User, User]
) -> None:
    await _watch(db_session, users[0])
    await _refresh(db_session, server).run()
    stored = server.pages.pop(GUEST)
    await _refresh(db_session, server).run()
    assert await db_session.scalar(select(func.count()).select_from(SourceLookup)) == 0
    server.pages[GUEST] = stored
    server.requests.clear()

    report = await _refresh(db_session, server).run()

    assert report is not None
    assert report.people_matched == 1
    assert server.requests[0] == SEARCH


async def test_a_guest_page_that_errors_keeps_the_remembered_guest(
    db_session: AsyncSession, server: FakeComiconomicon, users: tuple[User, User]
) -> None:
    await _watch(db_session, users[0])
    await _refresh(db_session, server).run()
    server.pages[GUEST] = (500, "")

    report = await _refresh(db_session, server).run()

    # A hiccup is not a reason to forget who the guest is.
    assert report is not None
    assert report.people_failed == 1
    [kept] = list(await db_session.scalars(select(SourceLookup)))
    assert (kept.outcome, kept.source_key) == ("found", "518")


async def test_a_page_linked_by_hand_that_has_gone_is_kept(
    db_session: AsyncSession, server: FakeComiconomicon, users: tuple[User, User]
) -> None:
    await _watch(db_session, users[0], "Andy J")
    person_id = await _person_id(db_session, "Andy J")
    await SourceLinks(db_session, COMICONOMICON).link(
        person_id, "https://comiconomicon.com" + GUEST
    )
    del server.pages[GUEST]

    report = await _refresh(db_session, server).run()

    assert report is not None
    assert report.people_failed == 1
    assert await SourceLinks(db_session, COMICONOMICON).manual(person_id) is not None


async def test_a_refusal_on_the_search_stops_the_run(
    db_session: AsyncSession, server: FakeComiconomicon, users: tuple[User, User]
) -> None:
    await _watch(db_session, users[0])
    server.searches["andrew jackson"] = (503, "")

    report = await _refresh(db_session, server).run()

    assert report is not None
    assert report.stopped == "Comiconomicon asked us to wait"
    run = await _only_run(db_session)
    assert run.status == "failed"
    assert (
        json.loads(run.summary or "{}")["stopped"] == "Comiconomicon asked us to wait"
    )
    assert await _count(db_session, Convention) == 0


async def test_a_refusal_on_a_guest_page_stops_the_run(
    db_session: AsyncSession, server: FakeComiconomicon, users: tuple[User, User]
) -> None:
    await _watch(db_session, users[0])
    server.pages[GUEST] = (429, "")

    report = await _refresh(db_session, server).run()

    assert report is not None
    assert report.stopped == "Comiconomicon asked us to wait"
    assert report.people_failed == 0


async def test_a_refusal_on_an_event_page_stops_the_run_but_keeps_the_rest(
    db_session: AsyncSession, server: FakeComiconomicon, users: tuple[User, User]
) -> None:
    await _watch(db_session, users[0])
    server.pages[EVENT] = (503, "")

    report = await _refresh(db_session, server).run()

    assert report is not None
    assert report.stopped == "Comiconomicon asked us to wait"
    # The guest was found and remembered, so the next run does not search again.
    assert await _count(db_session, SourceLookup) == 1


async def test_an_event_page_that_errors_is_counted_and_the_run_goes_on(
    db_session: AsyncSession, server: FakeComiconomicon, users: tuple[User, User]
) -> None:
    await _watch(db_session, users[0])
    server.pages[EVENT] = (500, "")

    report = await _refresh(db_session, server).run()

    assert report is not None
    assert (report.events_failed, report.events_fetched) == (1, 0)
    assert report.stopped is None


async def test_an_event_page_that_changed_shape_stores_nothing(
    db_session: AsyncSession, server: FakeComiconomicon, users: tuple[User, User]
) -> None:
    await _watch(db_session, users[0])
    server.pages[EVENT] = (200, "<html><body>nothing here</body></html>")

    report = await _refresh(db_session, server).run()

    assert report is not None
    assert (report.events_unreadable, report.events_changed) == (1, 0)
    assert await _count(db_session, Convention) == 0


# --- the run itself -------------------------------------------------------------------


async def test_the_run_is_recorded_under_its_own_name(
    db_session: AsyncSession, server: FakeComiconomicon, users: tuple[User, User]
) -> None:
    await _watch(db_session, users[0])

    await _refresh(db_session, server).run()

    run = await _only_run(db_session)
    assert (run.job, run.status) == (JOB, "succeeded")
    assert JOB == "comiconomicon-refresh"
    counts = json.loads(run.summary or "{}")
    assert (counts["people_matched"], counts["events_fetched"]) == (1, 1)


async def test_a_second_refresh_is_turned_away_while_one_is_running(
    db_session: AsyncSession, server: FakeComiconomicon
) -> None:
    db_session.add(JobRun(job=JOB, status="running", started_at=START))
    await db_session.commit()

    assert await _refresh(db_session, server).run() is None
    assert server.requests == []


async def test_a_rostercon_run_does_not_block_a_comiconomicon_run(
    db_session: AsyncSession, server: FakeComiconomicon
) -> None:
    db_session.add(JobRun(job="rostercon-refresh", status="running", started_at=START))
    await db_session.commit()

    assert await _refresh(db_session, server).run() is not None


async def test_a_run_that_died_long_ago_is_taken_over(
    db_session: AsyncSession, server: FakeComiconomicon
) -> None:
    stale = JobRun(
        job=JOB,
        status="running",
        started_at=START - STALE_AFTER - timedelta(minutes=5),
    )
    db_session.add(stale)
    await db_session.commit()

    assert await _refresh(db_session, server).run() is not None

    await db_session.refresh(stale)
    assert stale.status == "failed"


async def test_a_crash_is_recorded_released_and_raised(
    db_session: AsyncSession,
    server: FakeComiconomicon,
    users: tuple[User, User],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _watch(db_session, users[0])

    async def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(
        "casttrail.discovery.source_refresh.DiscoveryService.ingest", broken
    )

    with pytest.raises(RuntimeError, match="boom"):
        await _refresh(db_session, server).run()

    run = await _only_run(db_session)
    assert run.status == "failed"
    assert json.loads(run.summary or "{}")["stopped"] == "The refresh crashed"


async def test_stopping_the_app_mid_refresh_is_an_interruption(
    db_session: AsyncSession, server: FakeComiconomicon, users: tuple[User, User]
) -> None:
    await _watch(db_session, users[0])
    asked = asyncio.Event()

    async def never_answers(request: httpx2.Request) -> httpx2.Response:
        asked.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    client = create_client(transport=httpx2.MockTransport(never_answers))
    fetcher = ComiconomiconFetcher(client, min_interval=0, sleep=_no_sleep)
    task = asyncio.create_task(ComiconomiconRefresh(db_session, fetcher).run())
    await asked.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    run = await _only_run(db_session)
    assert json.loads(run.summary or "{}")["stopped"] == "Interrupted: the app stopped"
    assert await _refresh(db_session, server).run() is not None


# --- the database is free while the site is slow ------------------------------------


async def test_no_transaction_is_open_while_the_site_is_asked(
    db_session: AsyncSession, server: FakeComiconomicon, users: tuple[User, User]
) -> None:
    await _watch(db_session, users[0])

    await _refresh(db_session, server).run()
    server.open_during_request.clear()
    await _refresh(db_session, server).run()

    # The second run used the remembered guest, so its first request is the page.
    assert server.open_during_request
    assert not any(server.open_during_request)


# --- logging ----------------------------------------------------------------------


async def test_the_run_logs_which_source_it_is(
    db_session: AsyncSession, server: FakeComiconomicon, users: tuple[User, User]
) -> None:
    await _watch(db_session, users[0])

    with structlog.testing.capture_logs() as events:
        await _refresh(db_session, server).run()

    started = [e for e in events if e["event"] == "refresh_started"]
    finished = [e for e in events if e["event"] == "refresh_finished"]
    assert [e["source"] for e in started + finished] == ["comiconomicon"] * 2
    assert finished[0]["people_matched"] == 1
