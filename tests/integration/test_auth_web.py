import asyncio
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import update
from starlette.testclient import TestClient

from casttrail.auth.models import AuthSession, User
from casttrail.auth.service import AuthService
from casttrail.db import create_engine, create_session_factory
from casttrail.main import create_app
from casttrail.web.dependencies import CurrentUser  # noqa: TC001  (FastAPI reads it)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from fastapi import FastAPI

    from casttrail.config import Settings

pytestmark = pytest.mark.integration

PASSWORD = "correct horse battery"
ALEMBIC_INI = Path(__file__).parents[2] / "alembic.ini"
WRONG = "Incorrect email or password."
SAME_ORIGIN = {"Sec-Fetch-Site": "same-origin"}


def _migrate(database_url: str) -> None:
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")


def _seed(database_url: str, email: str, *, is_active: bool = True) -> None:
    async def run() -> None:
        engine = create_engine(database_url)
        async with create_session_factory(engine)() as session:
            auth = AuthService(session, session_ttl=timedelta(hours=1))
            user = await auth.create_user(
                email=email, display_name=email.split("@")[0], password=PASSWORD
            )
            user.is_active = is_active
            await session.commit()
        await engine.dispose()

    asyncio.run(run())


def _app(settings: Settings) -> FastAPI:
    app = create_app(settings)

    @app.get("/whoami")
    async def whoami(user: CurrentUser) -> dict[str, str]:
        return {"email": user.email}

    return app


@pytest.fixture
def web_settings(settings: Settings, database_url: str) -> Settings:
    _migrate(database_url)
    _seed(database_url, "a@example.com")
    _seed(database_url, "b@example.com")
    _seed(database_url, "off@example.com", is_active=False)
    return settings


@pytest.fixture
def client(web_settings: Settings) -> Iterator[TestClient]:
    with TestClient(
        _app(web_settings), follow_redirects=False, headers=SAME_ORIGIN
    ) as test_client:
        yield test_client


def _sign_in(client: TestClient, email: str = "a@example.com") -> None:
    response = client.post("/login", data={"email": email, "password": PASSWORD})
    assert response.status_code == 303


def test_login_page_renders_a_form(client: TestClient) -> None:
    response = client.get("/login")

    assert response.status_code == 200
    assert 'name="email"' in response.text
    assert 'name="password"' in response.text


def test_signing_in_redirects_home_and_grants_access(client: TestClient) -> None:
    assert client.get("/whoami").status_code == 303

    _sign_in(client)

    assert client.get("/whoami").json() == {"email": "a@example.com"}


def test_the_session_cookie_is_httponly_lax_and_scoped_to_the_whole_site(
    client: TestClient,
) -> None:
    response = client.post(
        "/login", data={"email": "a@example.com", "password": PASSWORD}
    )

    cookie = response.headers["set-cookie"].lower()
    assert cookie.startswith("casttrail-session=")
    assert "httponly" in cookie
    assert "samesite=lax" in cookie
    assert "path=/" in cookie
    assert "domain" not in cookie
    assert "secure" not in cookie


def test_the_cookie_is_secure_and_host_prefixed_over_https(
    web_settings: Settings,
) -> None:
    with TestClient(
        _app(web_settings),
        base_url="https://testserver",
        follow_redirects=False,
        headers=SAME_ORIGIN,
    ) as client:
        response = client.post(
            "/login", data={"email": "a@example.com", "password": PASSWORD}
        )

        cookie = response.headers["set-cookie"]
        assert cookie.startswith("__Host-Http-casttrail-session=")
        assert "Secure" in cookie
        assert client.get("/whoami").status_code == 200


def test_secure_can_be_forced_on_or_off(web_settings: Settings) -> None:
    always = web_settings.model_copy(update={"cookie_secure": "always"})
    with TestClient(
        _app(always), follow_redirects=False, headers=SAME_ORIGIN
    ) as client:
        response = client.post(
            "/login", data={"email": "a@example.com", "password": PASSWORD}
        )
        assert response.headers["set-cookie"].startswith("__Host-Http-")


@pytest.mark.parametrize(
    ("email", "password"),
    [
        ("a@example.com", "wrong password"),
        ("nobody@example.com", PASSWORD),
        ("off@example.com", PASSWORD),
    ],
    ids=["wrong-password", "unknown-email", "disabled-account"],
)
def test_every_refusal_gives_the_same_response(
    client: TestClient, email: str, password: str
) -> None:
    response = client.post("/login", data={"email": email, "password": password})

    assert response.status_code == 401
    assert WRONG in response.text
    assert "set-cookie" not in response.headers


def test_logging_out_ends_the_session_and_clears_the_cookie(
    client: TestClient,
) -> None:
    _sign_in(client)
    token = client.cookies["casttrail-session"]

    response = client.post("/logout")

    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert "max-age=0" in response.headers["set-cookie"].lower()
    client.cookies.set("casttrail-session", token)
    assert client.get("/whoami").status_code == 303


def test_logging_out_without_a_session_still_redirects(client: TestClient) -> None:
    assert client.post("/logout").status_code == 303


def test_a_signed_in_user_visiting_login_is_sent_home(client: TestClient) -> None:
    _sign_in(client)

    response = client.get("/login")

    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_two_users_are_signed_in_independently(web_settings: Settings) -> None:
    with (
        TestClient(
            _app(web_settings), follow_redirects=False, headers=SAME_ORIGIN
        ) as one,
        TestClient(
            _app(web_settings), follow_redirects=False, headers=SAME_ORIGIN
        ) as two,
    ):
        _sign_in(one, "a@example.com")
        _sign_in(two, "b@example.com")

        assert one.get("/whoami").json() == {"email": "a@example.com"}
        assert two.get("/whoami").json() == {"email": "b@example.com"}


def test_a_disabled_account_loses_access_immediately(
    web_settings: Settings, database_url: str
) -> None:
    with TestClient(
        _app(web_settings), follow_redirects=False, headers=SAME_ORIGIN
    ) as client:
        _sign_in(client)
        assert client.get("/whoami").status_code == 200

        async def disable() -> None:
            engine = create_engine(database_url)
            async with create_session_factory(engine)() as session:
                await session.execute(
                    update(User)
                    .where(User.email == "a@example.com")
                    .values(is_active=False)
                )
                await session.commit()
            await engine.dispose()

        asyncio.run(disable())

        assert client.get("/whoami").status_code == 303


def test_an_expired_session_is_refused(
    web_settings: Settings, database_url: str
) -> None:
    with TestClient(
        _app(web_settings), follow_redirects=False, headers=SAME_ORIGIN
    ) as client:
        _sign_in(client)

        async def expire() -> None:
            engine = create_engine(database_url)
            async with create_session_factory(engine)() as session:
                await session.execute(
                    update(AuthSession).values(expires_at=AuthSession.created_at)
                )
                await session.commit()
            await engine.dispose()

        asyncio.run(expire())

        assert client.get("/whoami").status_code == 303


def test_a_duplicated_session_cookie_is_ignored(client: TestClient) -> None:
    _sign_in(client)
    token = client.cookies["casttrail-session"]
    client.cookies.clear()

    response = client.get(
        "/whoami",
        headers={"cookie": f"casttrail-session={token}; casttrail-session=forged"},
    )

    assert response.status_code == 303


def test_an_unknown_token_is_refused(client: TestClient) -> None:
    client.cookies.set("casttrail-session", "made-up")

    assert client.get("/whoami").status_code == 303
