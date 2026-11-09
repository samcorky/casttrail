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
from casttrail.db import create_engine, create_session_factory
from casttrail.discovery.models import Convention, ConventionMerge, ConventionSource
from casttrail.discovery.service import DiscoveryService
from casttrail.main import create_app
from casttrail.matching.models import Match
from casttrail.matching.service import MatchService
from casttrail.providers.base import (
    ConventionInfo,
    GuestObservation,
    ListingResult,
    PersonDetail,
)
from casttrail.watching.service import WatchService

if TYPE_CHECKING:
    from collections.abc import Iterator

    from casttrail.config import Settings

pytestmark = pytest.mark.integration

PASSWORD = "correct horse battery"
SAME_ORIGIN = {"Sec-Fetch-Site": "same-origin"}
ALEMBIC_INI = Path(__file__).parents[2] / "alembic.ini"
SOON = datetime.now(UTC).date() + timedelta(days=30)


class Facts:
    def __init__(self) -> None:
        self.pair = uuid.UUID(int=0)
        self.one = uuid.UUID(int=0)
        self.two = uuid.UUID(int=0)


def _info(key: str, name: str) -> ConventionInfo:
    return ConventionInfo(
        source_key=key,
        name=name,
        source_url=f"https://example.test/{key}",
        start_date=SOON,
        end_date=SOON + timedelta(days=1),
    )


def _guests(*names: str) -> ListingResult:
    return ListingResult(
        items=[
            GuestObservation(
                source_key=n.lower().replace(" ", "-"),
                name=n,
                source_url=f"https://example.test/g/{n}",
            )
            for n in names
        ],
        complete=False,
    )


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
        await WatchService(session).watch_person(
            admin.id, PersonDetail(tmdb_id=1, name="Amy Pond")
        )
        service = DiscoveryService(session)
        one = await service.ingest(
            "rostercon", _info("a", "Some Con"), _guests("Amy Pond", "Bob")
        )
        two = await service.ingest(
            "comiconomicon", _info("1", "Some Con Lincoln"), _guests("Cat")
        )
        pair = await session.scalar(select(ConventionMerge.id))
        assert pair is not None
        facts.pair, facts.one, facts.two = pair, one.convention_id, two.convention_id
        await MatchService(session).rematch()
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


def _sign_in(client: TestClient, email: str) -> TestClient:
    client.cookies.clear()
    assert (
        client.post("/login", data={"email": email, "password": PASSWORD}).status_code
        == 303
    )
    return client


def _admin(client: TestClient) -> TestClient:
    return _sign_in(client, "admin@example.com")


async def _state(database_url: str) -> tuple[list[uuid.UUID], list[str], int]:
    engine = create_engine(database_url)
    async with create_session_factory(engine)() as session:
        conventions = list(await session.scalars(select(Convention.id)))
        statuses = list(await session.scalars(select(ConventionMerge.status)))
        matches = len(list(await session.scalars(select(Match.id))))
    await engine.dispose()
    return conventions, statuses, matches


# --- access -----------------------------------------------------------------------


def test_every_merge_page_needs_a_sign_in(client: TestClient, facts: Facts) -> None:
    assert client.get("/admin/merges").headers["location"] == "/login"
    for action in ("merge", "reject", "reconsider"):
        response = client.post(
            f"/admin/merges/{facts.pair}/{action}", data={"keep": str(facts.one)}
        )
        assert (response.status_code, response.headers["location"]) == (303, "/login")


def test_an_ordinary_user_gets_a_404_and_nothing_changes(
    client: TestClient, facts: Facts, database_url: str
) -> None:
    _sign_in(client, "user@example.com")

    assert client.get("/admin/merges").status_code == 404
    for action in ("merge", "reject", "reconsider"):
        response = client.post(
            f"/admin/merges/{facts.pair}/{action}", data={"keep": str(facts.one)}
        )
        assert response.status_code == 404

    conventions, statuses, _ = asyncio.run(_state(database_url))
    assert len(conventions) == 2
    assert statuses == ["suggested"]


