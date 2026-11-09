import asyncio
import logging
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from alembic import command
from alembic.config import Config
from pydantic import SecretStr
from starlette.testclient import TestClient

from casttrail.auth.service import AuthService
from casttrail.config import Settings
from casttrail.db import create_engine, create_session_factory
from casttrail.main import create_app
from casttrail.providers.base import (
    CastMember,
    MediaType,
    MetadataProvider,
    PersonCredit,
    PersonDetail,
    PersonSummary,
    Production,
    ProviderError,
)
from casttrail.providers.tmdb import TmdbProvider

if TYPE_CHECKING:
    from collections.abc import Iterator

    import httpx2

pytestmark = pytest.mark.integration

PASSWORD = "correct horse battery"
SAME_ORIGIN = {"Sec-Fetch-Site": "same-origin"}
HTMX = {"HX-Request": "true"}
ALEMBIC_INI = Path(__file__).parents[2] / "alembic.ini"


class FakeMetadata:
    """A stand-in for TMDB that serves canned answers and remembers the questions."""

    provider_id = "fake"

    def __init__(self) -> None:
        self.people = [
            PersonSummary(
                tmdb_id=1,
                name="Eddie McClintock",
                known_for_department="Acting",
                profile_path="/face1.jpg",
            ),
            PersonSummary(tmdb_id=2, name="Eddie Nobody"),
        ]
        self.productions = [
            Production(
                tmdb_id=10,
                media_type="movie",
                title="Skyfall",
                release_year=2012,
                poster_path="/poster1.jpg",
            ),
            Production(
                tmdb_id=20, media_type="tv", title="Warehouse 13", release_year=2009
            ),
            Production(tmdb_id=21, media_type="tv", title="Undated Show"),
        ]
        self.error: ProviderError | None = None
        self.queries: list[tuple[str, str]] = []

    async def search_people(self, query: str) -> list[PersonSummary]:
        self.queries.append(("people", query))
        if self.error:
            raise self.error
        return self.people

    async def search_productions(self, query: str) -> list[Production]:
        self.queries.append(("productions", query))
        if self.error:
            raise self.error
        return self.productions

    async def production(
        self, media_type: MediaType, tmdb_id: int
    ) -> Production | None:
        return None

    async def person(self, tmdb_id: int) -> PersonDetail | None:
        return None

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
            await auth.create_user(
                email="a@example.com", display_name="Alice", password=PASSWORD
            )
        await engine.dispose()

    asyncio.run(run())


@pytest.fixture
def fake() -> FakeMetadata:
    return FakeMetadata()


@pytest.fixture
def settings_with_users(settings: Settings, database_url: str) -> Settings:
    _seed(database_url)
    return settings


@pytest.fixture
def client(settings_with_users: Settings, fake: FakeMetadata) -> Iterator[TestClient]:
    app = create_app(settings_with_users, metadata_provider=fake)
    with TestClient(app, follow_redirects=False, headers=SAME_ORIGIN) as test_client:
        assert (
            test_client.post(
                "/login", data={"email": "a@example.com", "password": PASSWORD}
            ).status_code
            == 303
        )
        yield test_client


def test_search_requires_a_sign_in(settings_with_users: Settings) -> None:
    with TestClient(
        create_app(settings_with_users, metadata_provider=FakeMetadata()),
        follow_redirects=False,
    ) as anonymous:
        page = anonymous.get("/search?q=eddie")
        fragment = anonymous.get("/search?q=eddie", headers=HTMX)

    assert (page.status_code, page.headers["location"]) == (303, "/login")
    assert fragment.status_code == 303


def test_the_page_has_a_form_that_works_without_javascript(client: TestClient) -> None:
    page = client.get("/search").text

    assert '<form method="get" action="/search"' in page
    assert 'name="q"' in page
    assert 'aria-label="Search people, films and series"' in page
    assert 'hx-get="/search"' in page
    assert 'hx-trigger="input changed delay:400ms' in page


