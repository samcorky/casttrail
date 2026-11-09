import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx2
import pytest
from sqlalchemy import func, select

from casttrail.auth.service import AuthService
from casttrail.discovery import refresh as refresh_module
from casttrail.discovery.models import Appearance, AppearanceSource, Convention, JobRun
from casttrail.discovery.refresh import STALE_AFTER, RosterconRefresh
from casttrail.discovery.service import DiscoveryService
from casttrail.identity.decisions import DecisionService
from casttrail.identity.models import ExternalIdentity, IdentityDecision
from casttrail.matching.models import Match
from casttrail.matching.service import MatchService, RematchResult
from casttrail.providers.base import PersonDetail
from casttrail.providers.rostercon_client import RosterconFetcher, create_client
from casttrail.watching.models import Watch
from casttrail.watching.service import WatchService

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from casttrail.auth.models import User

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).parents[1] / "fixtures" / "rostercon"
START = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
PASSWORD = "correct horse battery"
PAUL_URL = "/en/people/paul-mcgann-en"
EVENTS_URL = "/en/people/paul-mcgann-en/events/18489"
# Paul McGann's real rows: after 2026-11-08 only these two are still to come.
LOVE = "for-the-love-of-time-space-2027"  # 2027-02-13
PENSACON = "pensacon-2027"  # 2027-02-19


def _read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class Clock:
    """A clock that moves forward a minute each time it is read."""

    def __init__(self) -> None:
        self.current = START

    def __call__(self) -> datetime:
        self.current += timedelta(minutes=1)
        return self.current


async def _no_sleep(seconds: float) -> None:
    return None


class FakeRostercon:
    """Serves saved pages by path, and remembers what was asked for."""

    def __init__(self) -> None:
        self.pages: dict[str, tuple[int, str]] = {
            PAUL_URL: (200, _read("person_paul-mcgann-en_page.html")),
            EVENTS_URL: (200, _read("person_paul-mcgann-en_events_page1.html")),
        }
        self.requests: list[str] = []
        self.calls = 0

    def add_event(self, key: str) -> None:
        """Serve an event page: a real one, renamed so each has its own key."""
        text = _read("event_follow-the-yellow-gate-road-take-4.html")
        text = text.replace("follow-the-yellow-gate-road-take-4", key)
        self.pages[f"/en/event-convention/{key}"] = (200, text)

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request.url.path)
        status, text = self.pages.get(request.url.path, (404, ""))
        return httpx2.Response(status, text=text)

    def fetcher(self) -> RosterconFetcher:
        client = create_client(transport=httpx2.MockTransport(self.handler))
        return RosterconFetcher(client, min_interval=0, sleep=_no_sleep)

    def event_requests(self) -> list[str]:
        return [path for path in self.requests if "/event-convention/" in path]


@pytest.fixture
def server() -> FakeRostercon:
    fake = FakeRostercon()
    fake.add_event(LOVE)
    fake.add_event(PENSACON)
    return fake


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
    session: AsyncSession, user: User, name: str = "Paul McGann", tmdb_id: int = 1
) -> None:
    await WatchService(session).watch_person(
        user.id, PersonDetail(tmdb_id=tmdb_id, name=name)
    )


def _refresh(session: AsyncSession, server: FakeRostercon) -> RosterconRefresh:
    return RosterconRefresh(session, server.fetcher(), now=Clock())


async def _count(session: AsyncSession, model: Any) -> int:
    return await session.scalar(select(func.count()).select_from(model)) or 0


async def _only_run(session: AsyncSession) -> JobRun:
    run: JobRun | None = await session.scalar(select(JobRun))
    assert run is not None
    await session.refresh(run)
    return run


# --- what a refresh does ------------------------------------------------------------


async def test_a_refresh_stores_the_upcoming_events_of_a_watched_person(
    db_session: AsyncSession, server: FakeRostercon, users: tuple[User, User]
) -> None:
    await _watch(db_session, users[0])

    report = await _refresh(db_session, server).run()

    assert report is not None
    assert (report.people_watched, report.people_matched) == (1, 1)
    assert (report.events_found, report.events_fetched) == (14, 2)
    assert report.events_changed == 2
    assert report.stopped is None
    assert await _count(db_session, Convention) == 2
    assert await _count(db_session, Appearance) > 0


