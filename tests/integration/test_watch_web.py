import asyncio
import re
import uuid
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select
from starlette.testclient import TestClient

from casttrail.auth.service import AuthService
from casttrail.db import create_engine, create_session_factory
from casttrail.main import create_app
from casttrail.providers.base import (
    CastMember,
    MediaType,
    PersonCredit,
    PersonDetail,
    PersonSummary,
    Production,
    ProviderError,
)
from casttrail.watching.models import Watch

if TYPE_CHECKING:
    from collections.abc import Iterator

    from casttrail.config import Settings

pytestmark = pytest.mark.integration

PASSWORD = "correct horse battery"
SAME_ORIGIN = {"Sec-Fetch-Site": "same-origin"}
ALEMBIC_INI = Path(__file__).parents[2] / "alembic.ini"

EDDIE = PersonDetail(tmdb_id=120679, name="Eddie McClintock", imdb_id="nm0565858")
WAREHOUSE = Production(
    tmdb_id=15819, media_type="tv", title="Warehouse 13", release_year=2009
)
SKYFALL = Production(
    tmdb_id=37724, media_type="movie", title="Skyfall", release_year=2012
)


class FakeMetadata:
    provider_id = "fake"

    def __init__(self) -> None:
        self.error: ProviderError | None = None
        self.lookups = 0

    async def search_people(self, query: str) -> list[PersonSummary]:
        return [PersonSummary(tmdb_id=EDDIE.tmdb_id, name=EDDIE.name)]

    async def search_productions(self, query: str) -> list[Production]:
        return [SKYFALL, WAREHOUSE]

    async def production(
        self, media_type: MediaType, tmdb_id: int
    ) -> Production | None:
        self.lookups += 1
        if self.error:
            raise self.error
        for production in (SKYFALL, WAREHOUSE):
            if (production.media_type, production.tmdb_id) == (media_type, tmdb_id):
                return production
        return None

    async def person(self, tmdb_id: int) -> PersonDetail | None:
        self.lookups += 1
        if self.error:
            raise self.error
        return EDDIE if tmdb_id == EDDIE.tmdb_id else None

    async def person_credits(self, tmdb_id: int) -> list[PersonCredit] | None:
        return None

    async def cast(
        self, media_type: MediaType, tmdb_id: int, *, limit: int = 10
    ) -> list[CastMember] | None:
        return None


def _seed(database_url: str) -> None:
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")

    async def run() -> None:
        engine = create_engine(database_url)
        async with create_session_factory(engine)() as session:
            auth = AuthService(session, session_ttl=timedelta(hours=1))
            for name in ("alice", "bob"):
                await auth.create_user(
                    email=f"{name}@example.com",
                    display_name=name.title(),
                    password=PASSWORD,
                )
        await engine.dispose()

    asyncio.run(run())


def _watch_count(database_url: str) -> int:
    async def run() -> int:
        engine = create_engine(database_url)
        async with create_session_factory(engine)() as session:
            count = await session.scalar(select(func.count()).select_from(Watch))
        await engine.dispose()
        return count or 0

    return asyncio.run(run())


def _sign_in(client: TestClient, name: str) -> None:
    client.cookies.clear()
    response = client.post(
        "/login", data={"email": f"{name}@example.com", "password": PASSWORD}
    )
    assert response.status_code == 303


def _client(settings: Settings, fake: FakeMetadata) -> TestClient:
    return TestClient(
        create_app(settings, metadata_provider=fake),
        follow_redirects=False,
        headers=SAME_ORIGIN,
    )


@pytest.fixture
def fake() -> FakeMetadata:
    return FakeMetadata()


@pytest.fixture
def seeded(settings: Settings, database_url: str) -> Settings:
    _seed(database_url)
    return settings


@pytest.fixture
def client(seeded: Settings, fake: FakeMetadata) -> Iterator[TestClient]:
    with _client(seeded, fake) as test_client:
        _sign_in(test_client, "alice")
        yield test_client


