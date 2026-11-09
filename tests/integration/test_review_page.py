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

from casttrail.auth.models import User
from casttrail.auth.service import AuthService
from casttrail.catalogue.models import Person
from casttrail.db import create_engine, create_session_factory
from casttrail.discovery.models import Appearance, AppearanceSource, Convention
from casttrail.identity.models import ExternalIdentity, IdentityDecision
from casttrail.main import create_app
from casttrail.matching.models import Match
from casttrail.matching.service import MatchService
from casttrail.providers.base import PersonDetail
from casttrail.watching.service import WatchService

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy.ext.asyncio import AsyncSession

    from casttrail.config import Settings

pytestmark = pytest.mark.integration

PASSWORD = "correct horse battery"
SAME_ORIGIN = {"Sec-Fetch-Site": "same-origin"}
ALEMBIC_INI = Path(__file__).parents[2] / "alembic.ini"
NOW = datetime.now(UTC)
TODAY = NOW.date()


class Facts:
    """Ids of the rows the tests refer to."""

    def __init__(self) -> None:
        self.paul = uuid.UUID(int=0)
        self.sean = uuid.UUID(int=0)
        self.paul_guest = uuid.UUID(int=0)
        self.sean_guest = uuid.UUID(int=0)
        self.other_guest = uuid.UUID(int=0)


async def _guest(
    session: AsyncSession, key: str, name: str, days: int
) -> tuple[ExternalIdentity, Appearance]:
    from sqlalchemy.ext.asyncio import AsyncSession

    assert isinstance(session, AsyncSession)
    con = Convention(
        id=uuid.uuid4(),
        name=f"Con {key}",
        start_date=TODAY + timedelta(days=days),
        created_at=NOW,
        updated_at=NOW,
    )
    identity = ExternalIdentity(
        id=uuid.uuid4(), provider="rostercon", external_id=key, display_name=name
    )
    session.add_all([con, identity])
    await session.flush()
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
            source_url=f"https://www.rostercon.com/en/people/{key}",
        )
    )
    return identity, appearance


async def _seed_async(database_url: str) -> Facts:
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
        await watches.watch_person(
            admin.id, PersonDetail(tmdb_id=1, name="Paul McGann")
        )
        await watches.watch_person(
            admin.id, PersonDetail(tmdb_id=2, name="Sean Pertwee")
        )
        paul, paul_app = await _guest(session, "paul-en", "Paul McGann", 5)
        _, paul_app2 = await _guest(session, "paul-en-2", "x", 6)
        sean, _ = await _guest(session, "sean-en", "Sean Pertwee", 7)
        other, _ = await _guest(session, "other-en", "Someone Else", 8)
        # A second event for the same guest.
        con = Convention(
            id=uuid.uuid4(), name="Second Con", created_at=NOW, updated_at=NOW
        )
        session.add(con)
        await session.flush()
        second = Appearance(
            id=uuid.uuid4(), convention_id=con.id, external_identity_id=paul.id
        )
        session.add(second)
        await session.flush()
        session.add(
            AppearanceSource(
                id=uuid.uuid4(),
                appearance_id=second.id,
                provider="rostercon",
                source_url="https://www.rostercon.com/en/people/paul-en",
            )
        )
        await session.commit()
        await MatchService(session).rematch()
        facts.paul_guest, facts.sean_guest = paul.id, sean.id
        facts.other_guest = other.id
        facts.paul = (
            await session.scalar(select(Person.id).where(Person.name == "Paul McGann"))
        ) or facts.paul
        facts.sean = (
            await session.scalar(select(Person.id).where(Person.name == "Sean Pertwee"))
        ) or facts.sean
        assert paul_app is not None
        assert paul_app2 is not None
    await engine.dispose()
    return facts


@pytest.fixture
def facts(database_url: str) -> Facts:
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")
    return asyncio.run(_seed_async(database_url))


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


async def _read(database_url: str) -> tuple[list[tuple[uuid.UUID, str]], list[str]]:
    engine = create_engine(database_url)
    async with create_session_factory(engine)() as session:
        decisions = [
            (d.external_identity_id, d.decision)
            for d in await session.scalars(select(IdentityDecision))
        ]
        kinds = list(await session.scalars(select(Match.kind).order_by(Match.kind)))
    await engine.dispose()
    return decisions, kinds


# --- access -----------------------------------------------------------------------


