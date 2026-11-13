import asyncio
import uuid
from datetime import timedelta
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
from casttrail.discovery.links import RosterconLinks
from casttrail.discovery.models import SourceLookup
from casttrail.main import create_app
from casttrail.providers.base import PersonDetail
from casttrail.providers.rostercon import PersonPage
from casttrail.watching.service import WatchService

if TYPE_CHECKING:
    from collections.abc import Iterator

    from casttrail.config import Settings

pytestmark = pytest.mark.integration

PASSWORD = "correct horse battery"
SAME_ORIGIN = {"Sec-Fetch-Site": "same-origin"}
ALEMBIC_INI = Path(__file__).parents[2] / "alembic.ini"
TOM_URL = "https://www.rostercon.com/en/people/tom-wilson-en"


class Facts:
    """Ids of the people the tests refer to."""

    def __init__(self) -> None:
        self.tom = uuid.UUID(int=0)
        self.paul = uuid.UUID(int=0)
        self.kirsten = uuid.UUID(int=0)
        self.unwatched = uuid.UUID(int=0)


async def _seed(database_url: str) -> Facts:
    facts = Facts()
    engine = create_engine(database_url)
    async with create_session_factory(engine)() as session:
        auth = AuthService(session, session_ttl=timedelta(hours=1))
        admin = await auth.create_user(
            email="admin@example.com",
            display_name="Admin",
            password=PASSWORD,
            is_admin=True,
        )
        await auth.create_user(
            email="user@example.com", display_name="User", password=PASSWORD
        )
        watches = WatchService(session)
        for number, name in enumerate(
            ("Thomas F. Wilson", "Paul McGann", "Kirsten Cooke"), start=1
        ):
            await watches.watch_person(
                admin.id, PersonDetail(tmdb_id=number, name=name)
            )
        unwatched = Person(name="Not Watched")
        session.add(unwatched)
        await session.commit()
        ids = {
            person.name: person.id for person in await session.scalars(select(Person))
        }
        facts.tom = ids["Thomas F. Wilson"]
        facts.paul = ids["Paul McGann"]
        facts.kirsten = ids["Kirsten Cooke"]
        facts.unwatched = ids["Not Watched"]
        links = RosterconLinks(session)
        await links.remember(
            facts.paul,
            "found",
            PersonPage(
                slug="paul-mcgann-en",
                url="https://www.rostercon.com/en/people/paul-mcgann-en",
            ),
        )
        await links.remember(facts.kirsten, "not_found")
    await engine.dispose()
    return facts


async def _rows(database_url: str) -> dict[uuid.UUID, tuple[str, str | None, bool]]:
    engine = create_engine(database_url)
    async with create_session_factory(engine)() as session:
        rows = {
            row.person_id: (row.outcome, row.source_key, row.manual)
            for row in await session.scalars(select(SourceLookup))
        }
    await engine.dispose()
    return rows


@pytest.fixture
def facts(database_url: str) -> Facts:
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")
    return asyncio.run(_seed(database_url))


@pytest.fixture
def client(settings: Settings, facts: Facts) -> Iterator[TestClient]:
    del facts
    with TestClient(
        create_app(settings), follow_redirects=False, headers=SAME_ORIGIN
    ) as test_client:
        yield test_client


def _sign_in(client: TestClient, email: str) -> None:
    client.cookies.clear()
    assert (
        client.post("/login", data={"email": email, "password": PASSWORD}).status_code
        == 303
    )


def _admin(client: TestClient) -> TestClient:
    _sign_in(client, "admin@example.com")
    return client


# --- access -----------------------------------------------------------------------


def test_every_page_needs_a_sign_in(client: TestClient, facts: Facts) -> None:
    form = {"person_id": str(facts.tom), "url": TOM_URL}

    assert client.get("/admin/links").status_code == 303
    assert client.post("/admin/links/set", data=form).status_code == 303
    assert client.post("/admin/links/remove", data=form).status_code == 303


def test_a_non_admin_gets_a_404_and_changes_nothing(
    client: TestClient, facts: Facts, database_url: str
) -> None:
    _sign_in(client, "user@example.com")
    form = {"person_id": str(facts.tom), "url": TOM_URL}

    assert client.get("/admin/links").status_code == 404
    assert client.post("/admin/links/set", data=form).status_code == 404
    assert client.post("/admin/links/remove", data=form).status_code == 404

    assert facts.tom not in asyncio.run(_rows(database_url))


def test_a_cross_site_post_is_refused(
    client: TestClient, facts: Facts, database_url: str
) -> None:
    _admin(client)

    response = client.post(
        "/admin/links/set",
        data={"person_id": str(facts.tom), "url": TOM_URL},
        headers={"Sec-Fetch-Site": "cross-site"},
    )

    assert response.status_code == 403
    assert facts.tom not in asyncio.run(_rows(database_url))


# --- the page ---------------------------------------------------------------------


def test_the_page_lists_each_watched_person_with_where_they_stand(
    client: TestClient,
) -> None:
    page = _admin(client).get("/admin/links").text

    assert "Not Watched" not in page
    assert page.index("Kirsten Cooke") < page.index("Paul McGann")
    assert page.index("Paul McGann") < page.index("Thomas F. Wilson")
    kirsten = page[page.index("Kirsten Cooke") :]
    assert "Not found by name" in kirsten[: kirsten.index("</tr>")]
    paul = page[page.index("Paul McGann") :]
    assert "Found by name" in paul[: paul.index("</tr>")]
    assert "paul-mcgann-en" in paul[: paul.index("</tr>")]
    tom = page[page.index("Thomas F. Wilson") :]
    assert "Not looked up yet" in tom[: tom.index("</tr>")]


