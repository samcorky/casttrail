"""A slow outside call must never stop the rest of the app working.

Every database transaction takes SQLite's single write lock when it begins. If a
request kept one open while it waited on TMDB (or any outside service), every other
request would queue behind it and fail with "database is locked" after five seconds.
A signed-in request therefore must not leave a transaction open while its handler
does other things.
"""

import asyncio
import threading
import time
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import async_sessionmaker
from starlette.testclient import TestClient

from casttrail.auth.service import AuthService
from casttrail.catalogue.models import Person
from casttrail.db import create_engine, create_session_factory
from casttrail.discovery.refresh import RosterconRefresh
from casttrail.main import create_app
from casttrail.providers.base import (
    CastMember,
    MediaType,
    PersonCredit,
    PersonDetail,
    PersonSummary,
    Production,
)
from casttrail.providers.rostercon_client import RosterconUnavailableError
from casttrail.watching.service import WatchService

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy.ext.asyncio import AsyncSession

    from casttrail.config import Settings
    from casttrail.providers.rostercon_client import RosterconFetcher

pytestmark = pytest.mark.integration

PASSWORD = "correct horse battery"
SAME_ORIGIN = {"Sec-Fetch-Site": "same-origin"}
ALEMBIC_INI = Path(__file__).parents[2] / "alembic.ini"
# Longer than the database's five-second wait for the write lock, so a request that
# was blocked by it would fail here, not just be slow.
HOLD = 12.0


class SlowMetadata:
    """A TMDB stand-in that takes as long as the test lets it."""

    provider_id = "slow"

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    async def _wait(self) -> None:
        self.started.set()
        await asyncio.to_thread(self.release.wait, HOLD)

    async def search_people(self, query: str) -> list[PersonSummary]:
        await self._wait()
        return []

    async def search_productions(self, query: str) -> list[Production]:
        return []

    async def production(
        self, media_type: MediaType, tmdb_id: int
    ) -> Production | None:
        await self._wait()
        return None

    async def person(self, tmdb_id: int) -> PersonDetail | None:
        await self._wait()
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
            for name in ("alice", "bob"):
                await auth.create_user(
                    email=f"{name}@example.com", display_name=name, password=PASSWORD
                )
        await engine.dispose()

    asyncio.run(run())


@pytest.fixture
def metadata() -> SlowMetadata:
    return SlowMetadata()


@pytest.fixture
def client(
    settings: Settings, database_url: str, metadata: SlowMetadata
) -> Iterator[TestClient]:
    _seed(database_url)
    with TestClient(
        create_app(settings, metadata_provider=metadata),
        follow_redirects=False,
        headers=SAME_ORIGIN,
    ) as test_client:
        yield test_client
    metadata.release.set()


def _sign_in(client: TestClient, email: str) -> TestClient:
    other = TestClient(client.app, follow_redirects=False, headers=SAME_ORIGIN)
    response = other.post("/login", data={"email": email, "password": PASSWORD})
    assert response.status_code == 303
    return other


def _in_background(call: object) -> threading.Thread:
    thread = threading.Thread(target=call, daemon=True)  # type: ignore[arg-type]
    thread.start()
    return thread


def test_a_slow_search_does_not_block_other_requests(
    client: TestClient, metadata: SlowMetadata
) -> None:
    alice = _sign_in(client, "alice@example.com")
    bob = _sign_in(client, "bob@example.com")
    searching = _in_background(lambda: alice.get("/search", params={"q": "someone"}))
    assert metadata.started.wait(5), "the search never reached the outside service"

    began = time.monotonic()
    page = bob.get("/watchlist")
    health = client.get("/health")
    waited = time.monotonic() - began

    metadata.release.set()
    searching.join(timeout=HOLD + 5)
    assert (page.status_code, health.status_code) == (200, 200)
    assert waited < 3


def test_a_slow_lookup_while_watching_a_person_does_not_block_others(
    client: TestClient, metadata: SlowMetadata
) -> None:
    alice = _sign_in(client, "alice@example.com")
    bob = _sign_in(client, "bob@example.com")
    watching = _in_background(
        lambda: alice.post(
            "/watches/people", data={"tmdb_id": 1, "return_to": "search"}
        )
    )
    assert metadata.started.wait(5)

    began = time.monotonic()
    page = bob.get("/watchlist")
    waited = time.monotonic() - began

    metadata.release.set()
    watching.join(timeout=HOLD + 5)
    assert page.status_code == 200
    assert waited < 3


def test_a_slow_lookup_while_watching_a_film_does_not_block_others(
    client: TestClient, metadata: SlowMetadata
) -> None:
    alice = _sign_in(client, "alice@example.com")
    bob = _sign_in(client, "bob@example.com")
    watching = _in_background(
        lambda: alice.post(
            "/watches/productions",
            data={"media_type": "movie", "tmdb_id": 10, "return_to": "search"},
        )
    )
    assert metadata.started.wait(5)

    began = time.monotonic()
    page = bob.get("/watchlist")
    waited = time.monotonic() - began

    metadata.release.set()
    watching.join(timeout=HOLD + 5)
    assert page.status_code == 200
    assert waited < 3


# --- the refresh -------------------------------------------------------------------


class BlockingFetcher:
    """A Rostercon fetcher that waits, like a slow page, until the test lets it go."""

    def __init__(self) -> None:
        self.reached = asyncio.Event()
        self.release = asyncio.Event()

    async def get(self, url: str) -> object:
        self.reached.set()
        await self.release.wait()
        raise RosterconUnavailableError("stop here")


async def test_a_refresh_waiting_on_rostercon_does_not_hold_the_write_lock(
    db_session: AsyncSession,
) -> None:
    auth = AuthService(db_session, session_ttl=timedelta(hours=1))
    user = await auth.create_user(
        email="a@example.com", display_name="A", password=PASSWORD
    )
    await WatchService(db_session).watch_person(
        user.id, PersonDetail(tmdb_id=1, name="Paul McGann")
    )
    fetcher = BlockingFetcher()
    refresh = RosterconRefresh(
        db_session,
        cast("RosterconFetcher", fetcher),
    )
    assert db_session.bind is not None
    other_session = async_sessionmaker(db_session.bind, expire_on_commit=False)

    task = asyncio.create_task(refresh.run())
    try:
        await asyncio.wait_for(fetcher.reached.wait(), 5)
        # While the refresh waits for Rostercon, anyone else can still write.
        async with other_session() as other:
            other.add(Person(name="Someone Else"))
            await asyncio.wait_for(other.commit(), 3)
    finally:
        fetcher.release.set()
        report = await task

    assert report is not None
    assert report.stopped == "Rostercon asked us to wait"