def test_every_review_page_needs_a_sign_in(client: TestClient, facts: Facts) -> None:
    form = {"identity_id": str(facts.paul_guest), "person_id": str(facts.paul)}
    assert client.get("/admin/review").headers["location"] == "/login"
    for action in ("confirm", "reject", "unlink", "confirm-all"):
        response = client.post(f"/admin/review/{action}", data=form)
        assert (response.status_code, response.headers["location"]) == (303, "/login")


def test_a_non_admin_gets_a_404_and_changes_nothing(
    client: TestClient, facts: Facts, database_url: str
) -> None:
    _sign_in(client, "user@example.com")
    form = {"identity_id": str(facts.paul_guest), "person_id": str(facts.paul)}

    assert client.get("/admin/review").status_code == 404
    for action in ("confirm", "reject", "unlink", "confirm-all"):
        assert client.post(f"/admin/review/{action}", data=form).status_code == 404

    assert asyncio.run(_read(database_url))[0] == []


def test_a_cross_site_post_is_refused(client: TestClient, facts: Facts) -> None:
    _admin(client)

    response = client.post(
        "/admin/review/confirm",
        data={"identity_id": str(facts.paul_guest), "person_id": str(facts.paul)},
        headers={"Sec-Fetch-Site": "cross-site"},
    )

    assert response.status_code == 403


# --- the page ---------------------------------------------------------------------


def test_the_page_lists_name_matches_waiting_to_be_confirmed(
    client: TestClient,
) -> None:
    page = _admin(client).get("/admin/review").text

    assert "Waiting to be confirmed" in page
    assert page.index("Paul McGann") < page.index("Sean Pertwee")
    assert "Someone Else" not in page
    assert "Confirm all exact-name matches" in page
    assert "No guests are linked." in page
    assert 'href="/admin/review" aria-current="page"' in page


def test_the_event_count_for_a_guest_is_shown(client: TestClient) -> None:
    page = _admin(client).get("/admin/review").text

    row = page[page.index("Paul McGann") :]
    assert "<td>2</td>" in row[: row.index("</tr>")]


def test_an_unknown_notice_shows_nothing(client: TestClient) -> None:
    page = _admin(client).get("/admin/review?notice=%3Cb%3E").text

    assert 'role="status"' not in page


# --- confirm / reject / unlink ----------------------------------------------------


def test_confirming_links_the_guest_and_the_match_becomes_confirmed(
    client: TestClient, facts: Facts, database_url: str
) -> None:
    _admin(client)

    response = client.post(
        "/admin/review/confirm",
        data={"identity_id": str(facts.paul_guest), "person_id": str(facts.paul)},
    )

    assert response.headers["location"] == "/admin/review?notice=confirmed"
    decisions, kinds = asyncio.run(_read(database_url))
    assert decisions == [(facts.paul_guest, "confirmed")]
    assert kinds.count("confirmed") == 2
    page = client.get("/admin/review").text
    assert "Linked guests" in page
    assert "No guests are linked." not in page
    assert "Confirmed." not in page
    assert "Confirmed." in client.get(response.headers["location"]).text


def test_rejecting_removes_the_suggestion_for_good(
    client: TestClient, facts: Facts, database_url: str
) -> None:
    _admin(client)
    client.post(
        "/admin/review/reject",
        data={"identity_id": str(facts.paul_guest), "person_id": str(facts.paul)},
    )

    page = client.get("/admin/review").text

    assert asyncio.run(_read(database_url))[0] == [(facts.paul_guest, "rejected")]
    assert "Paul McGann" not in page
    assert "Sean Pertwee" in page


def test_unlinking_removes_the_link_and_the_match_is_possible_again(
    client: TestClient, facts: Facts, database_url: str
) -> None:
    _admin(client)
    data = {"identity_id": str(facts.paul_guest), "person_id": str(facts.paul)}
    client.post("/admin/review/confirm", data=data)

    response = client.post(
        "/admin/review/unlink", data={"identity_id": str(facts.paul_guest)}
    )

    assert response.headers["location"] == "/admin/review?notice=unlinked"
    decisions, kinds = asyncio.run(_read(database_url))
    assert decisions == []
    assert "confirmed" not in kinds
    assert "Paul McGann" in client.get("/admin/review").text


def test_unknown_ids_are_a_404_and_change_nothing(
    client: TestClient, facts: Facts, database_url: str
) -> None:
    _admin(client)
    nobody = str(uuid.uuid4())

    for action, data in (
        ("confirm", {"identity_id": nobody, "person_id": str(facts.paul)}),
        ("confirm", {"identity_id": str(facts.paul_guest), "person_id": nobody}),
        ("reject", {"identity_id": nobody, "person_id": str(facts.paul)}),
        ("unlink", {"identity_id": nobody}),
    ):
        assert client.post(f"/admin/review/{action}", data=data).status_code == 404

    assert asyncio.run(_read(database_url))[0] == []


