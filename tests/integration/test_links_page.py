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
from casttrail.discovery.links import ROSTERCON, SourceLinks
from casttrail.discovery.models import SourceLookup
from casttrail.main import create_app
from casttrail.providers.base import LinkTarget, PersonDetail
from casttrail.watching.service import WatchService

if TYPE_CHECKING:
    from collections.abc import Iterator

    from httpx2 import Response

    from casttrail.config import Settings

pytestmark = pytest.mark.integration

PASSWORD = "correct horse battery"
SAME_ORIGIN = {"Sec-Fetch-Site": "same-origin"}
ALEMBIC_INI = Path(__file__).parents[2] / "alembic.ini"
TOM_URL = "https://www.rostercon.com/en/people/tom-wilson-en"
ANDREW_URL = "https://comiconomicon.com/guest/518/Andrew_Jackson"


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
        links = SourceLinks(session, ROSTERCON)
        await links.remember(
            facts.paul,
            "found",
            LinkTarget(
                key="paul-mcgann-en",
                url="https://www.rostercon.com/en/people/paul-mcgann-en",
            ),
        )
        await links.remember(facts.kirsten, "not_found")
    await engine.dispose()
    return facts


Row = tuple[str, str | None, bool]


async def _rows(database_url: str) -> dict[tuple[str, uuid.UUID], Row]:
    engine = create_engine(database_url)
    async with create_session_factory(engine)() as session:
        rows = {
            (row.provider, row.person_id): (row.outcome, row.source_key, row.manual)
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


def _set(
    client: TestClient, person: uuid.UUID, url: str, provider: str = "rostercon"
) -> Response:
    return client.post(
        "/admin/links/set",
        data={"provider": provider, "person_id": str(person), "url": url},
    )


def _remove(client: TestClient, person: uuid.UUID, provider: str) -> Response:
    return client.post(
        "/admin/links/remove",
        data={"provider": provider, "person_id": str(person)},
    )


def _row_of(page: str, name: str) -> str:
    start = page[page.index(name) :]
    return start[: start.index("</tr>")]


# --- access -----------------------------------------------------------------------


def test_every_page_needs_a_sign_in(client: TestClient, facts: Facts) -> None:
    form = {"provider": "rostercon", "person_id": str(facts.tom), "url": TOM_URL}

    assert client.get("/admin/links").status_code == 303
    assert client.post("/admin/links/set", data=form).status_code == 303
    assert client.post("/admin/links/remove", data=form).status_code == 303


def test_a_non_admin_gets_a_404_and_changes_nothing(
    client: TestClient, facts: Facts, database_url: str
) -> None:
    _sign_in(client, "user@example.com")

    assert client.get("/admin/links").status_code == 404
    assert _set(client, facts.tom, TOM_URL).status_code == 404
    assert _remove(client, facts.tom, "rostercon").status_code == 404

    assert ("rostercon", facts.tom) not in asyncio.run(_rows(database_url))


def test_a_cross_site_post_is_refused(
    client: TestClient, facts: Facts, database_url: str
) -> None:
    _admin(client)

    response = client.post(
        "/admin/links/set",
        data={"provider": "rostercon", "person_id": str(facts.tom), "url": TOM_URL},
        headers={"Sec-Fetch-Site": "cross-site"},
    )

    assert response.status_code == 403
    assert ("rostercon", facts.tom) not in asyncio.run(_rows(database_url))


# --- the page ---------------------------------------------------------------------


def test_the_page_has_a_column_for_each_source(client: TestClient) -> None:
    page = _admin(client).get("/admin/links").text

    header = page[page.index("<thead>") : page.index("</thead>")]
    assert header.index("Rostercon") < header.index("Comiconomicon")
    assert 'href="/admin/links" aria-current="page"' in page
    assert ">Source pages</a>" in page


def test_the_page_lists_each_watched_person_with_where_they_stand(
    client: TestClient,
) -> None:
    page = _admin(client).get("/admin/links").text

    assert "Not Watched" not in page
    assert page.index("Kirsten Cooke") < page.index("Paul McGann")
    assert page.index("Paul McGann") < page.index("Thomas F. Wilson")
    assert "Not found by name" in _row_of(page, "Kirsten Cooke")
    paul = _row_of(page, "Paul McGann")
    assert "Found by name" in paul
    assert "paul-mcgann-en" in paul
    # Nothing has looked on Comiconomicon yet, so Paul is unknown there.
    assert paul.count("Not looked up yet") == 1
    # Tom is unknown on both.
    assert _row_of(page, "Thomas F. Wilson").count("Not looked up yet") == 2


def test_the_page_says_which_source_needs_links(client: TestClient) -> None:
    page = _admin(client).get("/admin/links").text

    assert "Rostercon:</strong> 1 person has no single page by name" in page
    assert "Comiconomicon:</strong>" not in page


def test_an_unknown_notice_shows_nothing(client: TestClient) -> None:
    page = _admin(client).get("/admin/links?notice=<script>").text

    assert "<script>" not in page
    assert 'role="status"' not in page


def test_an_invalid_notice_for_an_unknown_source_shows_nothing(
    client: TestClient,
) -> None:
    page = _admin(client).get("/admin/links?notice=invalid&source=nowhere").text

    assert 'role="status"' not in page


# --- setting a link ---------------------------------------------------------------


def test_pasting_an_address_links_the_person_by_hand(
    client: TestClient, facts: Facts, database_url: str
) -> None:
    admin = _admin(client)

    response = _set(admin, facts.tom, TOM_URL)

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/links?notice=linked"
    assert asyncio.run(_rows(database_url))[("rostercon", facts.tom)] == (
        "found",
        "tom-wilson-en",
        True,
    )
    page = admin.get("/admin/links?notice=linked").text
    assert "Linked. The next refresh will use this page." in page
    row = _row_of(page, "Thomas F. Wilson")
    assert "Set by hand" in row
    assert 'href="https://www.rostercon.com/en/people/tom-wilson-en"' in row
    assert "Remove link" in row


def test_a_comiconomicon_page_can_be_linked_the_same_way(
    client: TestClient, facts: Facts, database_url: str
) -> None:
    admin = _admin(client)

    response = _set(admin, facts.kirsten, ANDREW_URL, "comiconomicon")

    assert response.headers["location"] == "/admin/links?notice=linked"
    rows = asyncio.run(_rows(database_url))
    assert rows[("comiconomicon", facts.kirsten)] == ("found", "518", True)
    # Her Rostercon answer is a different row and is left alone.
    assert rows[("rostercon", facts.kirsten)] == ("not_found", None, False)
    row = _row_of(admin.get("/admin/links").text, "Kirsten Cooke")
    assert "Set by hand" in row
    assert 'href="https://comiconomicon.com/guest/518/Andrew_Jackson"' in row


def test_a_person_who_could_not_be_found_can_be_linked(
    client: TestClient, facts: Facts, database_url: str
) -> None:
    admin = _admin(client)

    _set(admin, facts.kirsten, "https://www.rostercon.com/en/people/kirsten-cooke-en")

    assert asyncio.run(_rows(database_url))[("rostercon", facts.kirsten)] == (
        "found",
        "kirsten-cooke-en",
        True,
    )
    assert "Rostercon:</strong>" not in admin.get("/admin/links").text


@pytest.mark.parametrize(
    "url",
    [
        "tom-wilson-en",
        "http://www.rostercon.com/en/people/tom-wilson-en",
        "https://evil.test/en/people/tom-wilson-en",
        "https://www.rostercon.com/en/event-convention/pensacon-2027",
        # A page on the other source is not a Rostercon page.
        ANDREW_URL,
    ],
)
def test_something_that_is_not_a_rostercon_page_is_refused_and_stores_nothing(
    client: TestClient, facts: Facts, database_url: str, url: str
) -> None:
    admin = _admin(client)

    response = _set(admin, facts.tom, url)

    location = response.headers["location"]
    assert location == "/admin/links?notice=invalid&source=rostercon"
    assert ("rostercon", facts.tom) not in asyncio.run(_rows(database_url))
    page = admin.get(location).text
    assert "Not a Rostercon page. Paste its address, like" in page
    assert "https://www.rostercon.com/en/people/tom-wilson-en" in page


@pytest.mark.parametrize("url", [TOM_URL, "https://comiconomicon.com/event/42/Con"])
def test_something_that_is_not_a_comiconomicon_page_is_refused_and_stores_nothing(
    client: TestClient, facts: Facts, database_url: str, url: str
) -> None:
    admin = _admin(client)

    response = _set(admin, facts.tom, url, "comiconomicon")

    location = response.headers["location"]
    assert location == "/admin/links?notice=invalid&source=comiconomicon"
    assert ("comiconomicon", facts.tom) not in asyncio.run(_rows(database_url))
    assert "Not a Comiconomicon page." in admin.get(location).text


def test_an_unknown_source_is_a_404_and_changes_nothing(
    client: TestClient, facts: Facts, database_url: str
) -> None:
    admin = _admin(client)
    before = asyncio.run(_rows(database_url))

    assert _set(admin, facts.tom, TOM_URL, "wikipedia").status_code == 404
    assert _remove(admin, facts.tom, "wikipedia").status_code == 404

    assert asyncio.run(_rows(database_url)) == before


def test_linking_a_person_who_does_not_exist_is_a_404(client: TestClient) -> None:
    assert _set(_admin(client), uuid.uuid4(), TOM_URL).status_code == 404


@pytest.mark.parametrize("bad", ["", "not-a-uuid", "1"])
def test_a_malformed_person_id_is_refused(client: TestClient, bad: str) -> None:
    admin = _admin(client)
    form = {"provider": "rostercon", "person_id": bad}

    assert (
        admin.post("/admin/links/set", data={**form, "url": TOM_URL}).status_code == 422
    )
    assert admin.post("/admin/links/remove", data=form).status_code == 422


def test_a_request_without_a_source_is_refused(
    client: TestClient, facts: Facts
) -> None:
    response = _admin(client).post(
        "/admin/links/set", data={"person_id": str(facts.tom), "url": TOM_URL}
    )

    assert response.status_code == 422


def test_an_overlong_address_is_refused(client: TestClient, facts: Facts) -> None:
    assert _set(_admin(client), facts.tom, TOM_URL + "a" * 300).status_code == 422


def test_linking_again_changes_the_page(
    client: TestClient, facts: Facts, database_url: str
) -> None:
    admin = _admin(client)
    _set(admin, facts.tom, TOM_URL)

    _set(admin, facts.tom, "https://www.rostercon.com/en/people/tom-wilson-2-en")

    rows = asyncio.run(_rows(database_url))
    assert rows[("rostercon", facts.tom)][1] == "tom-wilson-2-en"
    assert "Change" in admin.get("/admin/links").text


# --- removing a link --------------------------------------------------------------


def test_removing_a_link_returns_the_person_to_lookup_by_name(
    client: TestClient, facts: Facts, database_url: str
) -> None:
    admin = _admin(client)
    _set(admin, facts.tom, TOM_URL)

    response = _remove(admin, facts.tom, "rostercon")

    assert response.headers["location"] == "/admin/links?notice=unlinked"
    assert ("rostercon", facts.tom) not in asyncio.run(_rows(database_url))
    page = admin.get(response.headers["location"]).text
    assert "look for this person by name again" in page


def test_removing_a_link_on_one_source_leaves_the_other(
    client: TestClient, facts: Facts, database_url: str
) -> None:
    admin = _admin(client)
    _set(admin, facts.tom, TOM_URL)
    _set(admin, facts.tom, ANDREW_URL, "comiconomicon")

    _remove(admin, facts.tom, "rostercon")

    rows = asyncio.run(_rows(database_url))
    assert ("rostercon", facts.tom) not in rows
    assert rows[("comiconomicon", facts.tom)] == ("found", "518", True)


def test_removing_never_touches_what_a_refresh_found(
    client: TestClient, facts: Facts, database_url: str
) -> None:
    response = _remove(_admin(client), facts.paul, "rostercon")

    assert response.headers["location"] == "/admin/links?notice=none"
    assert asyncio.run(_rows(database_url))[("rostercon", facts.paul)] == (
        "found",
        "paul-mcgann-en",
        False,
    )
