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

from casttrail.auth.admin import AdminService
from casttrail.auth.errors import AccountError
from casttrail.auth.models import AuthSession, KnownDevice, User
from casttrail.auth.service import AuthService
from casttrail.auth.tokens import hash_token
from casttrail.db import create_engine, create_session_factory
from casttrail.main import create_app
from casttrail.web.dependencies import CurrentUser  # noqa: TC001 - FastAPI needs it at runtime

if TYPE_CHECKING:
    from collections.abc import Iterator

    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import AsyncSession

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
                email="admin@example.com",
                display_name="Admin",
                password=PASSWORD,
                is_admin=True,
            )
            await auth.create_user(
                email="user@example.com", display_name="User", password=PASSWORD
            )
        await engine.dispose()

    asyncio.run(run())


def _app(settings: Settings) -> FastAPI:
    app = create_app(settings)

    @app.get("/whoami")
    async def whoami(user: CurrentUser) -> dict[str, str]:
        return {"email": user.email}

    return app


@pytest.fixture
def client(settings: Settings, database_url: str) -> Iterator[TestClient]:
    _seed(database_url)
    with TestClient(
        _app(settings), follow_redirects=False, headers=SAME_ORIGIN
    ) as test_client:
        yield test_client


def _sign_in(client: TestClient, email: str, password: str = PASSWORD) -> None:
    client.cookies.clear()
    assert (
        client.post("/login", data={"email": email, "password": password}).status_code
        == 303
    )


def _user_id(database_url: str, email: str) -> uuid.UUID:
    async def run() -> uuid.UUID:
        engine = create_engine(database_url)
        async with create_session_factory(engine)() as session:
            found = await session.scalar(select(User.id).where(User.email == email))
        await engine.dispose()
        assert found is not None
        return found

    return asyncio.run(run())


# --- access ---------------------------------------------------------------------


def test_the_admin_pages_need_a_sign_in(client: TestClient) -> None:
    response = client.get("/admin/users")

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_a_non_admin_gets_a_404_for_every_admin_page(
    client: TestClient, database_url: str
) -> None:
    other = _user_id(database_url, "user@example.com")
    _sign_in(client, "user@example.com")

    assert client.get("/admin/users").status_code == 404
    assert (
        client.post(
            "/admin/users",
            data={"email": "x@example.com", "display_name": "X", "password": PASSWORD},
        ).status_code
        == 404
    )
    for action in ("disable", "enable", "make-admin"):
        assert client.post(f"/admin/users/{other}/{action}").status_code == 404


def test_an_admin_sees_every_account(client: TestClient) -> None:
    _sign_in(client, "admin@example.com")

    page = client.get("/admin/users").text

    assert "admin@example.com" in page
    assert "user@example.com" in page


# --- create ---------------------------------------------------------------------


def test_an_admin_can_add_a_user_who_can_then_sign_in(client: TestClient) -> None:
    _sign_in(client, "admin@example.com")

    response = client.post(
        "/admin/users",
        data={
            "email": "New@Example.com",
            "display_name": "New",
            "password": "another long password",
        },
    )

    assert response.status_code == 303
    assert "new@example.com" in client.get("/admin/users").text
    _sign_in(client, "new@example.com", "another long password")
    assert client.get("/whoami").json() == {"email": "new@example.com"}
    assert client.get("/admin/users").status_code == 404


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"password": "short"}, "at least 12 characters"),
        ({"password": "x" * 300}, "at most 256 characters"),
        ({"email": "user@example.com"}, "already in use"),
        ({"email": "not-an-email"}, "valid email"),
        ({"display_name": "   "}, "name must be"),
    ],
)
def test_a_bad_new_account_is_refused_with_the_reason(
    client: TestClient, data: dict[str, str], message: str
) -> None:
    _sign_in(client, "admin@example.com")
    form = {"email": "new@example.com", "display_name": "New", "password": PASSWORD}

    response = client.post("/admin/users", data=form | data)

    assert response.status_code == 400
    assert message in response.text
    assert "new@example.com" not in client.get("/admin/users").text or "email" in data


def test_a_refused_form_keeps_what_was_typed_but_never_the_password(
    client: TestClient,
) -> None:
    _sign_in(client, "admin@example.com")

    response = client.post(
        "/admin/users",
        data={
            "email": "new@example.com",
            "display_name": "New Person",
            "password": "too short",
        },
    )

    assert 'value="new@example.com"' in response.text
    assert 'value="New Person"' in response.text
    assert "too short" not in response.text