async def test_only_events_still_to_come_are_fetched(
    db_session: AsyncSession, server: FakeRostercon, users: tuple[User, User]
) -> None:
    await _watch(db_session, users[0])

    await _refresh(db_session, server).run()

    assert sorted(server.event_requests()) == sorted(
        f"/en/event-convention/{key}" for key in (LOVE, PENSACON)
    )


async def test_refreshing_twice_changes_nothing_the_second_time(
    db_session: AsyncSession, server: FakeRostercon, users: tuple[User, User]
) -> None:
    await _watch(db_session, users[0])
    first = await _refresh(db_session, server).run()
    counts = [
        await _count(db_session, model) for model in (Convention, Appearance, JobRun)
    ]

    second = await _refresh(db_session, server).run()

    assert first is not None
    assert second is not None
    assert second.events_fetched == 2
    assert second.events_changed == 0
    assert [
        await _count(db_session, model) for model in (Convention, Appearance)
    ] == counts[:2]
    assert await _count(db_session, JobRun) == counts[2] + 1


async def test_the_person_and_their_event_list_are_each_fetched_once(
    db_session: AsyncSession, server: FakeRostercon, users: tuple[User, User]
) -> None:
    await _watch(db_session, users[0])

    await _refresh(db_session, server).run()

    assert server.requests.count(PAUL_URL) == 1
    assert server.requests.count(EVENTS_URL) == 1


async def test_a_person_watched_by_two_users_is_looked_up_once(
    db_session: AsyncSession, server: FakeRostercon, users: tuple[User, User]
) -> None:
    await _watch(db_session, users[0])
    await _watch(db_session, users[1])

    report = await _refresh(db_session, server).run()

    assert report is not None
    assert report.people_watched == 1
    assert server.requests.count(PAUL_URL) == 1


async def test_two_people_at_the_same_event_fetch_it_once(
    db_session: AsyncSession, server: FakeRostercon, users: tuple[User, User]
) -> None:
    await _watch(db_session, users[0])
    await _watch(db_session, users[0], "Paula McGann", tmdb_id=2)
    server.pages["/en/people/paula-mcgann-en"] = server.pages[PAUL_URL]
    server.pages["/en/people/paula-mcgann-en/events/18489"] = server.pages[EVENTS_URL]

    await _refresh(db_session, server).run()

    assert len(server.event_requests()) == 2


async def test_nothing_watched_means_nothing_is_asked_of_rostercon(
    db_session: AsyncSession, server: FakeRostercon
) -> None:
    report = await _refresh(db_session, server).run()

    assert report is not None
    assert report.people_watched == 0
    assert server.requests == []
    assert (await _only_run(db_session)).status == "succeeded"


async def test_watched_films_and_series_are_not_looked_up(
    db_session: AsyncSession, server: FakeRostercon, users: tuple[User, User]
) -> None:
    from casttrail.providers.base import Production

    await WatchService(db_session).watch_production(
        users[0].id,
        Production(
            tmdb_id=1, media_type="movie", title="Doctor Who", release_year=1996
        ),
    )

    report = await _refresh(db_session, server).run()

    assert report is not None
    assert report.people_watched == 0
    assert server.requests == []


# --- people it cannot place -------------------------------------------------------


async def test_a_person_with_no_rostercon_page_is_counted_and_not_guessed(
    db_session: AsyncSession, server: FakeRostercon, users: tuple[User, User]
) -> None:
    await _watch(db_session, users[0], "Zed Nobody")

    report = await _refresh(db_session, server).run()

    assert report is not None
    assert report.people_unmatched == 1
    # Asked for directly: the two forms of the name, then the two forms of a second
    # person with it, which is how we learn there is nobody to be ambiguous with.
    assert server.requests == [
        "/en/people/zed-nobody-en",
        "/en/people/zed-nobody",
        "/en/people/zed-nobody-2-en",
        "/en/people/zed-nobody-2",
    ]


async def test_a_name_with_two_pages_is_ambiguous_and_not_guessed(
    db_session: AsyncSession, server: FakeRostercon, users: tuple[User, User]
) -> None:
    await _watch(db_session, users[0], "John Smith")
    server.pages["/en/people/john-smith-en"] = server.pages[PAUL_URL]
    server.pages["/en/people/john-smith-2-en"] = server.pages[PAUL_URL]

    report = await _refresh(db_session, server).run()

    assert report is not None
    assert report.people_ambiguous == 1
    assert report.people_matched == 0
    assert server.event_requests() == []
    assert not any("/events/" in path for path in server.requests)


