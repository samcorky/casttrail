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


def test_signed_in_pages_have_a_menu_button_that_starts_hidden(
    client: TestClient,
) -> None:
    _sign_in(client, "bob@example.com")

    page = client.get("/").text

    assert 'class="nav-toggle outline secondary" hidden' in page
    assert 'aria-controls="nav-menu"' in page
    assert 'aria-expanded="false"' in page
    assert '<ul id="nav-menu">' in page
    assert "/static/js/nav.js" in page


def test_the_menu_button_is_labelled_for_screen_readers(client: TestClient) -> None:
    _sign_in(client, "bob@example.com")

    page = client.get("/").text

    assert '<span class="visually-hidden">Menu</span>' in page
    assert '<span aria-hidden="true">&#9776;</span>' in page


def test_the_sign_in_page_has_no_menu_button(client: TestClient) -> None:
    assert 'class="nav-toggle' not in client.get("/login").text


def test_the_nav_script_and_styles_are_served(client: TestClient) -> None:
    script = client.get("/static/js/nav.js")
    css = client.get("/static/css/app.css")

    assert script.status_code == 200
    assert "javascript" in script.headers["content-type"]
    assert "aria-expanded" in script.text
    assert "Escape" in script.text
    assert ".js #nav-menu" in css.text
    assert ".visually-hidden" in css.text