def test_the_page_says_how_many_people_need_a_link(client: TestClient) -> None:
    page = _admin(client).get("/admin/links").text

    assert "1 person has no single page by name" in page
    assert 'href="/admin/links" aria-current="page"' in page


def test_an_unknown_notice_shows_nothing(client: TestClient) -> None:
    page = _admin(client).get("/admin/links?notice=<script>").text

    assert "<script>" not in page
    assert 'role="status"' not in page


# --- setting a link ---------------------------------------------------------------


def test_pasting_an_address_links_the_person_by_hand(
    client: TestClient, facts: Facts, database_url: str
) -> None:
    admin = _admin(client)

    response = admin.post(
        "/admin/links/set", data={"person_id": str(facts.tom), "url": TOM_URL}
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/links?notice=linked"
    assert asyncio.run(_rows(database_url))[facts.tom] == (
        "found",
        "tom-wilson-en",
        True,
    )
    page = admin.get(response.headers["location"]).text
    assert "Linked. The next refresh will use this page." in page
    tom = page[page.index("Thomas F. Wilson") :]
    row = tom[: tom.index("</tr>")]
    assert "Set by hand" in row
    assert 'href="https://www.rostercon.com/en/people/tom-wilson-en"' in row
    assert "Remove link" in row


def test_a_person_who_could_not_be_found_can_be_linked(
    client: TestClient, facts: Facts, database_url: str
) -> None:
    admin = _admin(client)

    admin.post(
        "/admin/links/set",
        data={
            "person_id": str(facts.kirsten),
            "url": "https://www.rostercon.com/en/people/kirsten-cooke-en",
        },
    )

    assert asyncio.run(_rows(database_url))[facts.kirsten] == (
        "found",
        "kirsten-cooke-en",
        True,
    )
    assert "no single page by name" not in admin.get("/admin/links").text


@pytest.mark.parametrize(
    "url",
    [
        "tom-wilson-en",
        "http://www.rostercon.com/en/people/tom-wilson-en",
        "https://evil.test/en/people/tom-wilson-en",
        "https://www.rostercon.com/en/event-convention/pensacon-2027",
    ],
)
def test_something_that_is_not_a_person_page_is_refused_and_stores_nothing(
    client: TestClient, facts: Facts, database_url: str, url: str
) -> None:
    admin = _admin(client)

    response = admin.post(
        "/admin/links/set", data={"person_id": str(facts.tom), "url": url}
    )

    assert response.headers["location"] == "/admin/links?notice=invalid"
    assert facts.tom not in asyncio.run(_rows(database_url))
    assert "Not a Rostercon person page" in admin.get(response.headers["location"]).text


def test_linking_a_person_who_does_not_exist_is_a_404(client: TestClient) -> None:
    response = _admin(client).post(
        "/admin/links/set", data={"person_id": str(uuid.uuid4()), "url": TOM_URL}
    )

    assert response.status_code == 404


@pytest.mark.parametrize("bad", ["", "not-a-uuid", "1"])
def test_a_malformed_person_id_is_refused(client: TestClient, bad: str) -> None:
    admin = _admin(client)

    assert (
        admin.post(
            "/admin/links/set", data={"person_id": bad, "url": TOM_URL}
        ).status_code
        == 422
    )
    assert admin.post("/admin/links/remove", data={"person_id": bad}).status_code == 422


def test_an_overlong_address_is_refused(client: TestClient, facts: Facts) -> None:
    response = _admin(client).post(
        "/admin/links/set",
        data={"person_id": str(facts.tom), "url": TOM_URL + "a" * 300},
    )

    assert response.status_code == 422


def test_linking_again_changes_the_page(
    client: TestClient, facts: Facts, database_url: str
) -> None:
    admin = _admin(client)
    admin.post("/admin/links/set", data={"person_id": str(facts.tom), "url": TOM_URL})

    admin.post(
        "/admin/links/set",
        data={
            "person_id": str(facts.tom),
            "url": "https://www.rostercon.com/en/people/tom-wilson-2-en",
        },
    )

    assert asyncio.run(_rows(database_url))[facts.tom][1] == "tom-wilson-2-en"
    assert "Change" in admin.get("/admin/links").text


# --- removing a link --------------------------------------------------------------


def test_removing_a_link_returns_the_person_to_lookup_by_name(
    client: TestClient, facts: Facts, database_url: str
) -> None:
    admin = _admin(client)
    admin.post("/admin/links/set", data={"person_id": str(facts.tom), "url": TOM_URL})

    response = admin.post("/admin/links/remove", data={"person_id": str(facts.tom)})

    assert response.headers["location"] == "/admin/links?notice=unlinked"
    assert facts.tom not in asyncio.run(_rows(database_url))
    assert (
        "look for this person by name again"
        in admin.get(response.headers["location"]).text
    )


def test_removing_never_touches_what_a_refresh_found(
    client: TestClient, facts: Facts, database_url: str
) -> None:
    response = _admin(client).post(
        "/admin/links/remove", data={"person_id": str(facts.paul)}
    )

    assert response.headers["location"] == "/admin/links?notice=none"
    assert asyncio.run(_rows(database_url))[facts.paul] == (
        "found",
        "paul-mcgann-en",
        False,
    )