async def test_a_person_page_that_fails_does_not_stop_the_others(
    db_session: AsyncSession, server: FakeRostercon, users: tuple[User, User]
) -> None:
    await _watch(db_session, users[0], "Aaron Gone", tmdb_id=5)
    await _watch(db_session, users[0])
    server.pages["/en/people/aaron-gone-en"] = (500, "")

    report = await _refresh(db_session, server).run()

    assert report is not None
    assert (report.people_failed, report.people_matched) == (1, 1)
    assert report.events_fetched == 2
    assert (await _only_run(db_session)).status == "succeeded"


async def test_a_person_page_without_a_full_list_link_still_gives_its_teaser(
    db_session: AsyncSession, server: FakeRostercon, users: tuple[User, User]
) -> None:
    await _watch(db_session, users[0])
    status, text = server.pages[PAUL_URL]
    server.pages[PAUL_URL] = (
        status,
        text.replace(f"https://www.rostercon.com{EVENTS_URL}", ""),
    )

    report = await _refresh(db_session, server).run()

    assert report is not None
    assert EVENTS_URL not in server.requests
    assert report.events_fetched == 2


async def test_an_event_with_no_date_is_still_fetched(
    db_session: AsyncSession, server: FakeRostercon, users: tuple[User, User]
) -> None:
    await _watch(db_session, users[0])
    status, text = server.pages[EVENTS_URL]
    assert "Feb 19th 2027" in text
    server.pages[EVENTS_URL] = (
        status,
        text.replace("Feb 19th 2027", "To be announced"),
    )
    status, text = server.pages[PAUL_URL]
    server.pages[PAUL_URL] = (status, text.replace("Feb 19th 2027", "To be announced"))

    report = await _refresh(db_session, server).run()

    assert report is not None
    assert PENSACON in " ".join(server.event_requests())


# --- events that cannot be used ---------------------------------------------------


async def test_an_event_page_that_is_missing_is_counted_and_the_run_goes_on(
    db_session: AsyncSession, server: FakeRostercon, users: tuple[User, User]
) -> None:
    await _watch(db_session, users[0])
    del server.pages[f"/en/event-convention/{LOVE}"]

    report = await _refresh(db_session, server).run()

    assert report is not None
    assert (report.events_failed, report.events_changed) == (1, 1)
    assert (await _only_run(db_session)).status == "succeeded"


async def test_an_event_page_that_changed_shape_stores_nothing(
    db_session: AsyncSession, server: FakeRostercon, users: tuple[User, User]
) -> None:
    await _watch(db_session, users[0])
    server.pages[f"/en/event-convention/{LOVE}"] = (200, "<html>new design</html>")

    report = await _refresh(db_session, server).run()

    assert report is not None
    assert (report.events_unreadable, report.events_changed) == (1, 1)
    assert await _count(db_session, Convention) == 1


