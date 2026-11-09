import asyncio
import re
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from alembic import command
from alembic.config import Config
from starlette.testclient import TestClient

from casttrail.auth.service import AuthService
from casttrail.db import create_engine, create_session_factory
from casttrail.main import create_app

if TYPE_CHECKING:
    from collections.abc import Iterator

    from casttrail.config import Settings

pytestmark = pytest.mark.integration

PASSWORD = "correct horse battery"
ALEMBIC_INI = Path(__file__).parents[2] / "alembic.ini"
PAGES = [
    "/login",
    "/status",
    "/",
    "/watchlist",
    "/search",
    "/account/password",
    "/admin/users",
]


def _seed(database_url: str) -> None:
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")

    async def run() -> None:
        engine = create_engine(database_url)
        async with create_session_factory(engine)() as session:
            auth = AuthService(session, session_ttl=timedelta(hours=1))
            await auth.create_user(
                email="admin@example.com",
                display_name="Admin",
                password=PASSWORD,
                is_admin=True,
            )
        await engine.dispose()

    asyncio.run(run())


@pytest.fixture
def client(settings: Settings, database_url: str) -> Iterator[TestClient]:
    _seed(database_url)
    with TestClient(
        create_app(settings),
        base_url="http://127.0.0.1:8002",
        follow_redirects=False,
        headers={"Sec-Fetch-Site": "same-origin", "X-Forwarded-Proto": "https"},
    ) as test_client:
        response = test_client.post(
            "/login", data={"email": "admin@example.com", "password": PASSWORD}
        )
        assert response.status_code == 303
        yield test_client


def _asset_addresses(page: str) -> list[str]:
    return re.findall(r'(?:src|href)="([^"]*/static/[^"]*)"', page)


@pytest.fixture
def anonymous(client: TestClient) -> TestClient:
    """The same app with nobody signed in, as the sign-in page needs."""
    client.cookies.clear()
    return client


@pytest.mark.parametrize("path", PAGES)
def test_every_page_links_its_assets_by_path_not_by_full_address(
    client: TestClient, path: str
) -> None:
    if path == "/login":
        client.cookies.clear()
    page = client.get(path, headers={"Host": "sam-pc.example.ts.net"}).text

    addresses = _asset_addresses(page)
    assert addresses, f"{path} links no assets"
    assert all(address.startswith("/static/") for address in addresses), addresses
    assert "http://" not in " ".join(addresses)


def test_the_same_links_work_whatever_address_the_browser_used(
    anonymous: TestClient,
) -> None:
    a = _asset_addresses(anonymous.get("/login", headers={"Host": "one.test"}).text)
    b = _asset_addresses(anonymous.get("/login", headers={"Host": "two.test:9"}).text)

    assert a == b


def test_the_linked_assets_actually_exist(client: TestClient) -> None:
    for address in set(_asset_addresses(client.get("/").text)):
        assert client.get(address).status_code == 200, address


def test_the_stylesheet_the_login_page_names_is_the_one_served(
    anonymous: TestClient,
) -> None:
    page = anonymous.get("/login").text

    assert 'href="/static/css/pico.min.css"' in page
    assert 'href="/static/css/app.css"' in page
    assert 'src="/static/js/htmx.min.js"' in page
