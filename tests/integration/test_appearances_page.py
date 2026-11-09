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
from casttrail.discovery.models import Appearance, AppearanceSource, Convention
from casttrail.identity.decisions import DecisionService
from casttrail.identity.models import ExternalIdentity
from casttrail.main import create_app
from casttrail.matching.models import Match
from casttrail.matching.queries import relevant_appearances
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
            name="Soon Con",
            start_date=TODAY + timedelta(days=5),
            city="Lincoln",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        gone = Convention(
            id=uuid.uuid4(),
            name="Gone Con",
            start_date=TODAY - timedelta(days=60),
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        undated = Convention(
            id=uuid.uuid4(),
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
    assert "Same name." in page
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
    assert "Linked by hand." in page
    assert "Same name." not in page


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
