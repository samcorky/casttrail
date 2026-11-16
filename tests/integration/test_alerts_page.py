import asyncio
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from alembic import command
from alembic.config import Config
from starlette.testclient import TestClient

from casttrail.auth.service import AuthService
from casttrail.db import create_engine, create_session_factory
from casttrail.discovery.browse import UK
from casttrail.discovery.models import Appearance, AppearanceSource, Convention
from casttrail.identity.models import ExternalIdentity
from casttrail.main import create_app
from casttrail.matching.service import MatchService
from casttrail.providers.base import PersonDetail
from casttrail.watching.service import WatchService

if TYPE_CHECKING:
    from collections.abc import Iterator

    from casttrail.config import Settings

pytestmark = pytest.mark.integration

PASSWORD = "correct horse battery"
SAME_ORIGIN = {"Sec-Fetch-Site": "same-origin"}
ALEMBIC_INI = Path(__file__).parents[2] / "alembic.ini"
TODAY = datetime.now(UTC).date()
EDDIE = PersonDetail(tmdb_id=120679, name="Eddie McClintock")
BADGE = '<span class="visually-hidden"> unread</span>'


async def _seed_async(database_url: str) -> None:
    """Alice watches Eddie, who is then found at two upcoming events (two alerts)."""
    engine = create_engine(database_url)
    async with create_session_factory(engine)() as session:
        auth = AuthService(session, session_ttl=timedelta(hours=1))
        alice = await auth.create_user(
            email="alice@example.com", display_name="Alice", password=PASSWORD
        )
        await auth.create_user(
            email="bob@example.com", display_name="Bob", password=PASSWORD
        )
        watch = await WatchService(session).watch_person(alice.id, EDDIE)
        identity = ExternalIdentity(
            provider="rostercon",
            external_id="eddie-en",
            display_name="Eddie McClintock",
            person_id=watch.person_id,
        )
        session.add(identity)
        for days, name in ((5, "Soon Con"), (40, "Later Con")):
            convention = Convention(
                country=UK,
                name=name,
                start_date=TODAY + timedelta(days=days),
                city="Lincoln",
            )
            session.add(convention)
            await session.flush()
            appearance = Appearance(
                convention_id=convention.id, external_identity_id=identity.id
            )
            session.add(appearance)
            await session.flush()
            session.add(
                AppearanceSource(
                    appearance_id=appearance.id,
                    provider="rostercon",
                    source_url="https://www.rostercon.com/en/people/eddie-en",
                )
            )
        await session.commit()
        await MatchService(session).rematch()
    await engine.dispose()


def _connect(database_url: str) -> sqlite3.Connection:
    return sqlite3.connect(database_url.removeprefix("sqlite+aiosqlite:///"))


def _alert_ids(database_url: str) -> list[str]:
    """Alice's alerts, the soonest event first."""
    with closing(_connect(database_url)) as connection:
        rows = connection.execute(
            """
            SELECT un.id FROM user_notifications un
            JOIN notification_events ne ON ne.id = un.event_id
            ORDER BY ne.start_date
            """
        ).fetchall()
    # SQLAlchemy stores a UUID as 32 hex digits; the page uses the dashed form.
    return [f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}" for (h,) in rows]


def _sign_in(client: TestClient, email: str) -> None:
    client.cookies.clear()
    response = client.post("/login", data={"email": email, "password": PASSWORD})
    assert response.status_code == 303


@pytest.fixture
def client(settings: Settings, database_url: str) -> Iterator[TestClient]:
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")
    asyncio.run(_seed_async(database_url))
    with TestClient(
        create_app(settings), follow_redirects=False, headers=SAME_ORIGIN
    ) as test_client:
        yield test_client


def test_the_page_needs_a_sign_in(client: TestClient) -> None:
    response = client.get("/alerts")

    assert (response.status_code, response.headers["location"]) == (303, "/login")


def test_a_user_sees_their_alerts_and_an_unread_count_on_every_page(
    client: TestClient,
) -> None:
    _sign_in(client, "alice@example.com")

    page = client.get("/alerts").text

    assert "Eddie McClintock" in page
    assert "Soon Con" in page
    assert "Later Con" in page
    assert page.count(">New</span>") == 2
    assert page.count(">Mark read</button>") == 2
    assert "Mark all read" in page
    assert 'href="/alerts" aria-current="page"' in page
    assert f"2{BADGE}" in page
    assert f"2{BADGE}" in client.get("/watchlist").text


def test_another_user_sees_none_of_it(client: TestClient) -> None:
    _sign_in(client, "bob@example.com")

    page = client.get("/alerts").text

    assert "Eddie McClintock" not in page
    assert "No alerts yet" in page
    assert "Mark all read" not in page
    assert BADGE not in page


def test_marking_one_read_lowers_the_count_and_can_be_repeated(
    client: TestClient, database_url: str
) -> None:
    _sign_in(client, "alice@example.com")
    soon, _ = _alert_ids(database_url)

    for _ in range(2):
        response = client.post(f"/alerts/{soon}/read")
        assert (response.status_code, response.headers["location"]) == (303, "/alerts")

    page = client.get("/alerts").text
    assert page.count(">Mark read</button>") == 1
    assert f"1{BADGE}" in page


def test_marking_all_read_clears_the_count(client: TestClient) -> None:
    _sign_in(client, "alice@example.com")

    response = client.post("/alerts/read")

    assert (response.status_code, response.headers["location"]) == (303, "/alerts")
    page = client.get("/alerts").text
    assert "Mark read" not in page
    assert "Mark all read" not in page
    assert BADGE not in page
    assert "Soon Con" in page


def test_another_users_alert_cannot_be_marked(
    client: TestClient, database_url: str
) -> None:
    soon, _ = _alert_ids(database_url)
    _sign_in(client, "bob@example.com")

    assert client.post(f"/alerts/{soon}/read").status_code == 404
    assert client.post("/alerts/read").status_code == 303

    _sign_in(client, "alice@example.com")
    assert f"2{BADGE}" in client.get("/alerts").text


def test_an_unknown_alert_is_not_found(client: TestClient) -> None:
    _sign_in(client, "alice@example.com")

    response = client.post("/alerts/00000000-0000-0000-0000-000000000000/read")

    assert response.status_code == 404


def test_a_changed_or_cancelled_alert_says_so(
    client: TestClient, database_url: str
) -> None:
    with closing(_connect(database_url)) as connection:
        connection.execute(
            """
            UPDATE notification_events SET rev = 2, cancelled = 1 WHERE start_date = (
                SELECT MAX(start_date) FROM notification_events)
            """
        )
        connection.execute("UPDATE user_notifications SET rev = 2")
        connection.commit()
    _sign_in(client, "alice@example.com")

    page = client.get("/alerts").text

    assert ">Cancelled</span>" in page
    assert ">Dates changed</span>" in page
    assert ">New</span>" not in page
