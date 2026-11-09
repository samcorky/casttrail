import asyncio
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
SAME_ORIGIN = {"Sec-Fetch-Site": "same-origin"}
ALEMBIC_INI = Path(__file__).parents[2] / "alembic.ini"


def _seed(database_url: str) -> None:
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")

    async def run() -> None:
        engine = create_engine(database_url)
        async with create_session_factory(engine)() as session:
            auth = AuthService(session, session_ttl=timedelta(hours=1))
            await auth.create_user(
                email="alice@example.com",
                display_name="Alice",
                password=PASSWORD,
                is_admin=True,
            )
            await auth.create_user(
                email="bob@example.com", display_name="Bob", password=PASSWORD
            )
        await engine.dispose()

    asyncio.run(run())


@pytest.fixture
def app_settings(settings: Settings, database_url: str) -> Settings:
    _seed(database_url)
    return settings


@pytest.fixture
def client(app_settings: Settings) -> Iterator[TestClient]:
    with TestClient(
        create_app(app_settings), follow_redirects=False, headers=SAME_ORIGIN
    ) as test_client:
        yield test_client


def _sign_in(client: TestClient, email: str) -> None:
    client.cookies.clear()
    response = client.post("/login", data={"email": email, "password": PASSWORD})
    assert response.status_code == 303


def test_the_dashboard_needs_a_sign_in(client: TestClient) -> None:
    response = client.get("/")

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_each_user_sees_only_their_own_dashboard(app_settings: Settings) -> None:
    with (
        TestClient(
            create_app(app_settings), follow_redirects=False, headers=SAME_ORIGIN
        ) as alice,
        TestClient(
            create_app(app_settings), follow_redirects=False, headers=SAME_ORIGIN
        ) as bob,
    ):
        _sign_in(alice, "alice@example.com")
        _sign_in(bob, "bob@example.com")

        alice_page = alice.get("/").text
        bob_page = bob.get("/").text

    assert "Hello, Alice" in alice_page
    assert "alice@example.com" in alice_page
    assert "bob@example.com" not in alice_page
    assert "Hello, Bob" in bob_page
    assert "bob@example.com" in bob_page
    assert "alice@example.com" not in bob_page


def test_a_user_sees_the_navigation_without_the_admin_link(client: TestClient) -> None:
    _sign_in(client, "bob@example.com")

    page = client.get("/").text

    assert 'aria-label="Main navigation"' in page
    assert 'href="/status"' in page
    assert 'action="/logout"' in page
    assert "/admin/users" not in page


def test_an_admin_sees_the_users_link(client: TestClient) -> None:
    _sign_in(client, "alice@example.com")

    assert 'href="/admin/users"' in client.get("/").text


def test_the_current_page_is_marked_in_the_navigation(client: TestClient) -> None:
    _sign_in(client, "alice@example.com")

    page = client.get("/admin/users").text

    assert 'href="/admin/users" aria-current="page"' in page
    assert 'href="/" aria-current="page"' not in page


def test_the_sign_in_page_has_no_account_navigation(client: TestClient) -> None:
    page = client.get("/login").text

    assert 'action="/logout"' not in page
    assert 'href="/status"' not in page


def test_signing_out_from_the_navigation_ends_the_session(client: TestClient) -> None:
    _sign_in(client, "bob@example.com")

    assert client.post("/logout").status_code == 303

    assert client.get("/").status_code == 303


def test_the_status_page_is_public_and_leads_back_home(client: TestClient) -> None:
    response = client.get("/status")

    assert response.status_code == 200
    assert 'href="/"' in response.text


def test_signed_in_pages_have_a_collapsing_menu_with_a_labelled_button(
    client: TestClient,
) -> None:
    _sign_in(client, "bob@example.com")

    page = client.get("/").text

    assert 'class="navbar-toggler"' in page
    assert 'data-bs-toggle="collapse"' in page
    assert 'data-bs-target="#nav-menu"' in page
    assert 'aria-controls="nav-menu"' in page
    assert 'aria-expanded="false"' in page
    assert 'aria-label="Menu"' in page
    assert 'id="nav-menu"' in page
    assert "/static/js/bootstrap.bundle.min.js" in page


def test_the_sign_in_page_has_no_menu_button(client: TestClient) -> None:
    page = client.get("/login").text

    assert "navbar-toggler" not in page
    assert 'id="nav-menu"' not in page


def test_the_menu_holds_the_pages_people_use_and_the_account_menu(
    client: TestClient,
) -> None:
    _sign_in(client, "bob@example.com")

    page = client.get("/").text

    for link in ("/watchlist", "/appearances", "/events", "/search"):
        assert f'href="{link}"' in page
    assert 'href="/account/password"' in page
    assert 'href="/status"' in page
    assert 'action="/logout"' in page
    assert ">Sign out</button>" in page
    assert "Bob" in page


def test_an_ordinary_user_does_not_see_the_admin_menu(client: TestClient) -> None:
    _sign_in(client, "bob@example.com")

    page = client.get("/").text

    assert "/admin/" not in page
    assert ">Admin</a>" not in page


def test_an_admin_sees_every_admin_page_in_one_menu(client: TestClient) -> None:
    _sign_in(client, "alice@example.com")

    page = client.get("/").text

    assert ">Admin</a>" in page
    for link in (
        "/admin/users",
        "/admin/refresh",
        "/admin/merges",
        "/admin/review",
        "/admin/health",
    ):
        assert f'href="{link}"' in page


def test_the_current_page_is_marked_in_the_menu(client: TestClient) -> None:
    _sign_in(client, "alice@example.com")

    page = client.get("/admin/users").text

    assert 'href="/admin/users" aria-current="page"' in page
    assert 'class="dropdown-item active" href="/admin/users"' in page
    assert 'class="nav-link dropdown-toggle active"' in page
    assert page.count('aria-current="page"') == 1


def test_the_theme_follows_the_device(client: TestClient) -> None:
    page = client.get("/login").text
    script = client.get("/static/js/theme.js")

    assert 'data-bs-theme="light"' in page
    assert page.index("/static/js/theme.js") < page.index(
        "/static/css/bootstrap.min.css"
    )
    assert script.status_code == 200
    assert "prefers-color-scheme: dark" in script.text
    assert "data-bs-theme" in script.text