async def test_soonest_events_come_first_and_the_rest_wait_for_the_next_run(
    db_session: AsyncSession,
    server: FakeRostercon,
    users: tuple[User, User],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _watch(db_session, users[0])
    monkeypatch.setattr(refresh_module, "MAX_EVENT_PAGES", 1)

    report = await _refresh(db_session, server).run()

    assert report is not None
    assert report.events_deferred == 1
    assert server.event_requests() == [f"/en/event-convention/{LOVE}"]


# --- being refused ----------------------------------------------------------------


async def test_a_refusal_stops_the_run_but_keeps_what_was_stored(
    db_session: AsyncSession, server: FakeRostercon, users: tuple[User, User]
) -> None:
    await _watch(db_session, users[0])
    server.pages[f"/en/event-convention/{PENSACON}"] = (503, "")

    report = await _refresh(db_session, server).run()

    assert report is not None
    assert report.stopped == "Rostercon asked us to wait"
    assert report.events_changed == 1
    assert await _count(db_session, Convention) == 1
    run = await _only_run(db_session)
    assert run.status == "failed"
    assert run.finished_at is not None
    assert json.loads(run.summary or "{}")["stopped"] == "Rostercon asked us to wait"


async def test_a_refused_lookup_stops_the_run(
    db_session: AsyncSession, server: FakeRostercon, users: tuple[User, User]
) -> None:
    await _watch(db_session, users[0])
    server.pages[PAUL_URL] = (429, "")

    report = await _refresh(db_session, server).run()

    assert report is not None
    assert report.stopped == "Rostercon asked us to wait"
    assert server.requests == [PAUL_URL]


async def test_a_lookup_that_errors_fails_that_person_and_not_the_run(
    db_session: AsyncSession, server: FakeRostercon, users: tuple[User, User]
) -> None:
    await _watch(db_session, users[0])
    server.pages[PAUL_URL] = (500, "")

    report = await _refresh(db_session, server).run()

    assert report is not None
    assert report.stopped is None
    assert report.people_failed == 1
    assert (await _only_run(db_session)).status == "succeeded"


async def test_a_full_list_that_cannot_be_read_fails_that_person_only(
    db_session: AsyncSession, server: FakeRostercon, users: tuple[User, User]
) -> None:
    await _watch(db_session, users[0])
    server.pages[EVENTS_URL] = (500, "")

    report = await _refresh(db_session, server).run()

    assert report is not None
    assert report.stopped is None
    assert (report.people_failed, report.people_matched) == (1, 0)


async def test_a_refusal_fetching_the_full_list_stops_the_run(
    db_session: AsyncSession, server: FakeRostercon, users: tuple[User, User]
) -> None:
    await _watch(db_session, users[0])
    server.pages[EVENTS_URL] = (429, "")

    report = await _refresh(db_session, server).run()

    assert report is not None
    assert report.stopped == "Rostercon asked us to wait"


async def test_a_name_is_looked_up_in_its_plain_form_when_there_is_no_en_page(
    db_session: AsyncSession, server: FakeRostercon, users: tuple[User, User]
) -> None:
    await _watch(db_session, users[0])
    server.pages["/en/people/paul-mcgann"] = server.pages.pop(PAUL_URL)

    report = await _refresh(db_session, server).run()

    assert report is not None
    assert report.people_matched == 1
    assert server.requests[:2] == [PAUL_URL, "/en/people/paul-mcgann"]


async def test_a_later_run_carries_on_after_a_refusal(
    db_session: AsyncSession, server: FakeRostercon, users: tuple[User, User]
) -> None:
    await _watch(db_session, users[0])
    server.pages[f"/en/event-convention/{PENSACON}"] = (503, "")
    await _refresh(db_session, server).run()
    server.add_event(PENSACON)

    report = await _refresh(db_session, server).run()

    assert report is not None
    assert report.stopped is None
    assert await _count(db_session, Convention) == 2


# --- the lock ---------------------------------------------------------------------


async def test_a_second_refresh_is_turned_away_while_one_is_running(
    db_session: AsyncSession, server: FakeRostercon, users: tuple[User, User]
) -> None:
    await _watch(db_session, users[0])
    clock = Clock()
    db_session.add(JobRun(job="rostercon-refresh", status="running", started_at=START))
    await db_session.commit()
    refresh = RosterconRefresh(db_session, server.fetcher(), now=clock)

    report = await refresh.run()

    assert report is None
    assert server.requests == []
    assert await _count(db_session, JobRun) == 1


async def test_a_refresh_that_died_long_ago_is_taken_over(
    db_session: AsyncSession, server: FakeRostercon, users: tuple[User, User]
) -> None:
    await _watch(db_session, users[0])
    old = JobRun(
        job="rostercon-refresh",
        status="running",
        started_at=START - STALE_AFTER - timedelta(minutes=5),
    )
    db_session.add(old)
    await db_session.commit()

    report = await _refresh(db_session, server).run()

    assert report is not None
    await db_session.refresh(old)
    assert old.status == "failed"
    assert "Taken over" in (old.summary or "")
    assert await _count(db_session, JobRun) == 2


async def test_a_run_that_is_not_yet_stale_is_left_alone(
    db_session: AsyncSession, server: FakeRostercon, users: tuple[User, User]
) -> None:
    await _watch(db_session, users[0])
    recent = JobRun(
        job="rostercon-refresh",
        status="running",
        started_at=START - STALE_AFTER + timedelta(minutes=5),
    )
    db_session.add(recent)
    await db_session.commit()

    assert await _refresh(db_session, server).run() is None
    await db_session.refresh(recent)
    assert recent.status == "running"


async def test_a_finished_run_does_not_block_the_next(
    db_session: AsyncSession, server: FakeRostercon, users: tuple[User, User]
) -> None:
    await _watch(db_session, users[0])

    assert await _refresh(db_session, server).run() is not None
    assert await _refresh(db_session, server).run() is not None


async def test_a_crash_is_recorded_released_and_raised(
    db_session: AsyncSession,
    server: FakeRostercon,
    users: tuple[User, User],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _watch(db_session, users[0])

    async def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(DiscoveryService, "ingest", broken)

    with pytest.raises(RuntimeError, match="boom"):
        await _refresh(db_session, server).run()

    run = await _only_run(db_session)
    assert run.status == "failed"
    assert json.loads(run.summary or "{}")["stopped"] == "The refresh crashed"
    monkeypatch.undo()
    assert await _refresh(db_session, server).run() is not None


async def test_the_run_records_what_it_did(
    db_session: AsyncSession, server: FakeRostercon, users: tuple[User, User]
) -> None:
    await _watch(db_session, users[0])

    await _refresh(db_session, server).run()

    run = await _only_run(db_session)
    summary = json.loads(run.summary or "{}")
    assert (run.status, run.finished_at is not None) == ("succeeded", True)
    assert summary["people_matched"] == 1
    assert summary["events_fetched"] == 2
    assert summary["stopped"] is None


async def test_a_refusal_while_looking_up_a_person_stops_the_run(
    db_session: AsyncSession, server: FakeRostercon, users: tuple[User, User]
) -> None:
    await _watch(db_session, users[0])
    server.pages[PAUL_URL] = (503, "")

    report = await _refresh(db_session, server).run()

    assert report is not None
    assert report.stopped == "Rostercon asked us to wait"
    assert report.people_failed == 0
    assert server.event_requests() == []
    assert (await _only_run(db_session)).status == "failed"


async def test_events_are_taken_by_date_and_not_by_name(
    db_session: AsyncSession,
    server: FakeRostercon,
    users: tuple[User, User],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _watch(db_session, users[0])
    for path in (PAUL_URL, EVENTS_URL):
        status, text = server.pages[path]
        assert "Feb 13th 2027" in text
        # Now "for-the-love-of-time-space-2027" is later, but sorts first by name.
        server.pages[path] = (status, text.replace("Feb 13th 2027", "Feb 25th 2027"))
    monkeypatch.setattr(refresh_module, "MAX_EVENT_PAGES", 1)

    await _refresh(db_session, server).run()

    assert server.event_requests() == [f"/en/event-convention/{PENSACON}"]


async def test_taking_over_leaves_finished_runs_alone(
    db_session: AsyncSession, server: FakeRostercon, users: tuple[User, User]
) -> None:
    await _watch(db_session, users[0])
    done = JobRun(
        job="rostercon-refresh",
        status="succeeded",
        started_at=START - STALE_AFTER - timedelta(days=3),
        summary="{}",
    )
    db_session.add(done)
    await db_session.commit()

    await _refresh(db_session, server).run()

    await db_session.refresh(done)
    assert (done.status, done.summary) == ("succeeded", "{}")


async def test_a_page_that_stops_parsing_keeps_what_was_stored_before(
    db_session: AsyncSession, server: FakeRostercon, users: tuple[User, User]
) -> None:
    await _watch(db_session, users[0])
    await _refresh(db_session, server).run()
    before = (
        await _count(db_session, Convention),
        await _count(db_session, Appearance),
    )
    names = set(await db_session.scalars(select(Convention.name)))
    for key in (LOVE, PENSACON):
        server.pages[f"/en/event-convention/{key}"] = (200, "<html>new design</html>")

    report = await _refresh(db_session, server).run()

    assert report is not None
    assert report.events_unreadable == 2
    assert report.events_changed == 0
    assert (
        await _count(db_session, Convention),
        await _count(db_session, Appearance),
    ) == before
    assert set(await db_session.scalars(select(Convention.name))) == names
    assert (
        await db_session.scalar(select(func.count()).select_from(AppearanceSource))
    ) == before[1]
    unlisted = await db_session.scalar(
        select(func.count())
        .select_from(AppearanceSource)
        .where(AppearanceSource.listed.is_(False))
    )
    assert unlisted == 0


async def test_a_refresh_matches_the_guests_it_stored_to_watched_people(
    db_session: AsyncSession, server: FakeRostercon, users: tuple[User, User]
) -> None:
    await _watch(db_session, users[0])
    await _watch(db_session, users[1], "Michael Shanks", 7)

    report = await _refresh(db_session, server).run()
    again = await _refresh(db_session, server).run()

    assert report is not None
    assert again is not None
    assert report.matches_added == 2  # Michael Shanks at each of the two events
    assert (again.matches_added, again.matches_removed) == (0, 0)
    summary = json.loads(
        (await db_session.scalars(select(JobRun.summary))).first() or "{}"
    )
    assert summary["matches_added"] in (0, 2)


async def test_matches_are_updated_even_when_the_run_is_cut_short(
    db_session: AsyncSession, server: FakeRostercon, users: tuple[User, User]
) -> None:
    await _watch(db_session, users[0])
    await _watch(db_session, users[1], "Michael Shanks", 7)
    server.pages[f"/en/event-convention/{PENSACON}"] = (503, "")

    report = await _refresh(db_session, server).run()

    assert report is not None
    assert report.stopped is not None
    assert report.matches_added == 1


async def test_a_refresh_reports_matches_that_went_away(
    db_session: AsyncSession, server: FakeRostercon, users: tuple[User, User]
) -> None:
    await _watch(db_session, users[0])
    await _watch(db_session, users[1], "Michael Shanks", 7)
    await _refresh(db_session, server).run()
    watch = await db_session.scalar(select(Watch).where(Watch.user_id == users[1].id))
    assert watch is not None
    await WatchService(db_session).unwatch(users[1].id, watch.id)

    report = await _refresh(db_session, server).run()

    assert report is not None
    assert report.matches_removed == 2


# --- WP-4 gate: a person's correction survives repeated syncs ---------------------


async def _snapshot(session: AsyncSession) -> tuple[Any, Any, Any]:
    decisions = [
        (str(d.external_identity_id), d.decision)
        for d in await session.scalars(
            select(IdentityDecision).order_by(IdentityDecision.id)
        )
    ]
    links = [
        (i.external_id, str(i.person_id), i.display_name or "")
        for i in await session.scalars(
            select(ExternalIdentity)
            .where(ExternalIdentity.provider == "rostercon")
            .order_by(ExternalIdentity.external_id)
        )
    ]
    matches = [
        (m.user_id, m.person_id, m.kind, m.reason, m.created_at)
        for m in await session.scalars(
            select(Match).order_by(Match.user_id, Match.person_id, Match.appearance_id)
        )
    ]
    return decisions, links, matches


async def test_a_correction_survives_three_more_syncs_and_rematches(
    db_session: AsyncSession, server: FakeRostercon, users: tuple[User, User]
) -> None:
    await _watch(db_session, users[0])
    shanks = await WatchService(db_session).watch_person(
        users[0].id, PersonDetail(tmdb_id=7, name="Michael Shanks")
    )
    jackson = await WatchService(db_session).watch_person(
        users[0].id, PersonDetail(tmdb_id=8, name="Andrew Jackson")
    )
    await _refresh(db_session, server).run()
    identities = {
        i.external_id: i.id
        for i in await db_session.scalars(
            select(ExternalIdentity).where(ExternalIdentity.provider == "rostercon")
        )
    }
    decisions = DecisionService(db_session)
    assert shanks.person_id is not None
    assert jackson.person_id is not None
    await decisions.confirm(
        identities["michael-shanks-en"], shanks.person_id, users[0].id
    )
    await decisions.reject(
        identities["andrew-jackson-en"], jackson.person_id, users[0].id
    )
    await MatchService(db_session).rematch()
    corrected = await _snapshot(db_session)
    kinds = {(m[1], m[2]) for m in corrected[2]}
    assert (shanks.person_id, "confirmed") in kinds
    assert all(person != jackson.person_id for person, _ in kinds)

    for _ in range(3):
        report = await _refresh(db_session, server).run()
        assert report is not None
        assert report.stopped is None
        assert (report.matches_added, report.matches_removed) == (0, 0)
        assert await MatchService(db_session).rematch() == RematchResult(0, 0, 0)

    assert await _snapshot(db_session) == corrected
    assert await _count(db_session, Convention) == 2