def test_an_empty_search_asks_nothing_and_shows_no_message(
    client: TestClient, fake: FakeMetadata
) -> None:
    page = client.get("/search")

    assert page.status_code == 200
    assert fake.queries == []
    assert 'role="status"' not in page.text


def test_results_are_grouped_into_people_films_and_series(client: TestClient) -> None:
    page = client.get("/search?q=eddie").text

    for heading in ("People", "Films", "Series"):
        assert f">{heading}</h2>" in page
    assert "Eddie McClintock" in page
    assert "<small>Acting</small>" in page
    assert "Skyfall" in page
    assert "<small>2012</small>" in page
    assert "Warehouse 13" in page
    assert "<small>2009</small>" in page


def test_a_missing_year_is_simply_left_out(client: TestClient) -> None:
    page = client.get("/search?q=show").text

    row = page.split("Undated Show")[1].split("</li>")[0]
    assert "<small>" not in row
    assert "None" not in page


def test_the_query_is_trimmed_and_both_kinds_are_searched(
    client: TestClient, fake: FakeMetadata
) -> None:
    client.get("/search", params={"q": "  eddie  "})

    assert sorted(fake.queries) == [("people", "eddie"), ("productions", "eddie")]


def test_an_htmx_request_gets_only_the_results(client: TestClient) -> None:
    fragment = client.get("/search?q=eddie", headers=HTMX)
    page = client.get("/search?q=eddie")

    assert "<html" not in fragment.text
    assert 'aria-label="Main navigation"' not in fragment.text
    assert "Eddie McClintock" in fragment.text
    assert "<html" in page.text
    assert fragment.headers["Vary"] == "HX-Request"
    assert page.headers["Vary"] == "HX-Request"


def test_the_page_carries_the_tmdb_attribution_and_a_nav_link(
    client: TestClient,
) -> None:
    page = client.get("/search").text

    assert "uses the TMDB API but is not endorsed or certified by TMDB" in page
    assert 'href="/search" aria-current="page"' in page


@pytest.mark.parametrize("query", ["a", "x"])
def test_a_very_short_query_asks_for_more_and_does_not_search(
    client: TestClient, fake: FakeMetadata, query: str
) -> None:
    page = client.get("/search", params={"q": query}).text

    assert "Type at least 2 characters." in page
    assert fake.queries == []


def test_a_very_long_query_is_refused(client: TestClient, fake: FakeMetadata) -> None:
    page = client.get("/search", params={"q": "x" * 101}).text

    assert "at most 100 characters" in page
    assert fake.queries == []


def test_no_matches_says_so(client: TestClient, fake: FakeMetadata) -> None:
    fake.people, fake.productions = [], []

    page = client.get("/search?q=zzzz").text

    assert "Nothing found for “zzzz”." in page
    assert ">People</h2>" not in page


def test_a_provider_failure_is_a_calm_message_and_is_logged(
    client: TestClient, fake: FakeMetadata, caplog: pytest.LogCaptureFixture
) -> None:
    fake.error = ProviderError("TMDB is limiting requests.")

    with caplog.at_level(logging.WARNING, logger="casttrail.web.search_routes"):
        response = client.get("/search?q=eddie", headers=HTMX)

    assert response.status_code == 200
    assert "Search is unavailable right now" in response.text
    assert "limiting requests" not in response.text
    assert "TMDB is limiting requests." in caplog.text


def test_names_from_tmdb_are_escaped(client: TestClient, fake: FakeMetadata) -> None:
    fake.people = [PersonSummary(tmdb_id=3, name="<script>alert(1)</script>")]
    fake.productions = [
        Production(
            tmdb_id=4, media_type="movie", title='"><img src=x onerror=alert(1)>'
        )
    ]

    page = client.get("/search?q=evil").text

    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "<img src=x" not in page