def _watch_ids(page: str) -> list[str]:
    return re.findall(r'action="/watches/([0-9a-f-]{36})/delete"', page)


def _watch_person(client: TestClient) -> object:
    return client.post("/watches/people", data={"tmdb_id": EDDIE.tmdb_id})


def _watch_series(client: TestClient) -> object:
    return client.post(
        "/watches/productions", data={"media_type": "tv", "tmdb_id": WAREHOUSE.tmdb_id}
    )


# --- sign-in required -----------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "url"),
    [
        ("GET", "/watchlist"),
        ("POST", "/watches/people"),
        ("POST", "/watches/productions"),
        ("POST", f"/watches/{uuid.uuid4()}/delete"),
    ],
)
def test_everything_needs_a_sign_in(
    seeded: Settings, fake: FakeMetadata, method: str, url: str
) -> None:
    with _client(seeded, fake) as anonymous:
        response = anonymous.request(method, url, data={"tmdb_id": "1"})

    assert (response.status_code, response.headers["location"]) == (303, "/login")


# --- watching -------------------------------------------------------------------


def test_the_watchlist_starts_empty_and_points_at_search(client: TestClient) -> None:
    page = client.get("/watchlist").text

    assert "not watching anything yet" in page
    assert 'href="/search"' in page


def test_watching_a_person_puts_them_on_the_watchlist(client: TestClient) -> None:
    response = _watch_person(client)

    assert response.status_code == 303  # type: ignore[attr-defined]
    assert response.headers["location"] == "/search"  # type: ignore[attr-defined]
    page = client.get("/watchlist").text
    assert "Eddie McClintock" in page
    assert ">People</h2>" in page
    assert "not watching anything yet" not in page


def test_watching_a_series_and_a_film_groups_them(client: TestClient) -> None:
    _watch_series(client)
    client.post("/watches/productions", data={"media_type": "movie", "tmdb_id": 37724})

    page = client.get("/watchlist").text

    assert ">Films</h2>" in page
    assert ">Series</h2>" in page
    assert "Skyfall" in page
    assert "Warehouse 13" in page
    assert ">People</h2>" not in page


def test_watching_twice_does_not_duplicate(client: TestClient) -> None:
    _watch_person(client)
    _watch_person(client)

    assert len(_watch_ids(client.get("/watchlist").text)) == 1


def test_the_details_come_from_tmdb_and_not_from_the_form(client: TestClient) -> None:
    client.post(
        "/watches/people",
        data={"tmdb_id": EDDIE.tmdb_id, "name": "<script>x</script> Forged"},
    )

    page = client.get("/watchlist").text

    assert "Eddie McClintock" in page
    assert "Forged" not in page


def test_the_watch_goes_back_to_the_search_that_was_open(client: TestClient) -> None:
    response = client.post(
        "/watches/people",
        data={"tmdb_id": EDDIE.tmdb_id, "return_to": "search", "q": "ed die&x=1"},
    )

    assert response.headers["location"] == "/search?q=ed+die%26x%3D1"


@pytest.mark.parametrize("return_to", ["https://evil.example", "//evil.example", "/x"])
def test_only_known_pages_can_be_returned_to(
    client: TestClient, return_to: str
) -> None:
    response = client.post(
        "/watches/people", data={"tmdb_id": EDDIE.tmdb_id, "return_to": return_to}
    )

    assert response.status_code == 422


# --- search shows what is watched -----------------------------------------------


def test_search_offers_watch_until_watched_then_stop(client: TestClient) -> None:
    before = client.get("/search?q=ed", headers={"HX-Request": "true"}).text
    assert before.count(">Watch</button>") == 3
    assert "Watching" not in before

    _watch_person(client)
    _watch_series(client)
    after = client.get("/search?q=ed", headers={"HX-Request": "true"}).text

    assert after.count(">Watch</button>") == 1
    assert after.count("&#10003; Watching") == 2
    assert len(_watch_ids(after)) == 2