def test_a_cross_site_post_is_refused(client: TestClient, facts: Facts) -> None:
    _admin(client)

    response = client.post(
        f"/admin/merges/{facts.pair}/merge",
        data={"keep": str(facts.one)},
        headers={"Sec-Fetch-Site": "cross-site"},
    )

    assert response.status_code == 403


# --- the page ---------------------------------------------------------------------


def test_the_page_shows_a_suggested_pair_with_both_sides(client: TestClient) -> None:
    page = _admin(client).get("/admin/merges").text

    assert "Might be the same event (1)" in page
    assert "Alike names on overlapping days." in page
    assert "Some Con Lincoln" in page
    assert "Some Con<" in page.replace("Some Con Lincoln", "")
    assert "Rostercon" in page
    assert "Comiconomicon" in page
    assert page.count("Same event: keep this one") == 2
    assert "Different events" in page
    assert 'href="/admin/merges" aria-current="page"' in page


def test_with_nothing_to_decide_the_page_says_so(
    client: TestClient, facts: Facts
) -> None:
    _admin(client)
    client.post(f"/admin/merges/{facts.pair}/reject")

    page = client.get("/admin/merges").text

    assert "Nothing is waiting." in page
    assert "Marked as different (1)" in page
    assert "Suggest again" in page


def test_an_unknown_notice_shows_nothing(client: TestClient) -> None:
    page = _admin(client).get("/admin/merges?notice=%3Cb%3E").text

    assert 'role="status"' not in page


# --- deciding ---------------------------------------------------------------------


def test_merging_joins_the_events_and_says_so(
    client: TestClient, facts: Facts, database_url: str
) -> None:
    _admin(client)

    response = client.post(
        f"/admin/merges/{facts.pair}/merge", data={"keep": str(facts.one)}
    )
    page = client.get(response.headers["location"]).text

    assert response.headers["location"] == "/admin/merges?notice=merged"
    assert "The two events are now one." in page
    conventions, statuses, _ = asyncio.run(_state(database_url))
    assert conventions == [facts.one]
    assert statuses == []
    assert client.get(f"/events/{facts.two}").status_code == 404
    assert client.get(f"/events/{facts.one}").status_code == 200


def test_the_joined_event_page_shows_both_sources(
    client: TestClient, facts: Facts
) -> None:
    _admin(client)
    client.post(f"/admin/merges/{facts.pair}/merge", data={"keep": str(facts.two)})

    page = client.get(f"/events/{facts.two}").text

    assert "Rostercon" in page
    assert "Comiconomicon" in page
    assert "Amy Pond" in page
    assert "Cat" in page


def test_merging_keeps_a_watched_persons_match_pointing_at_the_kept_event(
    client: TestClient, facts: Facts, database_url: str
) -> None:
    _admin(client)
    before = asyncio.run(_state(database_url))[2]

    client.post(f"/admin/merges/{facts.pair}/merge", data={"keep": str(facts.two)})
    page = client.get("/appearances").text

    assert before == 1
    assert "Amy Pond" in page
    assert f"/events/{facts.two}" in page
    assert f"/events/{facts.one}" not in page


def test_rejecting_keeps_both_events(
    client: TestClient, facts: Facts, database_url: str
) -> None:
    _admin(client)

    response = client.post(f"/admin/merges/{facts.pair}/reject")

    assert response.headers["location"] == "/admin/merges?notice=rejected"
    conventions, statuses, _ = asyncio.run(_state(database_url))
    assert len(conventions) == 2
    assert statuses == ["rejected"]
    assert "Marked as different events" in client.get(response.headers["location"]).text


def test_a_rejection_can_be_taken_back_from_the_page(
    client: TestClient, facts: Facts, database_url: str
) -> None:
    _admin(client)
    client.post(f"/admin/merges/{facts.pair}/reject")

    response = client.post(f"/admin/merges/{facts.pair}/reconsider")

    assert response.headers["location"] == "/admin/merges?notice=reconsidered"
    assert asyncio.run(_state(database_url))[1] == ["suggested"]
    assert (
        "That pair will be suggested again."
        in client.get(response.headers["location"]).text
    )