def test_the_query_is_escaped_when_echoed_back(client: TestClient) -> None:
    page = client.get("/search", params={"q": '"><script>alert(1)</script>'}).text

    assert "<script>alert(1)</script>" not in page


def test_search_is_switched_off_when_tmdb_is_not_configured(
    settings_with_users: Settings,
) -> None:
    app = create_app(settings_with_users)
    with TestClient(app, follow_redirects=False, headers=SAME_ORIGIN) as client:
        client.post("/login", data={"email": "a@example.com", "password": PASSWORD})

        assert app.state.metadata is None
        page = client.get("/search?q=eddie").text

    assert "Search is not set up" in page


def test_the_app_builds_the_tmdb_provider_from_the_token_and_closes_it(
    settings_with_users: Settings,
) -> None:
    configured = Settings(
        database_url=settings_with_users.database_url,
        tmdb_read_token=SecretStr("a-test-token"),
    )
    app = create_app(configured)

    with TestClient(app):
        provider = app.state.metadata
        assert isinstance(provider, TmdbProvider)
        client: httpx2.AsyncClient = provider._client
        assert client.headers["Authorization"] == "Bearer a-test-token"
        assert not client.is_closed

    assert client.is_closed


def test_an_injected_provider_is_used_even_when_a_token_is_set(
    settings_with_users: Settings, fake: FakeMetadata
) -> None:
    configured = Settings(
        database_url=settings_with_users.database_url,
        tmdb_read_token=SecretStr("a-test-token"),
    )
    app = create_app(configured, metadata_provider=fake)

    with TestClient(app):
        assert app.state.metadata is fake


def test_the_token_never_appears_in_the_settings_repr() -> None:
    settings = Settings(tmdb_read_token=SecretStr("a-test-token"))

    assert "a-test-token" not in repr(settings)
    assert "a-test-token" not in str(settings)


def test_the_fake_satisfies_the_protocol(fake: FakeMetadata) -> None:
    provider: MetadataProvider = fake

    assert provider.provider_id == "fake"


# --- pictures -------------------------------------------------------------------


def test_posters_and_photos_load_from_tmdb_without_a_referrer(
    client: TestClient,
) -> None:
    page = client.get("/search?q=eddie", headers=HTMX).text

    assert 'src="https://image.tmdb.org/t/p/w185/face1.jpg"' in page
    assert 'src="https://image.tmdb.org/t/p/w185/poster1.jpg"' in page
    assert page.count("<img ") == page.count('referrerpolicy="no-referrer"')
    assert page.count("<img ") == page.count('loading="lazy"')


def test_a_result_with_no_picture_shows_its_first_letter(client: TestClient) -> None:
    page = client.get("/search?q=eddie", headers=HTMX).text

    assert '<div class="no-image" aria-hidden="true">E</div>' in page
    assert '<div class="no-image" aria-hidden="true">W</div>' in page


@pytest.mark.parametrize(
    "path",
    [
        "//evil.example/x.jpg",
        "/../secret.jpg",
        "/a b.jpg",
        "javascript:alert(1)",
        "/x.jpg?then=something",
        'x.jpg"onerror="alert(1)',
        "",
    ],
)
def test_an_odd_image_path_is_never_put_in_a_url(
    client: TestClient, fake: FakeMetadata, path: str
) -> None:
    fake.people = [PersonSummary(tmdb_id=3, name="Odd", profile_path=path)]
    fake.productions = []

    page = client.get("/search?q=odd", headers=HTMX).text

    assert "<img " not in page
    assert "evil.example" not in page
    assert "onerror" not in page
    assert '<div class="no-image" aria-hidden="true">O</div>' in page


def test_the_stylesheet_is_served(client: TestClient) -> None:
    css = client.get("/static/css/app.css")

    assert css.status_code == 200
    assert ".no-image" in css.text
    assert "aspect-ratio" in css.text
    assert "/static/css/app.css" in client.get("/search").text