@pytest.mark.parametrize("bad", ["", "not-a-uuid"])
def test_malformed_ids_are_refused(client: TestClient, bad: str) -> None:
    _admin(client)

    response = client.post(
        "/admin/review/confirm", data={"identity_id": bad, "person_id": bad}
    )

    assert response.status_code == 422


# --- bulk confirm -----------------------------------------------------------------


def test_confirm_all_confirms_every_single_exact_name_match(
    client: TestClient, facts: Facts, database_url: str
) -> None:
    _admin(client)

    response = client.post("/admin/review/confirm-all")

    assert response.headers["location"] == "/admin/review?notice=confirmed"
    decisions, kinds = asyncio.run(_read(database_url))
    assert sorted(decisions) == sorted(
        [(facts.paul_guest, "confirmed"), (facts.sean_guest, "confirmed")]
    )
    assert "possible" not in kinds
    assert "Nothing is waiting." in client.get("/admin/review").text


def test_confirm_all_leaves_a_guest_with_two_suggestions_alone(
    settings: Settings, database_url: str, facts: Facts
) -> None:
    async def add_twin() -> None:
        engine = create_engine(database_url)
        async with create_session_factory(engine)() as session:
            user = await session.scalar(
                select(User).where(User.email == "user@example.com")
            )
            assert user is not None
            await WatchService(session).watch_person(
                user.id, PersonDetail(tmdb_id=99, name="Paul McGann")
            )
            await MatchService(session).rematch()
        await engine.dispose()

    asyncio.run(add_twin())
    with TestClient(
        create_app(settings), follow_redirects=False, headers=SAME_ORIGIN
    ) as client:
        _admin(client)
        response = client.post("/admin/review/confirm-all")

    decisions, _ = asyncio.run(_read(database_url))
    assert decisions == [(facts.sean_guest, "confirmed")]
    assert response.headers["location"] == "/admin/review?notice=confirmed"


def test_confirm_all_with_nothing_to_do_says_so(
    client: TestClient, database_url: str
) -> None:
    _admin(client)
    client.post("/admin/review/confirm-all")

    response = client.post("/admin/review/confirm-all")

    assert response.headers["location"] == "/admin/review?notice=none"
    assert "Nothing to confirm." in client.get(response.headers["location"]).text


# --- finding and linking a guest --------------------------------------------------


def test_searching_finds_guests_and_offers_people_to_link(
    client: TestClient, facts: Facts
) -> None:
    page = _admin(client).get("/admin/review?q=someone").text

    assert "Someone Else" in page
    assert "Nobody" in page
    assert f'value="{facts.other_guest}"' in page
    assert f'<option value="{facts.paul}"' in page


def test_a_search_with_no_result_says_so(client: TestClient) -> None:
    page = _admin(client).get("/admin/review?q=zzz").text

    assert "No guest has that name." in page


@pytest.mark.parametrize("q", ["%", "_", "\\", "'; drop table people; --"])
def test_search_characters_are_taken_literally(client: TestClient, q: str) -> None:
    response = _admin(client).get("/admin/review", params={"q": q})

    assert response.status_code == 200
    assert "Someone Else" not in response.text


def test_a_blank_search_finds_nothing_rather_than_everyone(client: TestClient) -> None:
    page = _admin(client).get("/admin/review?q=%20%20").text

    assert "Someone Else" not in page
    assert "No guest has that name." in page


def test_no_people_are_offered_until_there_is_a_search_result(
    client: TestClient,
) -> None:
    assert "<option" not in _admin(client).get("/admin/review").text


def test_a_long_search_is_cut_to_100_characters(client: TestClient) -> None:
    page = _admin(client).get("/admin/review", params={"q": "a" * 150}).text

    assert 'value="' + "a" * 100 + '"' in page
    assert "a" * 101 not in page