def test_two_events_of_one_source_are_refused_with_a_reason(
    client: TestClient, facts: Facts, database_url: str
) -> None:
    async def make_same_source_pair() -> uuid.UUID:
        engine = create_engine(database_url)
        async with create_session_factory(engine)() as session:
            now = datetime.now(UTC)
            third = Convention(
                id=uuid.uuid4(), name="Some Con Again", created_at=now, updated_at=now
            )
            session.add(third)
            await session.flush()
            session.add(
                ConventionSource(
                    id=uuid.uuid4(),
                    convention_id=third.id,
                    provider="rostercon",
                    source_key="c",
                    source_url="https://example.test/c",
                )
            )
            first, second = sorted((facts.one, third.id), key=str)
            pair = ConventionMerge(
                id=uuid.uuid4(),
                first_id=first,
                second_id=second,
                status="suggested",
                reason="Alike names on overlapping days.",
                created_at=now,
            )
            session.add(pair)
            await session.commit()
            pair_id = pair.id
        await engine.dispose()
        return pair_id

    pair_id = asyncio.run(make_same_source_pair())
    _admin(client)

    response = client.post(
        f"/admin/merges/{pair_id}/merge", data={"keep": str(facts.one)}
    )
    page = client.get(response.headers["location"]).text

    assert response.headers["location"] == "/admin/merges?notice=same-source"
    assert "one source lists both" in page
    conventions, statuses, _ = asyncio.run(_state(database_url))
    assert len(conventions) == 3
    assert sorted(statuses) == ["suggested", "suggested"]


def test_an_unknown_pair_is_a_404(client: TestClient) -> None:
    _admin(client)
    nobody = uuid.uuid4()

    assert (
        client.post(
            f"/admin/merges/{nobody}/merge", data={"keep": str(nobody)}
        ).status_code
        == 404
    )
    assert client.post(f"/admin/merges/{nobody}/reject").status_code == 404
    assert client.post(f"/admin/merges/{nobody}/reconsider").status_code == 404


def test_keeping_something_that_is_not_in_the_pair_is_a_404(
    client: TestClient, facts: Facts, database_url: str
) -> None:
    _admin(client)

    response = client.post(
        f"/admin/merges/{facts.pair}/merge", data={"keep": str(uuid.uuid4())}
    )

    assert response.status_code == 404
    assert len(asyncio.run(_state(database_url))[0]) == 2


@pytest.mark.parametrize("keep", ["", "not-a-uuid"])
def test_a_malformed_choice_is_refused(
    client: TestClient, facts: Facts, keep: str
) -> None:
    _admin(client)

    response = client.post(f"/admin/merges/{facts.pair}/merge", data={"keep": keep})

    assert response.status_code == 422


def test_a_malformed_pair_id_is_refused(client: TestClient) -> None:
    _admin(client)

    assert client.post("/admin/merges/not-a-uuid/reject").status_code == 422


def test_deciding_twice_is_a_404_the_second_time(
    client: TestClient, facts: Facts
) -> None:
    _admin(client)
    assert client.post(f"/admin/merges/{facts.pair}/reject").status_code == 303

    assert client.post(f"/admin/merges/{facts.pair}/reject").status_code == 404
    assert (
        client.post(
            f"/admin/merges/{facts.pair}/merge", data={"keep": str(facts.one)}
        ).status_code
        == 404
    )


def test_the_events_in_the_page_are_escaped(
    client: TestClient, database_url: str
) -> None:
    async def rename() -> None:
        engine = create_engine(database_url)
        async with create_session_factory(engine)() as session:
            con = await session.scalar(select(Convention))
            assert con is not None
            con.name = "<script>alert(1)</script>"
            await session.commit()
        await engine.dispose()

    asyncio.run(rename())

    page = _admin(client).get("/admin/merges").text

    assert "<script>alert" not in page