def test_a_film_and_a_series_with_the_same_id_are_not_confused(
    client: TestClient, fake: FakeMetadata
) -> None:
    twin = Production(
        tmdb_id=SKYFALL.tmdb_id, media_type="tv", title="Skyfall The Series"
    )

    async def search(_query: str) -> list[Production]:
        return [SKYFALL, twin]

    async def lookup(media_type: MediaType, tmdb_id: int) -> Production | None:
        return twin if media_type == "tv" else SKYFALL

    fake.search_productions = search  # type: ignore[method-assign, assignment]
    fake.production = lookup  # type: ignore[method-assign]
    client.post("/watches/productions", data={"media_type": "tv", "tmdb_id": "37724"})

    page = client.get("/search?q=sky", headers={"HX-Request": "true"}).text

    film_card = page.split(">Films</h2>")[1].split(">Series</h2>")[0]
    series_card = page.split(">Series</h2>")[1]
    assert ">Watch</button>" in film_card
    assert "&#10003; Watching" in series_card


# --- stopping -------------------------------------------------------------------


def test_stopping_removes_it_and_returns_to_the_watchlist(client: TestClient) -> None:
    _watch_person(client)
    watch_id = _watch_ids(client.get("/watchlist").text)[0]

    response = client.post(f"/watches/{watch_id}/delete")

    assert response.status_code == 303
    assert response.headers["location"] == "/watchlist"
    assert _watch_ids(client.get("/watchlist").text) == []


def test_stopping_from_search_goes_back_to_that_search(client: TestClient) -> None:
    _watch_person(client)
    watch_id = _watch_ids(client.get("/watchlist").text)[0]

    response = client.post(
        f"/watches/{watch_id}/delete", data={"return_to": "search", "q": "eddie"}
    )

    assert response.headers["location"] == "/search?q=eddie"


def test_stopping_something_that_is_not_there_is_a_404(client: TestClient) -> None:
    response = client.post(f"/watches/{uuid.uuid4()}/delete")

    assert response.status_code == 404
    assert "not found" in response.text


def test_a_badly_formed_watch_id_is_a_422(client: TestClient) -> None:
    assert client.post("/watches/not-a-uuid/delete").status_code == 422


# --- the WP-2 gate: isolation and persistence -----------------------------------


def test_each_user_has_their_own_watchlist(
    seeded: Settings, fake: FakeMetadata
) -> None:
    with _client(seeded, fake) as alice, _client(seeded, fake) as bob:
        _sign_in(alice, "alice")
        _sign_in(bob, "bob")
        _watch_person(alice)
        bob.post("/watches/productions", data={"media_type": "movie", "tmdb_id": 37724})

        alice_page = alice.get("/watchlist").text
        bob_page = bob.get("/watchlist").text

    assert "Eddie McClintock" in alice_page
    assert "Skyfall" not in alice_page
    assert "Skyfall" in bob_page
    assert "Eddie McClintock" not in bob_page


def test_another_users_watch_is_a_404_and_is_left_alone(
    seeded: Settings, fake: FakeMetadata, database_url: str
) -> None:
    with _client(seeded, fake) as alice, _client(seeded, fake) as bob:
        _sign_in(alice, "alice")
        _sign_in(bob, "bob")
        _watch_person(alice)
        alices_watch = _watch_ids(alice.get("/watchlist").text)[0]

        attempt = bob.post(f"/watches/{alices_watch}/delete")
        missing = bob.post(f"/watches/{uuid.uuid4()}/delete")

        assert attempt.status_code == 404
        assert attempt.text == missing.text
        assert _watch_ids(alice.get("/watchlist").text) == [alices_watch]
    assert _watch_count(database_url) == 1


def test_watches_survive_a_restart(
    seeded: Settings, fake: FakeMetadata, database_url: str
) -> None:
    with _client(seeded, fake) as first:
        _sign_in(first, "alice")
        _watch_person(first)
        _watch_series(first)

    with _client(seeded, fake) as restarted:
        _sign_in(restarted, "alice")
        page = restarted.get("/watchlist").text

    assert "Eddie McClintock" in page
    assert "Warehouse 13" in page
    assert len(_watch_ids(page)) == 2
    assert _watch_count(database_url) == 2