def test_a_search_shows_at_most_twenty_guests(
    settings: Settings, database_url: str, facts: Facts
) -> None:
    del facts

    async def add_many() -> None:
        engine = create_engine(database_url)
        async with create_session_factory(engine)() as session:
            convention = await session.scalar(select(Convention))
            assert convention is not None
            for n in range(25):
                identity = ExternalIdentity(
                    id=uuid.uuid4(),
                    provider="rostercon",
                    external_id=f"bulk-{n}",
                    display_name=f"Bulk Guest {n:02d}",
                )
                session.add(identity)
                await session.flush()
                session.add(
                    Appearance(
                        id=uuid.uuid4(),
                        convention_id=convention.id,
                        external_identity_id=identity.id,
                    )
                )
            await session.commit()
        await engine.dispose()

    asyncio.run(add_many())
    with TestClient(
        create_app(settings), follow_redirects=False, headers=SAME_ORIGIN
    ) as client:
        page = _admin(client).get("/admin/review?q=bulk").text

    assert page.count("Bulk Guest") == 20
    assert "Bulk Guest 00" in page
    assert "Bulk Guest 24" not in page


def test_a_guest_can_be_linked_to_any_person_and_keeps_the_search(
    client: TestClient, facts: Facts, database_url: str
) -> None:
    _admin(client)

    response = client.post(
        "/admin/review/confirm",
        data={
            "identity_id": str(facts.other_guest),
            "person_id": str(facts.paul),
            "q": "someone",
        },
    )

    assert response.headers["location"] == "/admin/review?notice=confirmed&q=someone"
    assert (facts.other_guest, "confirmed") in asyncio.run(_read(database_url))[0]
    page = client.get(response.headers["location"]).text
    assert "Someone Else" in page


def test_tmdb_identities_are_never_listed_as_guests(
    client: TestClient,
) -> None:
    page = _admin(client).get("/admin/review?q=1").text

    assert "No guest has that name." in page
    linked = _admin(client).get("/admin/review").text
    assert "No guests are linked." in linked


async def _person_ids(database_url: str) -> tuple[uuid.UUID, uuid.UUID]:
    """A person's own TMDB and IMDb identities, as watching a person records them."""
    engine = create_engine(database_url)
    async with create_session_factory(engine)() as session:
        person = await session.scalar(
            select(Person).where(Person.name == "Paul McGann")
        )
        assert person is not None
        ids = []
        for provider, key in (("tmdb", "1"), ("imdb", "nm0565858")):
            found = await session.scalar(
                select(ExternalIdentity).where(
                    ExternalIdentity.provider == provider,
                    ExternalIdentity.external_id == key,
                )
            )
            if found is None:
                found = ExternalIdentity(
                    id=uuid.uuid4(),
                    provider=provider,
                    external_id=key,
                    person_id=person.id,
                )
                session.add(found)
            else:
                found.person_id = person.id
            ids.append(found.id)
        await session.commit()
    await engine.dispose()
    return ids[0], ids[1]


def test_a_persons_own_imdb_and_tmdb_ids_are_never_listed_as_guests(
    client: TestClient, database_url: str
) -> None:
    asyncio.run(_person_ids(database_url))

    review = _admin(client).get("/admin/review").text
    search = client.get("/admin/review?q=nm0565858").text

    assert "<td>nm0565858</td>" not in review
    assert "No guests are linked." in review
    assert "<td>nm0565858</td>" not in search
    assert "No guest has that name." in search


def test_a_persons_own_ids_cannot_be_unlinked_confirmed_or_rejected(
    client: TestClient, facts: Facts, database_url: str
) -> None:
    tmdb_id, imdb_id = asyncio.run(_person_ids(database_url))
    _admin(client)

    for identity in (tmdb_id, imdb_id):
        assert (
            client.post(
                "/admin/review/unlink", data={"identity_id": str(identity)}
            ).status_code
            == 404
        )
        assert (
            client.post(
                "/admin/review/confirm",
                data={"identity_id": str(identity), "person_id": str(facts.sean)},
            ).status_code
            == 404
        )
        assert (
            client.post(
                "/admin/review/reject",
                data={"identity_id": str(identity), "person_id": str(facts.sean)},
            ).status_code
            == 404
        )

    async def still_linked() -> list[uuid.UUID | None]:
        engine = create_engine(database_url)
        async with create_session_factory(engine)() as session:
            rows = [
                (await session.get(ExternalIdentity, i)).person_id  # type: ignore[union-attr]
                for i in (tmdb_id, imdb_id)
            ]
        await engine.dispose()
        return rows

    assert asyncio.run(still_linked()) == [facts.paul, facts.paul]
    assert asyncio.run(_decisions_count(database_url)) == 0


async def _decisions_count(database_url: str) -> int:
    engine = create_engine(database_url)
    async with create_session_factory(engine)() as session:
        count = len(list(await session.scalars(select(IdentityDecision.id))))
    await engine.dispose()
    return count