# --- disable and enable ---------------------------------------------------------


def test_disabling_a_user_ends_their_session_and_blocks_sign_in(
    client: TestClient, database_url: str
) -> None:
    target = _user_id(database_url, "user@example.com")
    _sign_in(client, "user@example.com")
    user_cookies = dict(client.cookies)
    assert client.get("/whoami").status_code == 200

    _sign_in(client, "admin@example.com")
    assert client.post(f"/admin/users/{target}/disable").status_code == 303

    client.cookies.clear()
    client.cookies.update(user_cookies)
    assert client.get("/whoami").status_code == 303
    client.cookies.clear()
    assert (
        client.post(
            "/login", data={"email": "user@example.com", "password": PASSWORD}
        ).status_code
        == 401
    )


def test_enabling_a_user_lets_them_sign_in_again(
    client: TestClient, database_url: str
) -> None:
    target = _user_id(database_url, "user@example.com")
    _sign_in(client, "admin@example.com")
    assert client.post(f"/admin/users/{target}/disable").status_code == 303

    assert client.post(f"/admin/users/{target}/enable").status_code == 303

    _sign_in(client, "user@example.com")
    assert client.get("/whoami").status_code == 200


def test_an_admin_cannot_disable_themselves(
    client: TestClient, database_url: str
) -> None:
    own = _user_id(database_url, "admin@example.com")
    _sign_in(client, "admin@example.com")

    response = client.post(f"/admin/users/{own}/disable")

    assert response.status_code == 400
    assert "cannot disable your own account" in response.text
    assert client.get("/whoami").status_code == 200


def test_the_page_offers_no_disable_button_for_yourself(
    client: TestClient, database_url: str
) -> None:
    own = _user_id(database_url, "admin@example.com")
    _sign_in(client, "admin@example.com")

    assert f"/admin/users/{own}/disable" not in client.get("/admin/users").text


# --- make admin -----------------------------------------------------------------


def test_making_a_user_admin_gives_them_the_pages(
    client: TestClient, database_url: str
) -> None:
    target = _user_id(database_url, "user@example.com")
    _sign_in(client, "admin@example.com")

    assert client.post(f"/admin/users/{target}/make-admin").status_code == 303

    _sign_in(client, "user@example.com")
    assert client.get("/admin/users").status_code == 200


@pytest.mark.parametrize("action", ["disable", "enable", "make-admin"])
def test_an_unknown_account_is_a_404(client: TestClient, action: str) -> None:
    _sign_in(client, "admin@example.com")

    response = client.post(f"/admin/users/{uuid.uuid4()}/{action}")

    assert response.status_code == 404


# --- services -------------------------------------------------------------------


@pytest.fixture
def auth(db_session: AsyncSession) -> AuthService:
    return AuthService(db_session, session_ttl=timedelta(hours=1))


@pytest.mark.parametrize(
    ("email", "name"),
    [("a" * 320 + "@x.com", "A"), ("a@example.com", "n" * 101)],
)
async def test_create_user_rejects_what_it_cannot_store(
    auth: AuthService, email: str, name: str
) -> None:
    with pytest.raises(AccountError):
        await auth.create_user(email=email, display_name=name, password=PASSWORD)


async def test_disabling_forgets_the_devices_too(
    auth: AuthService, db_session: AsyncSession
) -> None:
    admin = await auth.create_user(
        email="a@example.com", display_name="A", password=PASSWORD, is_admin=True
    )
    user = await auth.create_user(
        email="u@example.com", display_name="U", password=PASSWORD
    )
    signed_in = await auth.sign_in("u@example.com", PASSWORD)
    assert signed_in is not None
    assert await db_session.scalar(select(KnownDevice.id)) is not None

    await AdminService(db_session).set_active(admin, user.id, active=False)

    assert await db_session.scalar(select(KnownDevice.id)) is None
    assert (
        await db_session.scalar(
            select(AuthSession.id).where(
                AuthSession.token_hash == hash_token(signed_in.token)
            )
        )
        is None
    )
    refreshed = await db_session.get(User, user.id)
    assert refreshed is not None
    assert not refreshed.is_active
