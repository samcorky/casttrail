import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select
from starlette.testclient import TestClient

from casttrail.auth.service import AuthService
from casttrail.catalogue.models import Person
from casttrail.db import create_engine, create_session_factory
from casttrail.discovery.browse import UK
from casttrail.discovery.models import Appearance, AppearanceSource, Convention
from casttrail.identity.decisions import DecisionService
from casttrail.identity.models import ExternalIdentity
from casttrail.main import create_app
from casttrail.matching.models import Match
from casttrail.matching.queries import relevant_appearances, watched_guests
from casttrail.matching.service import MatchService
from casttrail.providers.base import (
    CastMember,
    MediaType,
    PersonCredit,
    PersonDetail,
    PersonSummary,
    Production,
)
from casttrail.watching.models import Watch
from casttrail.watching.service import WatchService

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy.ext.asyncio import AsyncSession

    from casttrail.config import Settings

pytestmark = pytest.mark.integration

PASSWORD = "correct horse battery"
SAME_ORIGIN = {"Sec-Fetch-Site": "same-origin"}
ALEMBIC_INI = Path(__file__).parents[2] / "alembic.ini"
TODAY = datetime.now(UTC).date()
EDDIE = PersonDetail(tmdb_id=120679, name="Eddie McClintock")


class FakeMetadata:
    provider_id = "fake"

    async def search_people(self, query: str) -> list[PersonSummary]:
        return []

    async def search_productions(self, query: str) -> list[Production]:
        return []

    async def production(
        self, media_type: MediaType, tmdb_id: int
    ) -> Production | None:
        return None

    async def person(self, tmdb_id: int) -> PersonDetail | None:
        return EDDIE if tmdb_id == EDDIE.tmdb_id else None

    async def person_credits(self, tmdb_id: int) -> list[PersonCredit] | None:
        return None

    async def cast(
        self, media_type: MediaType, tmdb_id: int, *, limit: int = 10
    ) -> list[CastMember] | None:
        return None