# --- lookups that go wrong ------------------------------------------------------


def test_an_unknown_person_or_title_is_a_404_and_adds_nothing(
    client: TestClient, database_url: str
) -> None:
    person = client.post("/watches/people", data={"tmdb_id": 999})
    production = client.post(
        "/watches/productions", data={"media_type": "movie", "tmdb_id": 999}
    )

    assert person.status_code == 404
    assert "no such person" in person.text
    assert production.status_code == 404
    assert "no such film or series" in production.text
    assert _watch_count(database_url) == 0


def test_a_tmdb_failure_says_so_and_adds_nothing(
    client: TestClient, fake: FakeMetadata, database_url: str
) -> None:
    fake.error = ProviderError("TMDB is limiting requests.")

    person = _watch_person(client)
    production = _watch_series(client)

    for response in (person, production):
        assert response.status_code == 502  # type: ignore[attr-defined]
        assert "unavailable" in response.text  # type: ignore[attr-defined]
        assert "limiting" not in response.text  # type: ignore[attr-defined]
    assert _watch_count(database_url) == 0


def test_without_tmdb_nothing_can_be_added(seeded: Settings, database_url: str) -> None:
    with TestClient(
        create_app(seeded), follow_redirects=False, headers=SAME_ORIGIN
    ) as client:
        _sign_in(client, "alice")

        person = _watch_person(client)
        production = _watch_series(client)

    assert person.status_code == production.status_code == 503  # type: ignore[attr-defined]
    assert _watch_count(database_url) == 0


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"tmdb_id": "abc"},
        {"tmdb_id": "0"},
        {"tmdb_id": "-5"},
        {"tmdb_id": "1", "q": "x" * 101},
    ],
)
def test_a_bad_form_is_refused(
    client: TestClient, fake: FakeMetadata, data: dict[str, str]
) -> None:
    response = client.post("/watches/people", data=data)

    assert response.status_code == 422
    assert fake.lookups == 0


def test_an_unknown_media_type_is_refused(client: TestClient) -> None:
    response = client.post(
        "/watches/productions", data={"media_type": "person", "tmdb_id": "1"}
    )

    assert response.status_code == 422


# --- csrf and escaping ----------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    ["/watches/people", "/watches/productions", f"/watches/{uuid.uuid4()}/delete"],
)
def test_a_cross_site_post_is_refused(client: TestClient, url: str) -> None:
    response = client.post(
        url,
        data={"tmdb_id": "1", "media_type": "tv"},
        headers={"Sec-Fetch-Site": "cross-site"},
    )

    assert response.status_code == 403


def test_titles_on_the_watchlist_are_escaped(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    evil = Production(
        tmdb_id=666, media_type="movie", title="<script>alert(1)</script>"
    )

    async def fake_production(media_type: str, tmdb_id: int) -> Production:
        return evil

    client.app.state.metadata.production = fake_production  # type: ignore[attr-defined]
    client.post("/watches/productions", data={"media_type": "movie", "tmdb_id": "666"})

    page = client.get("/watchlist").text

    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


# --- navigation and dashboard ---------------------------------------------------


def test_the_nav_links_to_the_watchlist_and_marks_it(client: TestClient) -> None:
    page = client.get("/watchlist").text

    assert 'href="/watchlist" aria-current="page"' in page


def test_the_dashboard_counts_what_is_watched(client: TestClient) -> None:
    assert "Nothing is being tracked yet" in client.get("/").text
    _watch_person(client)
    _watch_series(client)
    client.post("/watches/productions", data={"media_type": "movie", "tmdb_id": 37724})

    page = client.get("/").text

    assert "watching 1 person and 2 films and series" in re.sub(r"\s+", " ", page)
    assert 'href="/watchlist"' in page