async def _seed_async(database_url: str, *, match: bool = True) -> None:
    engine = create_engine(database_url)
    async with create_session_factory(engine)() as session:
        auth = AuthService(session, session_ttl=timedelta(hours=1))
        alice = await auth.create_user(
            email="alice@example.com", display_name="Alice", password=PASSWORD
        )
        await auth.create_user(
            email="bob@example.com", display_name="Bob", password=PASSWORD
        )
        alice_id = alice.id
        await WatchService(session).watch_person(alice_id, EDDIE)
        soon = Convention(
            id=uuid.uuid4(),
            country=UK,
            name="Soon Con",
            start_date=TODAY + timedelta(days=5),
            city="Lincoln",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        gone = Convention(
            id=uuid.uuid4(),
            country=UK,
            name="Gone Con",
            start_date=TODAY - timedelta(days=60),
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        undated = Convention(
            id=uuid.uuid4(),
            country=UK,
            name="Undated Con",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            status="cancelled",
        )
        session.add_all([soon, gone, undated])
        await session.flush()
        identity = ExternalIdentity(
            id=uuid.uuid4(),
            provider="rostercon",
            external_id="eddie-en",
            display_name="Eddie McClintock",
        )
        session.add(identity)
        await session.flush()
        for con in (soon, gone, undated):
            appearance = Appearance(
                id=uuid.uuid4(), convention_id=con.id, external_identity_id=identity.id
            )
            session.add(appearance)
            await session.flush()
            session.add(
                AppearanceSource(
                    id=uuid.uuid4(),
                    appearance_id=appearance.id,
                    provider="rostercon",
                    source_url="https://www.rostercon.com/en/people/eddie-en",
                )
            )
        await session.commit()
        if match:
            await MatchService(session).rematch()
    await engine.dispose()


def _seed(database_url: str, *, match: bool = True) -> None:
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")
    asyncio.run(_seed_async(database_url, match=match))


def _sign_in(client: TestClient, email: str) -> None:
    client.cookies.clear()
    response = client.post("/login", data={"email": email, "password": PASSWORD})
    assert response.status_code == 303


@pytest.fixture
def client(settings: Settings, database_url: str) -> Iterator[TestClient]:
    _seed(database_url)
    with TestClient(
        create_app(settings), follow_redirects=False, headers=SAME_ORIGIN
    ) as test_client:
        yield test_client


def test_the_page_needs_a_sign_in(client: TestClient) -> None:
    response = client.get("/appearances")

    assert (response.status_code, response.headers["location"]) == (303, "/login")


def test_a_user_sees_their_own_matches_soonest_first_with_the_reason(
    client: TestClient,
) -> None:
    _sign_in(client, "alice@example.com")

    page = client.get("/appearances").text

    assert "Eddie McClintock" in page
    assert page.index("Soon Con") < page.index("Undated Con")
    assert "Gone Con" not in page
    assert "Possible" in page
    assert "Same name only, so it may be someone else." in page
    assert "Date unknown" in page
    assert "(<strong>Cancelled</strong>)" in page
    assert 'href="/events/' in page
    assert 'href="/appearances" aria-current="page"' in page


def test_another_user_sees_none_of_it(client: TestClient) -> None:
    _sign_in(client, "bob@example.com")

    page = client.get("/appearances").text

    assert "Eddie McClintock" not in page
    assert "Soon Con" not in page
    assert "No appearances found yet" in page
    assert "wait for the next refresh" in page
    assert "/admin/refresh" not in page


def test_an_admin_with_nothing_is_pointed_at_the_refresh(
    settings: Settings, database_url: str
) -> None:
    _seed(database_url)
    config_user = TestClient(
        create_app(settings), follow_redirects=False, headers=SAME_ORIGIN
    )
    with config_user as client:
        asyncio.run(_make_admin(database_url))
        _sign_in(client, "bob@example.com")
        page = client.get("/appearances").text

    assert 'href="/admin/refresh"' in page


async def _make_admin(database_url: str) -> None:
    engine = create_engine(database_url)
    async with create_session_factory(engine)() as session:
        from casttrail.auth.models import User

        user = await session.scalar(select(User).where(User.email == "bob@example.com"))
        assert user is not None
        user.is_admin = True
        await session.commit()
    await engine.dispose()


# --- the query --------------------------------------------------------------------


async def _facts(database_url: str) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    engine = create_engine(database_url)
    async with create_session_factory(engine)() as session:
        alice = await session.scalar(select(Watch.user_id))
        person = await session.scalar(select(Person.id))
        identity = await session.scalar(
            select(ExternalIdentity.id).where(ExternalIdentity.provider == "rostercon")
        )
    await engine.dispose()
    assert alice is not None
    assert person is not None
    assert identity is not None
    return alice, person, identity


def test_confirming_a_guest_changes_the_label_on_the_page(
    settings: Settings, database_url: str
) -> None:
    _seed(database_url)
    alice_id, person_id, identity_id = asyncio.run(_facts(database_url))

    async def confirm() -> None:
        engine = create_engine(database_url)
        async with create_session_factory(engine)() as session:
            await DecisionService(session).confirm(identity_id, person_id, alice_id)
            await MatchService(session).rematch()
        await engine.dispose()

    asyncio.run(confirm())
    with TestClient(
        create_app(settings), follow_redirects=False, headers=SAME_ORIGIN
    ) as client:
        _sign_in(client, "alice@example.com")
        page = client.get("/appearances").text

    assert "Confirmed" in page
    assert "Confirmed in Review guests." in page
    assert "Same name only, so it may be someone else." not in page


async def test_the_query_orders_confirmed_before_possible_on_one_day(
    db_session: AsyncSession,
) -> None:
    auth = AuthService(db_session, session_ttl=timedelta(hours=1))
    user = await auth.create_user(
        email="u@example.com", display_name="U", password=PASSWORD
    )
    user_id = user.id
    con = Convention(
        id=uuid.uuid4(),
        country=UK,
        name="C",
        start_date=TODAY,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    db_session.add(con)
    await db_session.flush()
    for name, kind in (("Zed", "confirmed"), ("Amy", "possible")):
        person = Person(id=uuid.uuid4(), name=name)
        identity = ExternalIdentity(
            id=uuid.uuid4(), provider="rostercon", external_id=name
        )
        db_session.add_all([person, identity])
        await db_session.flush()
        appearance = Appearance(
            id=uuid.uuid4(), convention_id=con.id, external_identity_id=identity.id
        )
        db_session.add(appearance)
        await db_session.flush()
        db_session.add(
            Match(
                user_id=user_id,
                appearance_id=appearance.id,
                person_id=person.id,
                match_key=f"person:{person.id}",
                kind=kind,
                reason="r",
            )
        )
    await db_session.commit()

    rows = await relevant_appearances(db_session, user_id, today=TODAY)

    assert [(r.person_name, r.kind) for r in rows] == [
        ("Zed", "confirmed"),
        ("Amy", "possible"),
    ]


# --- matches stay current ---------------------------------------------------------


def test_watching_a_person_from_the_site_matches_at_once(
    settings: Settings, database_url: str
) -> None:
    _seed(database_url, match=False)
    with TestClient(
        create_app(settings, metadata_provider=FakeMetadata()),
        follow_redirects=False,
        headers=SAME_ORIGIN,
    ) as client:
        _sign_in(client, "bob@example.com")
        assert "Eddie McClintock" not in client.get("/appearances").text

        response = client.post(
            "/watches/people", data={"tmdb_id": EDDIE.tmdb_id, "return_to": "search"}
        )
        assert response.status_code == 303
        page = client.get("/appearances").text

        assert "Eddie McClintock" in page
        _sign_in(client, "bob@example.com")
        watches = client.get("/watchlist").text
        assert "Eddie McClintock" in watches


def test_unwatching_a_person_removes_their_matches_at_once(
    settings: Settings, database_url: str
) -> None:
    _seed(database_url)
    watch_id = asyncio.run(_watch_id(database_url))
    with TestClient(
        create_app(settings), follow_redirects=False, headers=SAME_ORIGIN
    ) as client:
        _sign_in(client, "alice@example.com")
        assert "Eddie McClintock" in client.get("/appearances").text

        response = client.post(
            f"/watches/{watch_id}/delete", data={"return_to": "watchlist"}
        )
        assert response.status_code == 303

        assert "Eddie McClintock" not in client.get("/appearances").text


async def _watch_id(database_url: str) -> uuid.UUID:
    engine = create_engine(database_url)
    async with create_session_factory(engine)() as session:
        found: uuid.UUID | None = await session.scalar(select(Watch.id))
    await engine.dispose()
    assert found is not None
    return found


# --- the events list names who you watch ------------------------------------------


def test_the_events_list_counts_and_names_the_people_you_watch(
    client: TestClient,
) -> None:
    _sign_in(client, "alice@example.com")

    page = client.get("/events?scope=all").text

    assert '<th class="text-end">Watching</th>' in page
    assert "You watch:" in page
    assert "Eddie McClintock" in page
    assert "(possible)" in page
    assert '<span class="badge text-bg-primary">1</span>' in page


def test_the_events_list_shows_no_one_else_s_watches(client: TestClient) -> None:
    _sign_in(client, "bob@example.com")

    page = client.get("/events?scope=all").text

    assert "Soon Con" in page
    assert "You watch:" not in page
    assert "badge text-bg-primary" not in page


async def test_watched_guests_counts_a_person_once_and_prefers_confirmed(
    db_session: AsyncSession,
) -> None:
    auth = AuthService(db_session, session_ttl=timedelta(hours=1))
    user = await auth.create_user(
        email="u@example.com", display_name="U", password=PASSWORD
    )
    other = await auth.create_user(
        email="o@example.com", display_name="O", password=PASSWORD
    )
    user_id, other_id = user.id, other.id
    con = Convention(
        id=uuid.uuid4(),
        country=UK,
        name="C",
        start_date=TODAY,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    quiet = Convention(
        id=uuid.uuid4(),
        country=UK,
        name="Q",
        start_date=TODAY,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    zed = Person(id=uuid.uuid4(), name="Zed")
    amy = Person(id=uuid.uuid4(), name="Amy")
    db_session.add_all([con, quiet, zed, amy])
    await db_session.flush()
    # Zed is listed by two sources: possible in one, confirmed in the other. Amy is
    # possible. Another user's match must not show.
    for key, person, kind, owner in (
        ("zed-r", zed, "possible", user_id),
        ("zed-c", zed, "confirmed", user_id),
        ("amy", amy, "possible", user_id),
        ("amy-o", amy, "confirmed", other_id),
    ):
        identity = ExternalIdentity(
            id=uuid.uuid4(), provider="rostercon", external_id=key
        )
        db_session.add(identity)
        await db_session.flush()
        appearance = Appearance(
            id=uuid.uuid4(), convention_id=con.id, external_identity_id=identity.id
        )
        db_session.add(appearance)
        await db_session.flush()
        db_session.add(
            Match(
                user_id=owner,
                appearance_id=appearance.id,
                person_id=person.id,
                match_key=f"person:{person.id}",
                kind=kind,
                reason="r",
            )
        )
    await db_session.commit()

    found = await watched_guests(db_session, user_id, [con.id, quiet.id])

    assert [(g.name, g.kind) for g in found[con.id]] == [
        ("Zed", "confirmed"),
        ("Amy", "possible"),
    ]
    assert quiet.id not in found
    assert await watched_guests(db_session, user_id, []) == {}


# --- UK only, unless asked for all -------------------------------------------------


async def _matched_at(
    session: AsyncSession, user_id: uuid.UUID, countries: dict[str, str | None]
) -> None:
    """Match one watched person at a convention in each named country."""
    person = Person(id=uuid.uuid4(), name="Eddie")
    session.add(person)
    for name, country in countries.items():
        con = Convention(
            id=uuid.uuid4(),
            country=country,
            name=name,
            start_date=TODAY,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        identity = ExternalIdentity(
            id=uuid.uuid4(), provider="rostercon", external_id=name
        )
        session.add_all([con, identity])
        await session.flush()
        appearance = Appearance(
            id=uuid.uuid4(), convention_id=con.id, external_identity_id=identity.id
        )
        session.add(appearance)
        await session.flush()
        session.add(
            Match(
                user_id=user_id,
                appearance_id=appearance.id,
                person_id=person.id,
                match_key=f"person:{person.id}",
                kind="possible",
                reason="r",
            )
        )
    await session.commit()


async def test_only_uk_appearances_are_listed_unless_all_are_asked_for(
    db_session: AsyncSession,
) -> None:
    auth = AuthService(db_session, session_ttl=timedelta(hours=1))
    user = await auth.create_user(
        email="u@example.com", display_name="U", password=PASSWORD
    )
    countries = {"Here Con": UK, "Away Con": "united-states", "Somewhere Con": None}
    await _matched_at(db_session, user.id, countries)

    uk = await relevant_appearances(db_session, user.id, today=TODAY)
    everywhere = await relevant_appearances(
        db_session, user.id, today=TODAY, uk_only=False
    )

    # As on the events page, a convention with no known country is not called UK.
    assert [r.convention.name for r in uk] == ["Here Con"]
    assert sorted(r.convention.name for r in everywhere) == sorted(countries)


def test_the_page_switches_between_uk_and_all_countries(client: TestClient) -> None:
    _sign_in(client, "alice@example.com")

    uk = client.get("/appearances").text
    everywhere = client.get("/appearances", params={"scope": "all"}).text

    assert "My UK appearances" in uk
    assert 'href="/appearances?scope=all">Show all countries' in uk
    assert "My appearances everywhere" in everywhere
    assert 'href="/appearances">Show UK only' in everywhere
    assert client.get("/appearances", params={"scope": "mars"}).status_code == 422


def test_an_empty_uk_list_offers_all_countries(client: TestClient) -> None:
    _sign_in(client, "bob@example.com")

    page = client.get("/appearances").text

    assert "No appearances found yet in the UK." in page
    assert 'href="/appearances?scope=all">show all countries' in page
